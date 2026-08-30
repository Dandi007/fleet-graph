"""deep-research 串行闭环图：一张 submit 驱动的 L2 业务图。

一个 research 工单走完
`seed -> {dispatch -> collect -> harvest -> converge} 循环 -> synthesis -> finalise`，
在 run root 下产出 `report.md` 与 `result.json`。串行 W=1：每个循环只派一个 clue
（`worker/clue_id`），worker 完成或失败后才进入下一轮。

节点纯度（规格第 5 条）：
- **script 节点**（dispatch / collect / harvest / converge / finalise）零 LLM 调用，
  只做计数、落盘与路由——它们不读含义、不写代理结论。
- **LLM 节点**只有 `seed`（TextNode，纯文本进出，产出初始 clue 列表）与
  `synthesis`（AgentRunLauncher，一次性 structured run，`write=False`，语料经
  `--prompt-file` 文件投递）。

state 只装 id 与计数（规格第 7 条）：clue 板（id/status/depth/retry）与
coverage/zero_growth_rounds/rounds。findings 逐条 append 到 `evidence.jsonl`、
报告正文落 `report.md`——正文永不进 checkpoint，kill-restart 后重放不会重复大段文本。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from fleet_graph.executors.agent_run import AgentRunSpec, RunWaitTimeout, derive_run_id
from fleet_graph.executors.text_node import TextSpec
from fleet_graph.graphs.adapters import parse_envelope
from fleet_graph.research_bus import (
    DOC_KIND_REPORT,
    PIPELINE_STATUS_TO_PROTOCOL,
    RESEARCH_CLUE_KIND,
    RESEARCH_DOC_KIND,
    RESEARCH_EVIDENCE_KIND,
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
    publish_best_effort,
)
from fleet_graph.state.run_artifacts import iso, write_json_durable

# role 名（规格第 8 条）：agent-runtime 侧 role 由另单交付，本单测试全部用 fake
# launcher/text node，不依赖真实 role 存在。R2 起 dispatch 按 clue 的 source 路由到
# SOURCE_ROLE 矩阵（6 个 dr-worker 角色），不再用单一 worker 角色。
WORKER_ROLE = "research_worker_local"
SYNTHESIS_ROLE = "research_synth"

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
# - synth input   research-synth.input.v1：{question[, clue_ids, corpus_note]}
# - synth result  research-synth.result.v1：{report_markdown, coverage_summary,
#   unresolved}
# role 声明 protocol.input 后 agent-run 强制要求 --input，缺了直接 CONTRACT_ERROR，
# 所以 dispatch/synthesis 必须为每个 run 落 input 文件并传 spec.input_path。

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
# runner 侧写；这里的是节点侧产物。
EVIDENCE_FILE = "evidence.jsonl"
REPORT_FILE = "report.md"
SEED_FILE = "seed.json"
SYNTHESIS_FILE = "synthesis.json"

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


def derive_research_id(question: str) -> str:
    """`research_id` 由问题文本内容寻址派生：sha256 前 12 hex，前缀 `r-`（规格第 2 条）。

    内容寻址意味着同一问题恒得同一 id，thread identity 才能跨重启稳定。
    """
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
    return f"r-{digest}"


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


def synthesis_run_id(thread_id: str, attempt: int = 1) -> str:
    """synthesis 的派生 run id：`derive_run_id(thread_id, "synthesis", attempt)`。"""
    return derive_run_id(thread_id, "synthesis", attempt)


@dataclass(frozen=True)
class ResearchBounds:
    """收敛判定的纯计数边界（规格第 6 条）。无时间、无 LLM、无 IO。

    - max_clues / max_depth：clue 总数/深度触顶 -> capped。
    - zero_growth_rounds：coverage 零增长连续 N 轮 -> converged。
    - max_rounds：轮次预算触顶 -> capped（预算截断尚未收敛的研究）。
    """

    max_clues: int = 12
    max_depth: int = 6
    zero_growth_rounds: int = 3
    max_rounds: int = 24


class ResearchState(TypedDict, total=False):
    """state 只装 id 与计数（规格第 7 条）。findings/report 正文一律落 run root 文件。"""

    research_id: str
    question: str
    generation: int
    #: clue 板：每项只含 id/status/depth/retry，不含线索正文（正文在 clues/<id>.json）。
    clues: list[dict[str, Any]]
    rounds: int
    #: coverage = done clue 计数。零增长连续 N 轮即 converged。
    coverage: int
    zero_growth_rounds: int
    #: 本轮已 dispatch 的 clue id（collect/harvest 据此定位；W=1 一次只一个）。
    pending_clue_id: str
    terminal: str
    terminal_reason: str
    #: clue 实体在 bus 上的版本头（message_id），维护 ``research.clue.v2`` 的
    #: ``supersedes`` 版本链。只装 id（规格第 7 条：state 只装 id 与计数）。
    clue_heads: dict[str, str]


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
    synthesis_role: str = SYNTHESIS_ROLE
    worker_timeout_seconds: int = 900
    synthesis_timeout_seconds: int = 900
    poll_interval: float = 2.0
    observe: Any = None
    clock: Any = None
    #: 发布端口：协议上等价 ``BusClient.publish``（含 entity_id / supersedes /
    #: idempotency_key）。生产装配真实 BusClient；测试注入 fake transport。
    #: None = 不发布（best-effort 降级，同 observe 缺失时一样静默）。
    publisher: Any = None

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


def _synthesis_input_path(run_root: Path) -> Path:
    return run_root / "inputs" / "synthesis.json"


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
    （不 str() 压平——synthesis 语料与人工复核都要 locator/quote 原文）。"""
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
    supersedes: str | None = None,
    run_id: str | None = None,
    sources: list[str] | None = None,
) -> str | None:
    """发布一条 clue 状态迁移到 ``research:{research_id}.index``（research.clue.v2）。

    root 实体：稳定 entity_id = clue_id，版本链经 ``supersedes``。幂等 key 由
    run/clue/status/retry 内容寻址派生，kill-restart 重派同 key 不产生重复实体。
    best-effort：失败只降级记录，返回 message_id 或 None。
    """
    protocol_status = PIPELINE_STATUS_TO_PROTOCOL[status]
    payload = clue_payload(
        text=text,
        status=protocol_status,
        depth=depth,
        sources=sources,
        run_id=run_id,
    )
    key = clue_idempotency_key(deps.research_id, clue_id, protocol_status, retry)
    return publish_best_effort(
        deps.publisher,
        channel_id=clue_index_channel(deps.research_id),
        kind=RESEARCH_CLUE_KIND,
        payload=payload,
        idempotency_key=key,
        entity_id=clue_id,
        supersedes=supersedes,
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
    )


def _publish_doc(deps: ResearchDeps, *, body: str) -> str | None:
    """发布 synthesis 报告到 ``research:{research_id}.docs``（research.doc.v2）。

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
    )


def _head(clue_heads: dict[str, str] | None, clue_id: str) -> str | None:
    """某 clue 实体当前的 bus 版本头 message_id（版本链 supersedes 用）。"""
    return (clue_heads or {}).get(clue_id)


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

        _observe(deps, {"event": "seed", "clues": len(clues)})
        return {
            "clues": clues,
            "clue_heads": heads,
            "coverage": 0,
            "zero_growth_rounds": 0,
            "rounds": 0,
        }

    return seed


def _dispatch_node(deps: ResearchDeps):
    def dispatch(state: ResearchState) -> ResearchState:
        clues = state.get("clues", [])
        open_clues = [c for c in clues if c["status"] == CLUE_OPEN]
        if not open_clues:
            # 无 open clue：让 converge 判定（converged / partial）。
            return {"pending_clue_id": ""}

        clue = open_clues[0]  # W=1：每轮只取一个。
        clue_id = clue["id"]
        # R2：clue 板每项带 source；未知 / 缺失回填默认源（clue 级降级，绝不 fault）。
        source = clue.get("source") or deps.default_source
        if source not in SOURCE_ROLE:
            source = deps.default_source
        query = _read_clue_query(deps.run_root, clue_id)
        prompt = WORKER_PROMPT.format(question=state.get("question") or deps.question, clue=query)
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
        heads = dict(state.get("clue_heads", {}))
        run_id = worker_run_id(deps.thread_id, clue_id, clue["retry"])
        mid = _publish_clue(
            deps,
            clue_id=clue_id,
            text=query,
            status=CLUE_DISPATCHED,
            depth=clue["depth"],
            retry=clue["retry"],
            supersedes=_head(heads, clue_id),
            run_id=run_id,
            sources=[source],
        )
        if mid:
            heads[clue_id] = mid
        return {
            "clues": _set_clue(clues, clue_id, status=CLUE_DISPATCHED),
            "clue_heads": heads,
            "pending_clue_id": clue_id,
        }

    return dispatch


def _collect_node(deps: ResearchDeps):
    def collect(state: ResearchState) -> ResearchState:
        clue_id = state.get("pending_clue_id", "")
        if not clue_id:
            return {}

        clues = state.get("clues", [])
        clue = _clue(clues, clue_id)
        if clue is None:
            # pending clue 不在板上 = 内部不一致，fault。
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": f"pending clue {clue_id} 不在 clue 板上",
            }

        run_id = worker_run_id(deps.thread_id, clue_id, clue["retry"])
        # R2：按 clue.source 路由到 SOURCE_ROLE 矩阵（纯常量，不做内联 if）。未知 /
        # 缺失 source 已在 dispatch 回填默认源，这里只防御性兜底，绝不 fault。
        source = clue.get("source") or deps.default_source
        role = SOURCE_ROLE.get(source, SOURCE_ROLE[deps.default_source])
        spec = AgentRunSpec(
            prompt="",
            role=role,
            input_path=str(_worker_input_path(deps.run_root, clue_id, clue["retry"])),
            prompt_file=str(_prompt_path(deps.run_root, clue_id)),
            structured=True,
            write=False,
            timeout_seconds=deps.worker_timeout_seconds,
            labels={"dispatcher": DISPATCHER_LABEL, "research": deps.research_id},
        )
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
            retry = clue["retry"] + 1
            new_status = CLUE_BLOCKED if retry >= MAX_RETRIES else CLUE_OPEN
            heads = dict(state.get("clue_heads", {}))
            query = _read_clue_query(deps.run_root, clue_id)
            mid = _publish_clue(
                deps,
                clue_id=clue_id,
                text=query,
                status=new_status,
                depth=clue["depth"],
                retry=retry,
                supersedes=_head(heads, clue_id),
                sources=[source],
            )
            if mid:
                heads[clue_id] = mid
            return {
                "clues": _set_clue(
                    clues,
                    clue_id,
                    retry=retry,
                    status=new_status,
                ),
                "clue_heads": heads,
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

        heads = dict(state.get("clue_heads", {}))
        if declared is not None:
            for evidence in declared["evidences"]:
                # 每条 evidence -> R1 evidence：evidence 已是 {claim, source, quote,
                # locator} 形状，原样 append 与发布（research.evidence.v2 leaf）。
                _append_evidence(deps.run_root, clue_id, clue["depth"], evidence, deps.now())
                _publish_evidence(deps, clue_id=clue_id, finding=evidence, depth=clue["depth"])
            new_clues = _set_clue(clues, clue_id, status=CLUE_DONE)
            query = _read_clue_query(deps.run_root, clue_id)
            mid = _publish_clue(
                deps,
                clue_id=clue_id,
                text=query,
                status=CLUE_DONE,
                depth=clue["depth"],
                retry=clue["retry"],
                supersedes=_head(heads, clue_id),
                sources=[source],
            )
            if mid:
                heads[clue_id] = mid
        else:
            retry = clue["retry"] + 1
            new_status = CLUE_BLOCKED if retry >= MAX_RETRIES else CLUE_OPEN
            new_clues = _set_clue(
                clues,
                clue_id,
                retry=retry,
                status=new_status,
            )
            query = _read_clue_query(deps.run_root, clue_id)
            mid = _publish_clue(
                deps,
                clue_id=clue_id,
                text=query,
                status=new_status,
                depth=clue["depth"],
                retry=retry,
                supersedes=_head(heads, clue_id),
                sources=[source],
            )
            if mid:
                heads[clue_id] = mid

        _observe(deps, {"event": "collect", "clue_id": clue_id, "run_id": run_id, "ok": status.ok})
        return {"clues": new_clues, "clue_heads": heads}

    return collect


def _harvest_node(deps: ResearchDeps):
    def harvest(state: ResearchState) -> ResearchState:
        clue_id = state.get("pending_clue_id", "")
        if not clue_id:
            return {}

        clues = state.get("clues", [])
        heads = dict(state.get("clue_heads", {}))
        clue = _clue(clues, clue_id)
        if clue is not None and clue["status"] == CLUE_DONE:
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

        coverage = _done_count(clues)
        zero_growth = (
            0 if coverage > state.get("coverage", 0) else state.get("zero_growth_rounds", 0) + 1
        )
        rounds = state.get("rounds", 0) + 1

        _observe(deps, {"event": "harvest", "rounds": rounds, "coverage": coverage})
        return {
            "clues": clues,
            "clue_heads": heads,
            "coverage": coverage,
            "zero_growth_rounds": zero_growth,
            "rounds": rounds,
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


def _synthesis_node(deps: ResearchDeps):
    def synthesis(state: ResearchState) -> ResearchState:
        question = state.get("question") or deps.question
        corpus = [question]
        evidence_path = deps.run_root / EVIDENCE_FILE
        if evidence_path.is_file():
            corpus.append(evidence_path.read_text(encoding="utf-8"))
        corpus_path = deps.run_root / "synthesis-prompt.md"
        corpus_path.parent.mkdir(parents=True, exist_ok=True)
        corpus_path.write_text("\n".join(corpus), encoding="utf-8")
        # research-synth.input.v1：--input 只携带题面 manifest（question + done
        # clue 清单）；语料本体照旧经 --prompt-file 投递。
        write_json_durable(
            _synthesis_input_path(deps.run_root),
            {
                "question": question,
                "clue_ids": [c["id"] for c in state.get("clues", []) if c["status"] == CLUE_DONE],
            },
        )

        run_id = synthesis_run_id(deps.thread_id)
        spec = AgentRunSpec(
            prompt="",
            role=deps.synthesis_role,
            input_path=str(_synthesis_input_path(deps.run_root)),
            prompt_file=str(corpus_path),
            structured=True,
            write=False,
            timeout_seconds=deps.synthesis_timeout_seconds,
            labels={"dispatcher": DISPATCHER_LABEL, "research": deps.research_id},
        )
        ticket = deps.launcher.launch(spec, run_id)
        status = deps.launcher.wait(
            ticket,
            poll_interval=deps.poll_interval,
            deadline_seconds=deps.synthesis_timeout_seconds + 120,
        )
        write_json_durable(deps.run_root / SYNTHESIS_FILE, status.result or {})

        if not (status.ok and status.result is not None):
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": f"synthesis run {run_id} 结束于 {status.state}",
            }
        try:
            declared = parse_envelope(status.result)
            # 契约字段是 report_markdown（research-synth.result.v1），不是旧设想的 report。
            report = declared.get("report_markdown") if isinstance(declared, dict) else None
        except Exception as exc:
            return {"terminal": TERMINAL_FAULT, "terminal_reason": f"synthesis 信封不可解析：{exc}"}
        if not isinstance(report, str) or not report:
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": "synthesis 未返回 report_markdown 正文",
            }

        (deps.run_root / REPORT_FILE).write_text(report, encoding="utf-8")
        # synthesis 报告 -> research.doc.v2（leaf，doc_kind=report，origin=research_id，
        # digest = 正文内容寻址）。本地镜像照旧写 report.md。
        _publish_doc(deps, body=report)
        _observe(deps, {"event": "synthesis", "run_id": run_id})
        return {}

    return synthesis


def _finalise_node(deps: ResearchDeps):
    def finalise(state: ResearchState) -> ResearchState:
        terminal = state.get("terminal")
        if not terminal:
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": "图到达 finalise 却没有 terminal",
            }
        if terminal != TERMINAL_FAULT and not (deps.run_root / REPORT_FILE).is_file():
            # 非 fault 却无 report.md = synthesis 静默缺失，fault 而不是假装成功。
            return {"terminal": TERMINAL_FAULT, "terminal_reason": "synthesis 未产出 report.md"}
        _observe(deps, {"event": "finalise", "terminal": terminal})
        return {}

    return finalise


def _after_seed(state: ResearchState) -> str:
    return "finalise" if state.get("terminal") else "dispatch"


def _after_dispatch(state: ResearchState) -> str:
    return "collect" if state.get("pending_clue_id") else "converge"


def _after_converge(state: ResearchState) -> str:
    return "synthesis" if state.get("terminal") else "dispatch"


def build_research_graph(deps: ResearchDeps) -> StateGraph:
    """装配 research 图。节点全部闭包在 deps 上，便于测试注入 fake。"""
    graph: StateGraph = StateGraph(ResearchState)
    graph.add_node("seed", _seed_node(deps))
    graph.add_node("dispatch", _dispatch_node(deps))
    graph.add_node("collect", _collect_node(deps))
    graph.add_node("harvest", _harvest_node(deps))
    graph.add_node("converge", _converge_node(deps))
    graph.add_node("synthesis", _synthesis_node(deps))
    graph.add_node("finalise", _finalise_node(deps))

    graph.add_edge(START, "seed")
    graph.add_conditional_edges("seed", _after_seed, {"dispatch", "finalise"})
    graph.add_conditional_edges("dispatch", _after_dispatch, {"collect", "converge"})
    graph.add_edge("collect", "harvest")
    graph.add_edge("harvest", "converge")
    graph.add_conditional_edges("converge", _after_converge, {"dispatch", "synthesis"})
    graph.add_edge("synthesis", "finalise")
    graph.add_edge("finalise", END)
    return graph


__all__ = [
    "CLUE_BLOCKED",
    "CLUE_DISPATCHED",
    "CLUE_DONE",
    "CLUE_OPEN",
    "CONVERGE_CONTINUE",
    "DEFAULT_SOURCE",
    "DEFAULT_SOURCES",
    "DISPATCHER_LABEL",
    "EVIDENCE_FILE",
    "MAX_RETRIES",
    "REPORT_FILE",
    "SEED_FILE",
    "SOURCE_ROLE",
    "SYNTHESIS_FILE",
    "TERMINAL_CAPPED",
    "TERMINAL_CONVERGED",
    "TERMINAL_FAULT",
    "TERMINAL_PARTIAL",
    "WORKER_ROLE",
    "ResearchBounds",
    "ResearchDeps",
    "ResearchState",
    "build_research_graph",
    "converge",
    "derive_clue_id",
    "derive_research_id",
    "initial_state",
    "synthesis_run_id",
    "worker_run_id",
]
