"""deep-research 并发 fan-out 闭环图：一张 submit 驱动的 L2 业务图。

一个 research 工单走完
`seed -> {dispatch -[Send]-> collect* -> harvest -> converge} 循环 -> debate -> finalise`，
在 run root 下产出 `report.md` 与 `result.json`。R3 起 dispatch 用 LangGraph Send API
按 wave 并发派发（缺省 W=4，`concurrency` 可配）：一个 wave 内至多 `concurrency` 个
open clue 同时 `dispatched`，collect 对每个 clue 单粒度的 launch+wait 并行执行，真实
时间重叠、wall-clock 下降（而非仅结构并列）。

节点纯度（规格第 5 条）：
- **script 节点**（dispatch / collect / harvest / converge / debate_report / finalise /
  anchor_check）零 LLM 调用，只做计数、落盘与路由——它们不读含义、不写代理结论。
- **LLM 节点**只有 `seed`（TextNode，纯文本进出，产出初始 clue 列表）与 R4 的
  对抗裁决子图 `debate`（advocate → opponent → judge → arbiter 四段 structured run，
  全部 `write=False`、语料经 `--prompt-file` 文件投递、`--input` 只携带 manifest）。
  `report.md` 由脚本节点 `debate_report`（零 LLM）从 judge/arbiter 产出组装，不是
  任何一个 LLM 角色直接写出的。arbiter 的 `verdict=continue` 只记录并响亮落盘（进
  report「分歧裁定」段 + events），不改动 converge 的路由语义——循环继续/终止仍由
  纯函数 `converge()` 决定。
- **R5 anchor_check**（零 LLM、零外呼 IO 的纯脚本节点）：finalise 之后把 report.md
  每条 `[anchor: …]` 引用核验回 evidence.jsonl，产出 `run_root/anchor-check.json`
  并在 report.md 报告头写 `dr-anchor-rate`。核验率 ≤90% 是软闸门：响亮记录（报告头
  + anchor-check.json + events），不判红、不改 converge 路由。正文/verdict 一律落
  文件，state 只装 id 与计数，不进 checkpoint。

state 只装 id 与计数（规格第 7 条）：clue 板（id/status/depth/retry）与
coverage/zero_growth_rounds/rounds。findings 逐条 append 到 `evidence.jsonl`、
debate 四角色 body/verdict 逐字落 `run_root/debate/`、报告正文落 `report.md`——
正文永不进 checkpoint，kill-restart 后重放不会重复大段文本。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from fleet_graph.executors.agent_run import AgentRunSpec, RunWaitTimeout, derive_run_id
from fleet_graph.executors.text_node import TextSpec
from fleet_graph.graphs.adapters import parse_envelope
from fleet_graph.research_anchor import SOFT_GATE_RATE, check_run
from fleet_graph.research_bus import (
    DOC_KIND_REPORT,
    PIPELINE_STATUS_TO_PROTOCOL,
    RESEARCH_CLUE_KIND,
    RESEARCH_DOC_KIND,
    RESEARCH_EVIDENCE_KIND,
    PublishDegradation,
    body_digest,
    clue_idempotency_key,
    clue_index_channel,
    clue_payload,
    doc_idempotency_key,
    doc_payload,
    docs_channel,
    evidence_channel,
    evidence_idempotency_key,
    evidence_payload,
    finding_anchor,
    publish_best_effort,
)
from fleet_graph.state.run_artifacts import iso, write_json_durable

# role 名（规格第 8 条）：agent-runtime 侧 role 由另单交付，本单测试全部用 fake
# launcher/text node，不依赖真实 role 存在。R2 起 dispatch 按 clue 的 source 路由到
# SOURCE_ROLE 矩阵（6 个 dr-worker 角色），不再用单一 worker 角色。
WORKER_ROLE = "research_worker_local"

# R4（对抗裁决）四角色：逐字引用 agent-runtime 已交付角色，绝不新造 / 改名 /
# 重注册角色。advocate（glm-5.2）/ opponent（gpt-5.6-sol）/ judge（deepseek-v4-pro）
# 三方三条不同模型腿，满足宪法条5「多模型讨论」；arbiter（claude-opus-5）整板裁决。
ADVOCATE_ROLE = "dr-debater-advocate"
OPPONENT_ROLE = "dr-debater-opponent"
JUDGE_ROLE = "dr-debater-judge"
ARBITER_ROLE = "dr-arbiter"

# debate 子图的角色顺序（自增链路）：judge/arbiter 的输入依赖前序角色产出。
DEBATE_ROLES = (ADVOCATE_ROLE, OPPONENT_ROLE, JUDGE_ROLE, ARBITER_ROLE)

# 多源 worker 矩阵（R2）：source 词汇 -> dr-worker 角色。SOURCE_ROLE 是纯库函数常量，
# dispatch 只读它路由，不做内联 if；value 必须逐字等于 agent-runtime 已交付的 6 个
# dr-worker 角色名，绝不新造 / 改名 / 重注册角色。
SOURCE_ROLE: dict[str, str] = {
    "code-local": "dr-worker-code-local",
    "code-remote": "dr-worker-code-remote",
    "wiki": "dr-worker-wiki",
    "feishu": "dr-worker-feishu",
    "content": "dr-worker-content",
    "web": "dr-worker-web",
}

# 矩阵词汇（固定顺序）：seed 缺省标注 / 未知 source 回填用。取首元素为默认源。
DEFAULT_SOURCES: list[str] = ["code-local", "code-remote", "wiki", "feishu", "content", "web"]
DEFAULT_SOURCE: str = DEFAULT_SOURCES[0]

# roles 侧 protocol 契约（agent-runtime profiles/roles/schemas/，SSoT 在 roles 仓）：
# - worker input  deep-research.worker-input/v1：{clue_id, clue_text[, depth,
#   sources[], revision, allowed_root]}
# - worker result worker.result.v1：{evidences[{quote,claim,source,locator,revision,
#   range?,uri?,digest?}], proposed_clues[{clue,reason}], materials[{uri,digest?}]}
#   （无 verdict / 无 clue_id——调查完成与否由 evidences 判定，见 collect 节点）
# - debater input deep-research.debater-input/v1：{question, evidences[{anchor,quote,
#   claim,clue_id?}], prior_arguments[]}
# - debater output dr-doc.result.v1：{body}
# - arbiter input deep-research.arbiter-input/v1：{question, board_stats{
#   clues_total, clues_explored, clues_pending, clues_dropped, evidence_total,
#   zero_growth_rounds, rounds_elapsed}, clue_titles[{clue_id,title,status?,depth?}],
#   recent_claims[{claim,clue_id?,round?}]}（rounds 并入 board_stats.rounds_elapsed，
#   不再有顶层 recent_rounds）
# - arbiter output dr-arbiter.result.v1：{verdict∈{enough,continue}, rationale}
# role 声明 protocol.input 后 agent-run 强制要求 --input，缺了直接 CONTRACT_ERROR，
# 所以 dispatch / debate 四角色必须为每个 run 落 input 文件并传 spec.input_path。

DISPATCHER_LABEL = "fleet-graph"

# clue 状态机（规格第 7 条）：open -> dispatched -> done | blocked。
# retry<2 失败回 open，=2 置 blocked——单 clue 失败绝不 fault 整图。
CLUE_OPEN = "open"
CLUE_DISPATCHED = "dispatched"
CLUE_DONE = "done"
CLUE_BLOCKED = "blocked"

# 终态词汇：exit 0 当且仅当 terminal ∈ {converged, capped, partial}。
TERMINAL_CONVERGED = "converged"
TERMINAL_CAPPED = "capped"
TERMINAL_PARTIAL = "partial"
TERMINAL_FAULT = "fault"

# converge 的继续信号：区别于任何终态。
CONVERGE_CONTINUE = "continue"

# 一个 clue 最多重试 2 次（第 1 次失败回 open，第 2 次失败置 blocked）。
MAX_RETRIES = 2

# run root 下的产物文件名。dd 形状（规格第 4 条）：events.jsonl + result.json 由
# runner 侧写；这里的是节点侧产物。R4 起报告正文由 debate_report 脚本节点从
# judge/arbiter 产出组装（零 LLM），四角色产出逐字落 run_root/debate/ 下。
EVIDENCE_FILE = "evidence.jsonl"
REPORT_FILE = "report.md"
SEED_FILE = "seed.json"
DEBATE_DIR = "debate"
ADVOCATE_FILE = "advocate.md"
OPPONENT_FILE = "opponent.md"
JUDGE_FILE = "judge.md"
ARBITER_FILE = "arbiter.json"

# debate 角色 -> run_root/debate/ 下的产出文件名（spec 落地约定，供审计对账）。
DEBATE_OUTPUT_FILES = {
    "advocate": ADVOCATE_FILE,
    "opponent": OPPONENT_FILE,
    "judge": JUDGE_FILE,
    "arbiter": ARBITER_FILE,
}

# judge 正文的机器可判段标记（R4 判据 ②）：judge 逐字保留 OPEN DISAGREEMENT，
# 脚本节点据此原样搬进 report.md「开放分歧」列表，绝不调和 / 删除 / 改写为共识。
OPEN_DISAGREEMENT_MARKER = "OPEN DISAGREEMENT:"
RULE_MARKER = "RULE:"

SEED_SYSTEM = (
    "You plan a deep-research investigation. Answer with a JSON array of clue "
    "entries. Each entry is a plain clue string, or an object "
    '{"text": str, "source": str}. No prose, no markdown fences.'
)
SEED_PROMPT = (
    "为研究问题生成初始调查线索（仅返回 JSON 数组）。每条线索请标注 source，"
    "source 取值仅限：{sources}；纯字符串将回填默认源 {default_source}。\n问题：{question}"
)

# worker 的输出契约由 role 侧 protocol.output（worker.result.v1）钉死，
# 这里不再随 prompt 复述 schema——prompt 只投递题面与线索。
WORKER_PROMPT = (
    "研究问题：{question}\n\n调查线索：{clue}\n\n"
    "围绕该线索收集事实 findings，并给出可继续深挖的子线索 new_clues。"
)

# R4 对抗裁决：debater/arbiter 的 prompt 只投递题面与语料正文，出场契约由角色侧
# protocol 钉死（不再随 prompt 复述 schema）。语料经 --prompt-file 投递，--input 只
# 携带 manifest。
# - debater 正文约定：advocate（正面论证）/ opponent（反驳证伪）每条实质性主张带
#   [anchor: …]；judge 逐条分歧裁定必须按 RULE: / OPEN DISAGREEMENT: 两段行输出，
#   让脚本节点能逐字提取进 report.md「分歧裁定」段（机器判据 ②）。
DEBATER_PROMPT = "研究问题：{question}\n\n证据语料：\n{evidences}\n\n{instruction}"
ADVOCATE_INSTRUCTION = (
    "你是 advocate：基于证据对研究问题给出正面论证。每条实质性主张必须标注证据出处"
    " [anchor: …]。正文仅以 markdown 输出。"
)
OPPONENT_INSTRUCTION = (
    "你是 opponent：基于证据对研究问题给出反驳/证伪路径。每条实质性主张必须标注证据"
    " 出处 [anchor: …]。正文仅以 markdown 输出。"
)
JUDGE_INSTRUCTION = (
    "你是 judge：逐条裁定 advocate 与 opponent 之间的分歧。\n"
    "advocate 论证：\n{advocate_body}\n\nopponent 论证：\n{opponent_body}\n\n"
    "对每一条分歧：\n"
    "1. 能被既有证据裁决的 → 输出一行「RULE: <分歧> 裁决：<结论> [anchor: <锚点>]」；\n"
    "2. 不能裁决的 → 输出一行「OPEN DISAGREEMENT: <分歧原文>」逐字保留，不得调和、"
    "不得改写为共识。\n正文仅以 markdown 输出。"
)
ARBITER_PROMPT = (
    "研究问题：{question}\n\n板面统计：{board_stats}\n关键线索：{clue_titles}\n"
    "近期主张：{recent_claims}\n已进行轮次：{recent_rounds}\n\n"
    "对整板给一次裁决：verdict ∈ {{enough, continue}}，并给出 rationale。正文仅以"
    " JSON 输出。"
)


def derive_research_id(question: str) -> str:
    """`research_id` 由问题文本内容寻址派生：sha256 前 12 hex，前缀 `r-`（规格第 2 条）。

    内容寻址意味着同一问题恒得同一 id，thread identity 才能跨重启稳定。
    """
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
    return f"r-{digest}"


def derive_run_instance(run_root: Path | str) -> str:
    """`run_instance` 由 run_root 内容寻址派生（R3-fix，规格第 1 条）。

    research 的 thread 身份注入稳定的 run 实例分量：同一题两次独立跑（不同 run_root）
    派生**不同** thread_id/run_id，不再撞 bus 409 IDEMPOTENCY_CONFLICT；同一次 run 的
    kill-restart（同 run_root）仍得**相同**身份，re-adopt/幂等不回退。

    **稳定非随机**（规格边界硬线）：sha256(resolved run_root 绝对路径) 前 12 hex，
    前缀 `i-`。绝不掺 uuid4 / 时间戳——掺了就 kill-restart 漂移，re-adopt 失效。
    """
    resolved = str(Path(run_root).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    return f"i-{digest}"


def derive_clue_id(query: str, source: str | None = None) -> str:
    """clue id 同样内容寻址派生（`c-` + sha256 前 12 hex）。

    R2 起按 source 参与寻址（`text|source`）：同一题面从不同源探查，clue id 不互相
    顶撞。``source=None`` 时退化为只按 text 寻址，与 R1 完全一致（向后兼容，不破 R1）。
    """
    key = query.strip()
    if source is not None:
        key = f"{key}|{source}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"c-{digest}"


def worker_run_id(thread_id: str, clue_id: str, retry: int) -> str:
    """worker 的派生 run id（规格第 5 条）：`derive_run_id(thread_id, "worker/{id}", retry+1)`。

    同 thread 同 clue 同 retry 恒得同 id——kill-restart 后同 id 重派即 re-adopt
    （launcher 已保证幂等），绝不会对在途 run 二次派发。
    """
    return derive_run_id(thread_id, f"worker/{clue_id}", retry + 1)


def debate_run_id(thread_id: str, role: str, attempt: int = 1) -> str:
    """debate 子图的派生 run id（规格落地约定）：`derive_run_id(thread_id, "debate/{role}", 1)`。

    同 thread 同角色恒得同 id——kill-restart 后同 id 重派即 re-adopt（launcher 幂等），
    绝不二次派发。``role`` 用角色简称（advocate/opponent/judge/arbiter）。
    """
    return derive_run_id(thread_id, f"debate/{role}", attempt)


@dataclass(frozen=True)
class ResearchBounds:
    """收敛判定的纯计数边界（规格第 6 条）。无时间、无 LLM、无 IO。

    - max_clues / max_depth：clue 总数/深度触顶 -> capped。
    - zero_growth_rounds：coverage 零增长连续 N 轮 -> converged。
    - max_rounds：轮次预算触顶 -> capped（预算截断尚未收敛的研究）。
    - concurrency：每 wave 最多并发派发的 open clue 数（R3，缺省 4）。
      只影响「同 wave 派几个」，不影响 clue id / input / run id 的派生。
    """

    max_clues: int = 12
    max_depth: int = 6
    zero_growth_rounds: int = 3
    max_rounds: int = 24
    concurrency: int = 4


def _merge_clues(
    current: list[dict[str, Any]] | None, update: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """clue 板的合并 reducer（R3 fan-out）：按 id 合并，update 覆盖同 id 条目。

    并行 collect 各自只返回自己那条 clue 的更新（dispatch/harvest 返回整板），
    LangGraph 用这个 reducer 把它们合并回 clue 板。顺序保持 current 的首见序、
    新 id 追加在尾部——children 的发现顺序因此允许与 W 相关，但集合不变。
    """
    merged = list(current or [])
    index = {c["id"]: i for i, c in enumerate(merged)}
    for entry in update:
        i = index.get(entry["id"])
        if i is None:
            index[entry["id"]] = len(merged)
            merged.append(entry)
        else:
            merged[i] = entry
    return merged


def _merge_heads(current: dict[str, str] | None, update: dict[str, str]) -> dict[str, str]:
    """clue_heads 的合并 reducer（R3 fan-out）：dict 浅合并，后者覆盖同键。"""
    return {**(current or {}), **(update or {})}


class ResearchState(TypedDict, total=False):
    """state 只装 id 与计数（规格第 7 条）。findings/report 正文一律落 run root 文件。"""

    research_id: str
    question: str
    generation: int
    #: clue 板：每项只含 id/status/depth/retry/source，不含线索正文（正文在 clues/<id>.json）。
    #: R3 起是 reducer channel：并行 collect 各自更新自己那条，按 id 合并。
    clues: Annotated[list[dict[str, Any]], _merge_clues]
    rounds: int
    #: coverage = done clue 计数。零增长连续 N 轮即 converged。
    coverage: int
    zero_growth_rounds: int
    #: 本轮已 dispatch 的 clue id 集合（只存 id，规格第 3 条）。collect 经 Send 的
    #: clue_id 定位，不再有全局 pending_clue_id；harvest 用它知道该收割哪些 clue。
    dispatched_ids: list[str]
    #: Send 命令携带的单 clue id（collect 的输入，只出现在 fan-out 任务的 state 里）。
    clue_id: str
    terminal: str
    terminal_reason: str
    #: clue 实体在 bus 上的版本头（message_id），维护 ``research.clue.v2`` 的
    #: ``supersedes`` 版本链。只装 id（规格第 7 条：state 只装 id 与计数）。
    #: R3 起是 reducer channel：并行 collect 各自更新自己那条，按 dict 合并。
    clue_heads: Annotated[dict[str, str], _merge_heads]
    #: clue 实体的 bus entity 锚（root 首发的 message_id）。bus 原生版本链里
    #: entity 不随 supersedes 换，续条必须用这个锚做 entity_id。只装 id。
    clue_entities: Annotated[dict[str, str], _merge_heads]


@dataclass
class ResearchDeps:
    """图对外只依赖这几个端口，全部注入以便测试替换。"""

    question: str
    research_id: str
    thread_id: str
    run_root: Path
    text_node: Any
    launcher: Any
    bounds: ResearchBounds = field(default_factory=ResearchBounds)
    seed_model: str = "deepseek-v4-flash"
    #: 多源矩阵词汇（R2，规格第 8 条）：默认固定顺序，取首元素为默认源（回填与 seed 提示用）。
    #: 生产装配 ResearchConfig.sources；测试注入自定义列表验证路由/回填。
    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    worker_role: str = WORKER_ROLE
    #: R4 对抗裁决四角色（逐字 = agent-runtime 已交付角色名，SSoT 在 roles 仓）。
    advocate_role: str = ADVOCATE_ROLE
    opponent_role: str = OPPONENT_ROLE
    judge_role: str = JUDGE_ROLE
    arbiter_role: str = ARBITER_ROLE
    worker_timeout_seconds: int = 900
    debater_timeout_seconds: int = 900
    arbiter_timeout_seconds: int = 900
    poll_interval: float = 2.0
    observe: Any = None
    clock: Any = None
    #: 发布端口：协议上等价 ``BusClient.publish``（含 entity_id / supersedes /
    #: idempotency_key）。生产装配真实 BusClient；测试注入 fake transport。
    #: None = 不发布（best-effort 降级，同 observe 缺失时一样静默）。
    publisher: Any = None
    #: best-effort 发布的降级观测（R1-返工）：每次被吞掉的发布失败都记入这里，
    #: run 终局产物据此落 ``publish_degraded``——降级不许静默。
    publish_degraded: PublishDegradation = field(default_factory=PublishDegradation)

    @property
    def default_source(self) -> str:
        """矩阵首元素 = 默认源：未知 / 缺失 source 的 clue 回填到这个源。"""
        return self.sources[0] if self.sources else DEFAULT_SOURCE

    def now(self) -> float:
        return self.clock() if self.clock is not None else time.time()


def converge(state: ResearchState, bounds: ResearchBounds) -> str:
    """收敛判定，纯函数（规格第 6 条）：纯计数，无时间、无 IO。

    优先级：
    1. clue 总数/深度触顶 -> capped（capped 绝不报成 converged——规格硬性要求）。
    2. 无 open clue：线索树耗尽 -> partial（有 blocked）或 converged（全部 done）。
    3. coverage 零增长连续 N 轮 -> partial（有 blocked 且其余收敛）或 converged。
    4. 轮次预算触顶 -> capped。
    5. 否则 continue。
    """
    clues = state.get("clues", [])
    total = len(clues)
    open_clues = [c for c in clues if c["status"] == CLUE_OPEN]
    blocked = [c for c in clues if c["status"] == CLUE_BLOCKED]

    # 触顶判定先行：规格明确 capped 不得报成 converged。
    if total >= bounds.max_clues:
        return TERMINAL_CAPPED
    if any(c["depth"] >= bounds.max_depth for c in clues):
        return TERMINAL_CAPPED

    # 线索树耗尽：没有可派的工作。
    if not open_clues:
        return TERMINAL_PARTIAL if blocked else TERMINAL_CONVERGED

    # coverage 零增长：clue 持续失败/无新发现，研究已停滞。
    if state.get("zero_growth_rounds", 0) >= bounds.zero_growth_rounds:
        return TERMINAL_PARTIAL if blocked else TERMINAL_CONVERGED

    # 轮次预算：还有 open 工作但预算耗尽，截断为 capped。
    if state.get("rounds", 0) >= bounds.max_rounds:
        return TERMINAL_CAPPED

    return CONVERGE_CONTINUE


def initial_state(research_id: str, question: str, generation: int) -> ResearchState:
    """图的入口 state。clue 板与计数清零，seed 节点填充初始线索。"""
    return {
        "research_id": research_id,
        "question": question,
        "generation": generation,
        "clues": [],
        "rounds": 0,
        "coverage": 0,
        "zero_growth_rounds": 0,
        "dispatched_ids": [],
    }


# --- 节点侧落盘助手 ---------------------------------------------------------


def _clue_file_path(run_root: Path, clue_id: str) -> Path:
    return run_root / "clues" / f"{clue_id}.json"


def _prompt_path(run_root: Path, clue_id: str) -> Path:
    return run_root / "clues" / f"{clue_id}-prompt.md"


def _result_path(run_root: Path, clue_id: str) -> Path:
    return run_root / "clues" / f"{clue_id}-result.json"


def _worker_input_path(run_root: Path, clue_id: str, retry: int) -> Path:
    """worker 的 --input 文件（deep-research.worker-input/v1）。按 retry 区分：
    retry 重派是一个新 run，input 文件与 run 一一对应，续跑时可整体复核。"""
    return run_root / "inputs" / f"worker-{clue_id}-r{retry}.json"


def _debate_input_path(run_root: Path, role: str) -> Path:
    """debate 角色的 --input manifest 文件（deep-research.debater-input/v1 或
    deep-research.arbiter-input/v1）。与 role 一一对应，落 inputs/ 独立文件。"""
    return run_root / "inputs" / f"debate-{role}.json"


def _debate_prompt_path(run_root: Path, role: str) -> Path:
    """debate 角色的 --prompt-file 语料文件（正文：题面 + 证据 + 前序角色产出）。"""
    return run_root / "inputs" / f"debate-{role}-prompt.md"


def _debate_output_path(run_root: Path, role: str) -> Path:
    """debate 角色产出落盘：debater body 逐字落 run_root/debate/<role>.md，
    arbiter 落 run_root/debate/arbiter.json。"""
    return run_root / DEBATE_DIR / DEBATE_OUTPUT_FILES[role]


def _worker_spec(deps: ResearchDeps, *, clue_id: str, source: str, retry: int) -> AgentRunSpec:
    """worker 的 AgentRunSpec（deep-research.worker-input/v1）。dispatch 与 collect 共用：
    dispatch 先 launch 全部（spawn detached worker，真实时间重叠），collect 同 id 再 launch
    = re-adopt 在途 run（launcher 幂等）后 wait。role 按 source 路由（R2 矩阵）。"""
    role = SOURCE_ROLE.get(source, SOURCE_ROLE[deps.default_source])
    return AgentRunSpec(
        prompt="",
        role=role,
        input_path=str(_worker_input_path(deps.run_root, clue_id, retry)),
        prompt_file=str(_prompt_path(deps.run_root, clue_id)),
        structured=True,
        write=False,
        timeout_seconds=deps.worker_timeout_seconds,
        labels={"dispatcher": DISPATCHER_LABEL, "research": deps.research_id},
    )


def _write_clue_file(run_root: Path, clue_id: str, query: str, *, depth: int, source: str) -> None:
    # 线索正文落在文件里而不是 state 里：state 只装 id/status/depth/retry/source（规格第 7 条）。
    write_json_durable(
        _clue_file_path(run_root, clue_id),
        {"id": clue_id, "query": query, "depth": depth, "source": source},
    )


def _read_clue_query(run_root: Path, clue_id: str) -> str:
    return json.loads(_clue_file_path(run_root, clue_id).read_text(encoding="utf-8"))["query"]


def _append_evidence(run_root: Path, clue_id: str, depth: int, finding: Any, now: float) -> None:
    """逐条 append 一条 evidence（规格第 7 条）：findings 只落盘，不进 state。

    finding 是契约里的结构化对象 {claim, source, quote, locator}，原样落盘
    （不 str() 压平——debate 语料与人工复核都要 locator/quote 原文）。"""
    entry = {"at": iso(now), "clue_id": clue_id, "depth": depth, "finding": finding}
    with (run_root / EVIDENCE_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()


def _clue(clues: list[dict[str, Any]], clue_id: str) -> dict[str, Any] | None:
    for entry in clues:
        if entry["id"] == clue_id:
            return entry
    return None


def _set_clue(clues: list[dict[str, Any]], clue_id: str, **changes: Any) -> list[dict[str, Any]]:
    """返回更新后的新列表：langgraph 按 key 整体替换，原地改可能不进 checkpoint。"""
    return [{**entry, **changes} if entry["id"] == clue_id else entry for entry in clues]


def _done_count(clues: list[dict[str, Any]]) -> int:
    return sum(1 for c in clues if c["status"] == CLUE_DONE)


def _observe(deps: ResearchDeps, entry: dict[str, Any]) -> None:
    if deps.observe is not None:
        deps.observe(entry)


def _publish_clue(
    deps: ResearchDeps,
    *,
    clue_id: str,
    text: str,
    status: str,
    depth: int,
    retry: int,
    entity_id: str | None = None,
    supersedes: str | None = None,
    run_id: str | None = None,
    sources: list[str] | None = None,
) -> str | None:
    """发布一条 clue 状态迁移到 ``research:{research_id}.index``（research.clue.v2）。

    root 实体版本链是 **bus 原生** 的：首条发布**不传** entity_id（bus 分配
    ``entity_id = message_id`` 作锚），续条传 ``entity_id``（= 锚）+ ``supersedes``
    （= 上一版本头 message_id）。本地线索身份走 ``payload.clue_id`` 内容寻址。
    幂等 key 由 run/clue/status/retry 内容寻址派生，kill-restart 重派同 key 不产生
    重复实体。best-effort：失败只降级记录，返回 message_id 或 None。
    """
    protocol_status = PIPELINE_STATUS_TO_PROTOCOL[status]
    payload = clue_payload(
        text=text,
        status=protocol_status,
        depth=depth,
        sources=sources,
        run_id=run_id,
        clue_id=clue_id,
    )
    key = clue_idempotency_key(deps.research_id, clue_id, protocol_status, retry)
    return publish_best_effort(
        deps.publisher,
        channel_id=clue_index_channel(deps.research_id),
        kind=RESEARCH_CLUE_KIND,
        payload=payload,
        idempotency_key=key,
        entity_id=entity_id,
        supersedes=supersedes,
        degraded=deps.publish_degraded,
    )


def _publish_evidence(
    deps: ResearchDeps, *, clue_id: str, finding: dict[str, Any], depth: int
) -> str | None:
    """发布一条 finding 到 ``research:{research_id}.evidence``（research.evidence.v2）。

    leaf 实体：无版本链；``clue_id`` 指 clue 的 entity_id。幂等 key 由 finding
    内容寻址派生。best-effort：失败只降级记录。
    """
    payload = evidence_payload(clue_id=clue_id, finding=finding)
    key = evidence_idempotency_key(deps.research_id, clue_id, finding)
    return publish_best_effort(
        deps.publisher,
        channel_id=evidence_channel(deps.research_id),
        kind=RESEARCH_EVIDENCE_KIND,
        payload=payload,
        idempotency_key=key,
        degraded=deps.publish_degraded,
    )


def _publish_doc(deps: ResearchDeps, *, body: str) -> str | None:
    """发布 debate_report 组装出的报告到 ``research:{research_id}.docs``
    （research.doc.v2）。

    leaf 实体：``doc_kind=report``，``origin=research_id``，``digest`` = 正文内容
    寻址（全局去重键）。幂等 key 由 digest 派生。best-effort：失败只降级记录。
    """
    digest = body_digest(body)
    payload = doc_payload(
        doc_kind=DOC_KIND_REPORT, digest=digest, body=body, origin=deps.research_id
    )
    key = doc_idempotency_key(deps.research_id, digest)
    return publish_best_effort(
        deps.publisher,
        channel_id=docs_channel(deps.research_id),
        kind=RESEARCH_DOC_KIND,
        payload=payload,
        idempotency_key=key,
        degraded=deps.publish_degraded,
    )


def _head(clue_heads: dict[str, str] | None, clue_id: str) -> str | None:
    """某 clue 实体当前的 bus 版本头 message_id（版本链 supersedes 用）。"""
    return (clue_heads or {}).get(clue_id)


def _entity(clue_entities: dict[str, str] | None, clue_id: str) -> str | None:
    """某 clue 实体的 bus entity 锚（root 首发的 message_id）。

    续条必须用这个锚做 entity_id：bus 原生版本链里 entity 不随 supersedes 换，
    一旦锚错整条链就跨实体断裂。
    """
    return (clue_entities or {}).get(clue_id)


# --- 节点 -------------------------------------------------------------------


def _seed_node(deps: ResearchDeps):
    def seed(state: ResearchState) -> ResearchState:
        question = state.get("question") or deps.question
        result = deps.text_node.complete(
            TextSpec(model=deps.seed_model, system=SEED_SYSTEM, max_tokens=2048),
            SEED_PROMPT.format(
                question=question,
                sources=", ".join(deps.sources),
                default_source=deps.default_source,
            ),
        )
        seed_text = result.text
        # seed 正文落盘（审计用），不进 state。
        write_json_durable(
            deps.run_root / SEED_FILE, {"question": question, "seed_text": seed_text}
        )

        try:
            queries = json.loads(seed_text)
        except json.JSONDecodeError as exc:
            # seed 输出不可解析 = LLM 节点故障，fault 整图（区别于 clue 失败，后者绝不 fault）。
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": f"seed 返回不可解析的 JSON：{exc}",
            }
        if not isinstance(queries, list):
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": "seed 未返回 clue 数组",
            }

        clues: list[dict[str, Any]] = []
        heads: dict[str, str] = {}
        entities: dict[str, str] = {}
        for item in queries:
            if isinstance(item, str):
                text, source = item, deps.default_source
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                text = item["text"]
                source = item.get("source") or deps.default_source
            else:
                # seed 项既非纯字符串也非 {"text","source"} 对象 = 输出畸形，fault。
                return {
                    "terminal": TERMINAL_FAULT,
                    "terminal_reason": "seed 项既非纯字符串也非 {text,source} 对象",
                }
            # 未知 / 缺失 source 属 clue 级降级：回填默认源，绝不 fault 整图。
            if source not in SOURCE_ROLE:
                source = deps.default_source
            clue_id = derive_clue_id(text, source)
            if _clue(clues, clue_id) is not None:
                # 内容寻址 id 去重：重复线索只入板一次。
                continue
            _write_clue_file(deps.run_root, clue_id, text, depth=0, source=source)
            clues.append(
                {"id": clue_id, "status": CLUE_OPEN, "depth": 0, "retry": 0, "source": source}
            )
            # 发布初始 open 状态（research.clue.v2，root 版本链起点）。
            mid = _publish_clue(
                deps,
                clue_id=clue_id,
                text=text,
                status=CLUE_OPEN,
                depth=0,
                retry=0,
                sources=[source],
            )
            if mid:
                heads[clue_id] = mid
                entities[clue_id] = mid

        _observe(deps, {"event": "seed", "clues": len(clues)})
        return {
            "clues": clues,
            "clue_heads": heads,
            "clue_entities": entities,
            "coverage": 0,
            "zero_growth_rounds": 0,
            "rounds": 0,
        }

    return seed


def _dispatch_node(deps: ResearchDeps):
    def dispatch(state: ResearchState) -> ResearchState:
        clues = state.get("clues", [])
        open_clues = [c for c in clues if c["status"] == CLUE_OPEN]
        wave = open_clues[: deps.bounds.concurrency]  # R3：本 wave 至多 concurrency 个。
        if not wave:
            # 无 open clue：让 converge 判定（converged / partial）。
            return {"dispatched_ids": []}

        new_clues = list(clues)
        heads = dict(state.get("clue_heads", {}))
        entities = dict(state.get("clue_entities", {}))
        dispatched_ids: list[str] = []
        for clue in wave:
            clue_id = clue["id"]
            # R2：clue 板每项带 source；未知 / 缺失回填默认源（clue 级降级，绝不 fault）。
            source = clue.get("source") or deps.default_source
            if source not in SOURCE_ROLE:
                source = deps.default_source
            query = _read_clue_query(deps.run_root, clue_id)
            prompt = WORKER_PROMPT.format(
                question=state.get("question") or deps.question, clue=query
            )
            prompt_path = _prompt_path(deps.run_root, clue_id)
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt, encoding="utf-8")
            # role 声明了 protocol.input（deep-research.worker-input/v1），agent-run 强制
            # --input：每个 run 落一份 input 文件，collect 侧经 spec.input_path 投递。
            # R2 input 形状：clue_id / clue_text / depth / sources（string array）。
            write_json_durable(
                _worker_input_path(deps.run_root, clue_id, clue["retry"]),
                {
                    "clue_id": clue_id,
                    "clue_text": query,
                    "depth": clue["depth"],
                    "sources": [source],
                },
            )

            _observe(
                deps,
                {"event": "dispatch", "clue_id": clue_id, "retry": clue["retry"], "source": source},
            )
            # 发布 open -> dispatched 迁移（research.clue.v2，supersedes 接上一版本头）。
            run_id = worker_run_id(deps.thread_id, clue_id, clue["retry"])
            mid = _publish_clue(
                deps,
                clue_id=clue_id,
                text=query,
                status=CLUE_DISPATCHED,
                depth=clue["depth"],
                retry=clue["retry"],
                entity_id=_entity(entities, clue_id),
                supersedes=_head(heads, clue_id),
                run_id=run_id,
                sources=[source],
            )
            if mid:
                heads[clue_id] = mid
            # dispatch 先 launch 全部（真实时间重叠）：本 wave 全部 worker 此刻 spawn，
            # collect 对每个 run_id 再 launch = re-adopt 在途 run（launcher 幂等）后 wait。
            deps.launcher.launch(
                _worker_spec(deps, clue_id=clue_id, source=source, retry=clue["retry"]), run_id
            )
            new_clues = _set_clue(new_clues, clue_id, status=CLUE_DISPATCHED)
            dispatched_ids.append(clue_id)
        return {
            "clues": new_clues,
            "clue_heads": heads,
            "clue_entities": entities,
            "dispatched_ids": dispatched_ids,
        }

    return dispatch


def _after_dispatch(state: ResearchState) -> str | list[Send]:
    """dispatch 的 fan-out 路由：有本 wave 的 dispatched_ids 就 Send 给 collect，
    否则让 converge 判定（converged / partial）。Send payload 携带 clue_id + 该 clue
    的板条目 + 上一版本头 + entity 锚——collect 是单 clue 粒度，任务输入就是
    Send 命令的内容。
    """
    ids = state.get("dispatched_ids", [])
    if not ids:
        return "converge"
    clues = {c["id"]: c for c in state.get("clues", [])}
    heads = state.get("clue_heads", {})
    entities = state.get("clue_entities", {})
    return [
        Send(
            "collect",
            {
                "clue_id": cid,
                "clue": clues[cid],
                "prev_head": heads.get(cid),
                "prev_entity": entities.get(cid),
            },
        )
        for cid in ids
    ]


def _collect_node(deps: ResearchDeps):
    def collect(state: ResearchState) -> ResearchState:
        clue_id = state.get("clue_id", "")
        clue = state.get("clue")
        if not clue_id or clue is None:
            return {}

        # R3：collect 单 clue 粒度。clue 条目由 Send 命令携带（id/status/depth/retry/
        # source，只装 id 与计数），取代全局 pending_clue_id。run id 派生仍与并发度无关。
        retry = clue["retry"]
        depth = clue["depth"]
        run_id = worker_run_id(deps.thread_id, clue_id, retry)
        # R2：按 clue.source 路由到 SOURCE_ROLE 矩阵（纯常量，不做内联 if）。未知 /
        # 缺失 source 已在 dispatch 回填默认源，这里只防御性兜底，绝不 fault。
        source = clue.get("source") or deps.default_source
        # dispatch 已 launch 全部；同 run_id 再 launch = re-adopt 在途 run（幂等）。
        spec = _worker_spec(deps, clue_id=clue_id, source=source, retry=retry)
        ticket = deps.launcher.launch(spec, run_id)
        try:
            status = deps.launcher.wait(
                ticket,
                poll_interval=deps.poll_interval,
                deadline_seconds=deps.worker_timeout_seconds + 120,
            )
        except RunWaitTimeout:
            # wait 超时 = 该 run 仍在跑，绝不是已丢：run id 派生恒定，retry 会
            # re-adopt 在途 run 而不是二次派发（launcher 幂等）。按 clue 失败落
            # retry/block，绝不 fault 整图（规格第 7 条），与 dd_actors.py /
            # supervisor.py 的同款降级一致。
            _observe(
                deps,
                {"event": "collect", "clue_id": clue_id, "run_id": run_id, "timeout": True},
            )
            retry = retry + 1
            new_status = CLUE_BLOCKED if retry >= MAX_RETRIES else CLUE_OPEN
            query = _read_clue_query(deps.run_root, clue_id)
            mid = _publish_clue(
                deps,
                clue_id=clue_id,
                text=query,
                status=new_status,
                depth=depth,
                retry=retry,
                entity_id=state.get("prev_entity"),
                supersedes=state.get("prev_head"),
                sources=[source],
            )
            return {
                "clues": [
                    {
                        "id": clue_id,
                        "status": new_status,
                        "depth": depth,
                        "retry": retry,
                        "source": source,
                    }
                ],
                "clue_heads": {clue_id: mid} if mid else {},
            }
        # run 结果落盘（harvest 据此提取新线索，resume 后也能重读）。
        write_json_durable(_result_path(deps.run_root, clue_id), status.result or {})

        # 消费契约 worker.result.v1：合法信封（isinstance dict 且 evidences 是
        # list）即判「调查完成」——evidences 非空 = found，evidences 空 =
        # not_found，二者都属 done，不触发 retry。工具面不可用 / blocked 不再依赖
        # worker 自报字段（新契约无 verdict）：改由 run 失败（status.ok 为假）、
        # 信封解析失败或 wait 超时触发，仍走 retry/block 路径，绝不 fault 整图。
        # R2 契约字段是 evidences[{quote,claim,source,locator,revision,range?,uri?,
        # digest?}]（不再是 R1 的 findings）。
        declared: dict[str, Any] | None = None
        if status.ok and status.result is not None:
            try:
                parsed = parse_envelope(status.result)
                if isinstance(parsed, dict) and isinstance(parsed.get("evidences"), list):
                    declared = parsed
            except Exception:
                # 信封解析失败按 clue 失败处理（retry/block），绝不 fault 整图。
                declared = None

        if declared is not None:
            for evidence in declared["evidences"]:
                # 每条 evidence -> R1 evidence：evidence 已是 {claim, source, quote,
                # locator} 形状，原样 append 与发布（research.evidence.v2 leaf）。
                _append_evidence(deps.run_root, clue_id, depth, evidence, deps.now())
                _publish_evidence(deps, clue_id=clue_id, finding=evidence, depth=depth)
            entry: dict[str, Any] = {
                "id": clue_id,
                "status": CLUE_DONE,
                "depth": depth,
                "retry": retry,
                "source": source,
            }
            query = _read_clue_query(deps.run_root, clue_id)
            mid = _publish_clue(
                deps,
                clue_id=clue_id,
                text=query,
                status=CLUE_DONE,
                depth=depth,
                retry=retry,
                entity_id=state.get("prev_entity"),
                supersedes=state.get("prev_head"),
                sources=[source],
            )
        else:
            retry = retry + 1
            new_status = CLUE_BLOCKED if retry >= MAX_RETRIES else CLUE_OPEN
            entry = {
                "id": clue_id,
                "status": new_status,
                "depth": depth,
                "retry": retry,
                "source": source,
            }
            query = _read_clue_query(deps.run_root, clue_id)
            mid = _publish_clue(
                deps,
                clue_id=clue_id,
                text=query,
                status=new_status,
                depth=depth,
                retry=retry,
                entity_id=state.get("prev_entity"),
                supersedes=state.get("prev_head"),
                sources=[source],
            )

        _observe(deps, {"event": "collect", "clue_id": clue_id, "run_id": run_id, "ok": status.ok})
        return {"clues": [entry], "clue_heads": {clue_id: mid} if mid else {}}

    return collect


def _harvest_node(deps: ResearchDeps):
    def harvest(state: ResearchState) -> ResearchState:
        clues = state.get("clues", [])
        heads = dict(state.get("clue_heads", {}))
        entities = dict(state.get("clue_entities", {}))
        dispatched_ids = state.get("dispatched_ids", [])

        # R3：一个 wave 可能收割多个 done clue（每个都提取 proposed_clues）。
        for clue_id in dispatched_ids:
            clue = _clue(clues, clue_id)
            if clue is None or clue["status"] != CLUE_DONE:
                continue
            raw = json.loads(_result_path(deps.run_root, clue_id).read_text(encoding="utf-8"))
            try:
                structured = parse_envelope(raw)
            except Exception:
                # collect 已判定 done 说明信封本可解析；这里解不出就按无子线索继续，
                # 绝不 fault 整图（与 collect 的降级同义，保续跑健壮性）。
                structured = {}
            # 契约字段是 proposed_clues[{clue, reason}]（worker.result.v1），
            # 不是旧设想的 new_clues 字符串数组，也不是 R1 的 [{text, rationale}]。
            proposed = structured.get("proposed_clues", []) if isinstance(structured, dict) else []
            # 子线索继承父 clue 的 source（worker 在哪个源取证，其子线索也归该源）。
            child_source = clue.get("source") or deps.default_source
            if child_source not in SOURCE_ROLE:
                child_source = deps.default_source
            for item in proposed:
                text = item.get("clue") if isinstance(item, dict) else None
                if not isinstance(text, str) or not text.strip():
                    continue
                child_id = derive_clue_id(text.strip(), child_source)
                if _clue(clues, child_id) is not None:
                    continue
                depth = clue["depth"] + 1
                _write_clue_file(
                    deps.run_root, child_id, text.strip(), depth=depth, source=child_source
                )
                clues = [
                    *clues,
                    {
                        "id": child_id,
                        "status": CLUE_OPEN,
                        "depth": depth,
                        "retry": 0,
                        "source": child_source,
                    },
                ]
                # 新子线索入板即发布初始 open（research.clue.v2 版本链起点）。
                mid = _publish_clue(
                    deps,
                    clue_id=child_id,
                    text=text.strip(),
                    status=CLUE_OPEN,
                    depth=depth,
                    retry=0,
                    sources=[child_source],
                )
                if mid:
                    heads[child_id] = mid
                    entities[child_id] = mid

        coverage = _done_count(clues)
        zero_growth = (
            0 if coverage > state.get("coverage", 0) else state.get("zero_growth_rounds", 0) + 1
        )
        rounds = state.get("rounds", 0) + 1

        _observe(deps, {"event": "harvest", "rounds": rounds, "coverage": coverage})
        return {
            "clues": clues,
            "clue_heads": heads,
            "clue_entities": entities,
            "coverage": coverage,
            "zero_growth_rounds": zero_growth,
            "rounds": rounds,
            "dispatched_ids": [],
        }

    return harvest


def _terminal_reason(verdict: str, state: ResearchState, bounds: ResearchBounds) -> str:
    clues = state.get("clues", [])
    total = len(clues)
    done = _done_count(clues)
    blocked = sum(1 for c in clues if c["status"] == CLUE_BLOCKED)
    if verdict == TERMINAL_CAPPED:
        if total >= bounds.max_clues:
            return f"max_clues {bounds.max_clues} 触顶（total={total}）"
        if any(c["depth"] >= bounds.max_depth for c in clues):
            return f"max_depth {bounds.max_depth} 触顶"
        return f"max_rounds {bounds.max_rounds} 触顶（rounds={state.get('rounds', 0)}）"
    if verdict == TERMINAL_PARTIAL:
        return f"{blocked} 个 clue retry 耗尽 blocked，其余 {done} 个 done"
    return f"coverage 收敛：{done}/{total} clues done，{state.get('rounds', 0)} rounds"


def _converge_node(deps: ResearchDeps):
    def converge_node(state: ResearchState) -> ResearchState:
        verdict = converge(state, deps.bounds)
        if verdict == CONVERGE_CONTINUE:
            return {}
        return {
            "terminal": verdict,
            "terminal_reason": _terminal_reason(verdict, state, deps.bounds),
        }

    return converge_node


def _load_evidences(run_root: Path) -> list[dict[str, Any]]:
    """读 evidence.jsonl 的 finding 形状（R4：复用既有协议，不新增中间协议）。

    返回 debater-input.v1 的 evidences 形状 {anchor, quote, claim, clue_id?}：
    anchor 复用 research_bus.finding_anchor（source@locator），quote/claim 原样，
    clue_id 取 entry 的归属线索 id。
    """
    path = run_root / EVIDENCE_FILE
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        finding = entry.get("finding") or {}
        out.append(
            {
                "anchor": finding_anchor(finding),
                "quote": finding.get("quote", ""),
                "claim": finding.get("claim", ""),
                "clue_id": entry.get("clue_id"),
            }
        )
    return out


def _format_evidences(evidences: list[dict[str, Any]]) -> str:
    """把 evidences 逐条转成 debater --prompt-file 语料的正文段落。"""
    if not evidences:
        return "（无证据）"
    lines: list[str] = []
    for i, ev in enumerate(evidences, 1):
        lines.append(f"{i}. [anchor: {ev['anchor']}] {ev['claim']}")
        if ev.get("quote"):
            lines.append(f"   quote: {ev['quote']}")
    return "\n".join(lines)


def _debater_input(
    question: str, evidences: list[dict[str, Any]], prior_arguments: list[str] | None = None
) -> dict[str, Any]:
    """deep-research.debater-input/v1 的 manifest。judge 才带 prior_arguments。"""
    payload: dict[str, Any] = {"question": question, "evidences": evidences}
    if prior_arguments is not None:
        payload["prior_arguments"] = prior_arguments
    return payload


def _debater_node(deps: ResearchDeps, *, role: str, role_value: str):
    """advocate/opponent/judge 三段 debater run 的公共节点工厂。

    语料经 --prompt-file 投递、--input 只携带 manifest（deep-research.debater-input
    /v1）；``write=False``、structured。产出 ``{body}`` 逐字落 run_root/debate/
    <role>.md。run 失败 / 信封不可解析 / 无 body → ``TERMINAL_FAULT``（响亮，不
    静默，沿用 synthesis 的失败语义）。
    """

    def node(state: ResearchState) -> ResearchState:
        question = state.get("question") or deps.question
        evidences = _load_evidences(deps.run_root)
        prior_arguments: list[str] | None = None
        if role == "judge":
            prior_arguments = [
                _read_debate_body(deps, "advocate"),
                _read_debate_body(deps, "opponent"),
            ]
        input_payload = _debater_input(question, evidences, prior_arguments)

        if role == "advocate":
            instruction = ADVOCATE_INSTRUCTION
        elif role == "opponent":
            instruction = OPPONENT_INSTRUCTION
        else:
            instruction = JUDGE_INSTRUCTION.format(
                advocate_body=prior_arguments[0], opponent_body=prior_arguments[1]
            )
        corpus = DEBATER_PROMPT.format(
            question=question, evidences=_format_evidences(evidences), instruction=instruction
        )

        write_json_durable(_debate_input_path(deps.run_root, role), input_payload)
        prompt_path = _debate_prompt_path(deps.run_root, role)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(corpus, encoding="utf-8")

        run_id = debate_run_id(deps.thread_id, role)
        spec = AgentRunSpec(
            prompt="",
            role=role_value,
            input_path=str(_debate_input_path(deps.run_root, role)),
            prompt_file=str(prompt_path),
            structured=True,
            write=False,
            timeout_seconds=deps.debater_timeout_seconds,
            labels={"dispatcher": DISPATCHER_LABEL, "research": deps.research_id},
        )
        ticket = deps.launcher.launch(spec, run_id)
        status = deps.launcher.wait(
            ticket,
            poll_interval=deps.poll_interval,
            deadline_seconds=deps.debater_timeout_seconds + 120,
        )

        if not (status.ok and status.result is not None):
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": f"debate/{role} run {run_id} 结束于 {status.state}",
            }
        try:
            declared = parse_envelope(status.result)
            body = declared.get("body") if isinstance(declared, dict) else None
        except Exception as exc:
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": f"debate/{role} 信封不可解析：{exc}",
            }
        if not isinstance(body, str) or not body:
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": f"debate/{role} 未返回 body 正文",
            }

        # 产出逐字落盘（dr-doc.result.v1 的 body），供审计与 agent-run 记录对账。
        _debate_output_path(deps.run_root, role).parent.mkdir(parents=True, exist_ok=True)
        _debate_output_path(deps.run_root, role).write_text(body, encoding="utf-8")
        _observe(deps, {"event": "debate", "role": role, "run_id": run_id})
        return {}

    return node


def _read_debate_body(deps: ResearchDeps, role: str) -> str:
    """读前序 debater 已落盘的 body 原文（judge 的 prior_arguments 来源）。"""
    return _debate_output_path(deps.run_root, role).read_text(encoding="utf-8")


def _arbiter_node(deps: ResearchDeps):
    def arbiter(state: ResearchState) -> ResearchState:
        question = state.get("question") or deps.question
        clues = state.get("clues", [])
        statuses = [c.get("status") for c in clues]
        done_clues = [c for c in clues if c["status"] == CLUE_DONE]
        evidences = _load_evidences(deps.run_root)
        # deep-research.arbiter-input/v1（arbiter-input.v1.json）的 board_stats：
        # 允许键 = clues_total / clues_explored / clues_pending / clues_dropped /
        # evidence_total / evidence_added_last_round / zero_growth_rounds /
        # rounds_elapsed，且 additionalProperties:false。rounds 并入
        # board_stats.rounds_elapsed，不再作为顶层 recent_rounds 发出。
        board_stats = {
            "clues_total": len(statuses),
            "clues_explored": statuses.count(CLUE_DONE),
            "clues_pending": statuses.count(CLUE_OPEN),
            "clues_dropped": statuses.count(CLUE_BLOCKED),
            "evidence_total": len(evidences),
            "zero_growth_rounds": state.get("zero_growth_rounds", 0),
            "rounds_elapsed": state.get("rounds", 0),
        }
        clue_titles = [
            {
                "clue_id": c["id"],
                "title": _read_clue_query(deps.run_root, c["id"]),
                "status": c.get("status"),
                "depth": c.get("depth"),
            }
            for c in done_clues
        ]
        recent_claims = [{"claim": ev["claim"], "clue_id": ev.get("clue_id")} for ev in evidences]
        recent_rounds = state.get("rounds", 0)
        input_payload = {
            "question": question,
            "board_stats": board_stats,
            "clue_titles": clue_titles,
            "recent_claims": recent_claims,
        }
        corpus = ARBITER_PROMPT.format(
            question=question,
            board_stats=json.dumps(board_stats, ensure_ascii=False),
            clue_titles=", ".join(t["title"] for t in clue_titles) or "（无）",
            recent_claims=", ".join(c["claim"] for c in recent_claims) or "（无）",
            recent_rounds=recent_rounds,
        )

        write_json_durable(_debate_input_path(deps.run_root, "arbiter"), input_payload)
        prompt_path = _debate_prompt_path(deps.run_root, "arbiter")
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(corpus, encoding="utf-8")

        run_id = debate_run_id(deps.thread_id, "arbiter")
        spec = AgentRunSpec(
            prompt="",
            role=deps.arbiter_role,
            input_path=str(_debate_input_path(deps.run_root, "arbiter")),
            prompt_file=str(prompt_path),
            structured=True,
            write=False,
            timeout_seconds=deps.arbiter_timeout_seconds,
            labels={"dispatcher": DISPATCHER_LABEL, "research": deps.research_id},
        )
        ticket = deps.launcher.launch(spec, run_id)
        status = deps.launcher.wait(
            ticket,
            poll_interval=deps.poll_interval,
            deadline_seconds=deps.arbiter_timeout_seconds + 120,
        )

        if not (status.ok and status.result is not None):
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": f"debate/arbiter run {run_id} 结束于 {status.state}",
            }
        try:
            declared = parse_envelope(status.result)
            verdict = declared.get("verdict") if isinstance(declared, dict) else None
            rationale = declared.get("rationale") if isinstance(declared, dict) else None
        except Exception as exc:
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": f"debate/arbiter 信封不可解析：{exc}",
            }
        if verdict not in {"enough", "continue"} or not isinstance(rationale, str):
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": "debate/arbiter 未返回合法 {verdict, rationale}",
            }

        # arbiter 产出逐字落盘（dr-arbiter.result.v1）。verdict=continue 仅记录并
        # 响亮落盘（进 report + events），不改动 converge 的路由语义（硬线）。
        arbiter_path = _debate_output_path(deps.run_root, "arbiter")
        arbiter_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_durable(arbiter_path, {"verdict": verdict, "rationale": rationale})
        _observe(deps, {"event": "debate", "role": "arbiter", "run_id": run_id, "verdict": verdict})
        return {}

    return arbiter


def _extract_judge_disagreements(body: str) -> tuple[list[str], list[str]]:
    """从 judge 正文逐字拆出「已裁定分歧」行（RULE:）与「开放分歧」行
    （OPEN DISAGREEMENT:）。

    R4 判据 ② 依赖「逐字保留」：这些行按原文原样进入 report.md，不调和 / 不删 /
    不改写为共识。只做行级拆分，不做语义推断（节点零 LLM）。
    """
    ruled: list[str] = []
    open_items: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if OPEN_DISAGREEMENT_MARKER in stripped:
            open_items.append(line)
        elif stripped.startswith(RULE_MARKER):
            ruled.append(line)
    return ruled, open_items


def _assemble_report(
    question: str,
    judge_body: str,
    ruled: list[str],
    open_items: list[str],
    arbiter: dict[str, Any],
) -> str:
    """从 judge / arbiter 产出组装 report.md（零 LLM 脚本节点）。

    「分歧裁定」段必须含三小节；judge 零条未决分歧时「开放分歧」段显式写
    「本轮无未决分歧」——该段不得省略。
    """
    lines: list[str] = [f"# {question}", ""]
    lines.append(judge_body if judge_body.strip() else "（judge 未产出正文）")
    lines.extend(["", "## 分歧裁定", "", "### 已裁定分歧"])
    if ruled:
        lines.extend(ruled)
    else:
        lines.append("本轮无已裁定分歧")
    lines.extend(["", "### 开放分歧"])
    if open_items:
        lines.extend(open_items)
    else:
        lines.append("本轮无未决分歧")
    lines.extend(["", "### arbiter 裁决"])
    lines.append(f"- verdict: {arbiter['verdict']}")
    lines.append(f"- rationale: {arbiter['rationale']}")
    return "\n".join(lines)


def _debate_report_node(deps: ResearchDeps):
    def debate_report(state: ResearchState) -> ResearchState:
        """报告组装节点：零 LLM，纯落盘与发布（规格第 5 条 script 节点）。

        report.md 由 judge 产出组装（附 arbiter 的裁决记录），并发布到
        research.doc.v2（leaf，doc_kind=report，digest = 正文内容寻址）。
        """
        judge_path = _debate_output_path(deps.run_root, "judge")
        arbiter_path = _debate_output_path(deps.run_root, "arbiter")
        if not (judge_path.is_file() and arbiter_path.is_file()):
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": "debate_report 缺 judge/arbiter 产出",
            }
        judge_body = judge_path.read_text(encoding="utf-8")
        arbiter = json.loads(arbiter_path.read_text(encoding="utf-8"))
        ruled, open_items = _extract_judge_disagreements(judge_body)
        report = _assemble_report(
            state.get("question") or deps.question,
            judge_body,
            ruled,
            open_items,
            arbiter,
        )
        (deps.run_root / REPORT_FILE).write_text(report, encoding="utf-8")
        # R5：报告 doc 的发布移到 anchor_check 之后（报告头需先写 dr-anchor-rate，
        # 保证 R1 双源对账的「report doc body == 本地 report.md」不被打破）。
        # arbiter 的 verdict 响亮落 events（含 continue），供审计对账。
        _observe(
            deps,
            {
                "event": "debate_report",
                "open_disagreements": len(open_items),
                "verdict": arbiter.get("verdict"),
            },
        )
        return {}

    return debate_report


def _finalise_node(deps: ResearchDeps):
    def finalise(state: ResearchState) -> ResearchState:
        terminal = state.get("terminal")
        if not terminal:
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": "图到达 finalise 却没有 terminal",
            }
        if terminal != TERMINAL_FAULT and not (deps.run_root / REPORT_FILE).is_file():
            # 非 fault 却无 report.md = debate_report 静默缺失，fault 而不是假装成功。
            return {"terminal": TERMINAL_FAULT, "terminal_reason": "debate_report 未产出 report.md"}
        _observe(deps, {"event": "finalise", "terminal": terminal})
        return {}

    return finalise


def _anchor_check_node(deps: ResearchDeps):
    def anchor_check(state: ResearchState) -> ResearchState:
        """R5：锚点核验节点（零 LLM、零外呼 IO 的纯脚本节点）。

        finalise 之后逐条把 report.md 的 ``[anchor: …]`` 引用核验回 evidence.jsonl，
        产出 ``run_root/anchor-check.json``（claims + summary），并在 report.md
        报告头写 ``dr-anchor-rate``。核验率 ≤90% 是**软闸门**：响亮记录（报告头 +
        anchor-check.json + events），不判红、不改 converge 路由（本节点不碰
        converge）。state 只装 id 与计数，正文/verdict 一律落文件，不进 checkpoint。
        """
        run_root = deps.run_root
        report_path = run_root / REPORT_FILE
        if not report_path.is_file():
            # fault 路径（无报告可核验）：保持原样，不写任何产物。
            return {}
        result = check_run(run_root)
        summary = result["summary"]
        rate = summary["rate"]
        met = rate > SOFT_GATE_RATE
        # 报告头已写 dr-anchor-rate：report doc（research.doc.v2）在此时发布，保证
        # R1 双源对账「report doc body == 本地 report.md」一致。
        _publish_doc(deps, body=report_path.read_text(encoding="utf-8"))
        # 软闸门响亮记录：rate ≤90% 只记录未达标（met=false），不改路由。
        _observe(
            deps,
            {
                "event": "anchor_check",
                "rate": rate,
                "met": met,
                "total": summary["total"],
                "ok": summary["ok"],
                "failed": summary["failed"],
                "unanchored": summary["unanchored"],
                "sums_ok": summary["sums_ok"],
            },
        )
        return {}

    return anchor_check


def _after_seed(state: ResearchState) -> str:
    return "finalise" if state.get("terminal") else "dispatch"


def _after_converge(state: ResearchState) -> str:
    return "debate_advocate" if state.get("terminal") else "dispatch"


def _after_debate_advocate(state: ResearchState) -> str:
    return "finalise" if state.get("terminal") == TERMINAL_FAULT else "debate_opponent"


def _after_debate_opponent(state: ResearchState) -> str:
    return "finalise" if state.get("terminal") == TERMINAL_FAULT else "debate_judge"


def _after_debate_judge(state: ResearchState) -> str:
    return "finalise" if state.get("terminal") == TERMINAL_FAULT else "debate_arbiter"


def _after_debate_arbiter(state: ResearchState) -> str:
    return "finalise" if state.get("terminal") == TERMINAL_FAULT else "debate_report"


def build_research_graph(deps: ResearchDeps) -> StateGraph:
    """装配 research 图。节点全部闭包在 deps 上，便于测试注入 fake。

    R4：converge 判终后进入对抗裁决子图 `debate`（advocate → opponent → judge →
    arbiter → debate_report），任意一段 run 失败即 fault，绝不再往下走。arbiter
    的 verdict=continue 只记录落盘，不改动 converge 的路由语义。
    """
    graph: StateGraph = StateGraph(ResearchState)
    graph.add_node("seed", _seed_node(deps))
    graph.add_node("dispatch", _dispatch_node(deps))
    graph.add_node("collect", _collect_node(deps))
    graph.add_node("harvest", _harvest_node(deps))
    graph.add_node("converge", _converge_node(deps))
    graph.add_node(
        "debate_advocate", _debater_node(deps, role="advocate", role_value=deps.advocate_role)
    )
    graph.add_node(
        "debate_opponent", _debater_node(deps, role="opponent", role_value=deps.opponent_role)
    )
    graph.add_node("debate_judge", _debater_node(deps, role="judge", role_value=deps.judge_role))
    graph.add_node("debate_arbiter", _arbiter_node(deps))
    graph.add_node("debate_report", _debate_report_node(deps))
    graph.add_node("finalise", _finalise_node(deps))
    graph.add_node("anchor_check", _anchor_check_node(deps))

    graph.add_edge(START, "seed")
    graph.add_conditional_edges("seed", _after_seed, {"dispatch", "finalise"})
    graph.add_conditional_edges("dispatch", _after_dispatch, {"collect", "converge"})
    graph.add_edge("collect", "harvest")
    graph.add_edge("harvest", "converge")
    graph.add_conditional_edges("converge", _after_converge, {"dispatch", "debate_advocate"})
    graph.add_conditional_edges(
        "debate_advocate", _after_debate_advocate, {"debate_opponent", "finalise"}
    )
    graph.add_conditional_edges(
        "debate_opponent", _after_debate_opponent, {"debate_judge", "finalise"}
    )
    graph.add_conditional_edges("debate_judge", _after_debate_judge, {"debate_arbiter", "finalise"})
    graph.add_conditional_edges(
        "debate_arbiter", _after_debate_arbiter, {"debate_report", "finalise"}
    )
    graph.add_edge("debate_report", "finalise")
    graph.add_edge("finalise", "anchor_check")
    graph.add_edge("anchor_check", END)
    return graph


__all__ = [
    "ADVOCATE_FILE",
    "ADVOCATE_ROLE",
    "ARBITER_FILE",
    "ARBITER_ROLE",
    "CLUE_BLOCKED",
    "CLUE_DISPATCHED",
    "CLUE_DONE",
    "CLUE_OPEN",
    "CONVERGE_CONTINUE",
    "DEBATE_DIR",
    "DEBATE_ROLES",
    "DEFAULT_SOURCE",
    "DEFAULT_SOURCES",
    "DISPATCHER_LABEL",
    "EVIDENCE_FILE",
    "JUDGE_FILE",
    "JUDGE_ROLE",
    "MAX_RETRIES",
    "OPEN_DISAGREEMENT_MARKER",
    "OPPONENT_FILE",
    "OPPONENT_ROLE",
    "REPORT_FILE",
    "RULE_MARKER",
    "SEED_FILE",
    "SOURCE_ROLE",
    "TERMINAL_CAPPED",
    "TERMINAL_CONVERGED",
    "TERMINAL_FAULT",
    "TERMINAL_PARTIAL",
    "WORKER_ROLE",
    "ResearchBounds",
    "ResearchDeps",
    "ResearchState",
    "_anchor_check_node",
    "_assemble_report",
    "_extract_judge_disagreements",
    "build_research_graph",
    "converge",
    "debate_run_id",
    "derive_clue_id",
    "derive_research_id",
    "derive_run_instance",
    "initial_state",
    "worker_run_id",
]
