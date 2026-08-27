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
from fleet_graph.state.run_artifacts import iso, write_json_durable

# role 名（规格第 8 条）：agent-runtime 侧 role 由另单交付，本单测试全部用 fake
# launcher/text node，不依赖真实 role 存在。
WORKER_ROLE = "research_worker_local"
SYNTHESIS_ROLE = "research_synth"

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
    "strings only, no prose, no markdown fences."
)
SEED_PROMPT = "为研究问题生成初始调查线索（仅返回 JSON 数组）：{question}"

WORKER_SYSTEM = (
    "You investigate one clue of a deep-research question. Return a JSON object "
    'with {"findings": [...], "new_clues": [...]}.'
)
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


def derive_clue_id(query: str) -> str:
    """clue id 同样内容寻址派生（`c-` + sha256 前 12 hex）。

    同一个线索文本永远映射到同一个 id，harvest 据此去重，避免同一子线索反复入板。
    """
    digest = hashlib.sha256(query.strip().encode("utf-8")).hexdigest()[:12]
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
    worker_role: str = WORKER_ROLE
    synthesis_role: str = SYNTHESIS_ROLE
    worker_timeout_seconds: int = 900
    synthesis_timeout_seconds: int = 900
    poll_interval: float = 2.0
    observe: Any = None
    clock: Any = None

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


def _write_clue_file(run_root: Path, clue_id: str, query: str, *, depth: int) -> None:
    # 线索正文落在文件里而不是 state 里：state 只装 id/status/depth/retry（规格第 7 条）。
    write_json_durable(
        _clue_file_path(run_root, clue_id), {"id": clue_id, "query": query, "depth": depth}
    )


def _read_clue_query(run_root: Path, clue_id: str) -> str:
    return json.loads(_clue_file_path(run_root, clue_id).read_text(encoding="utf-8"))["query"]


def _append_evidence(run_root: Path, clue_id: str, depth: int, finding: str, now: float) -> None:
    """逐条 append 一条 evidence（规格第 7 条）：findings 只落盘，不进 state。"""
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


# --- 节点 -------------------------------------------------------------------


def _seed_node(deps: ResearchDeps):
    def seed(state: ResearchState) -> ResearchState:
        question = state.get("question") or deps.question
        result = deps.text_node.complete(
            TextSpec(model=deps.seed_model, system=SEED_SYSTEM, max_tokens=2048),
            SEED_PROMPT.format(question=question),
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
        if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": "seed 未返回 clue 字符串数组",
            }

        clues: list[dict[str, Any]] = []
        for query in queries:
            clue_id = derive_clue_id(query)
            if _clue(clues, clue_id) is not None:
                # 内容寻址 id 去重：重复线索只入板一次。
                continue
            _write_clue_file(deps.run_root, clue_id, query, depth=0)
            clues.append({"id": clue_id, "status": CLUE_OPEN, "depth": 0, "retry": 0})

        _observe(deps, {"event": "seed", "clues": len(clues)})
        return {"clues": clues, "coverage": 0, "zero_growth_rounds": 0, "rounds": 0}

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
        query = _read_clue_query(deps.run_root, clue_id)
        prompt = WORKER_PROMPT.format(question=state.get("question") or deps.question, clue=query)
        prompt_path = _prompt_path(deps.run_root, clue_id)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")

        _observe(deps, {"event": "dispatch", "clue_id": clue_id, "retry": clue["retry"]})
        return {
            "clues": _set_clue(clues, clue_id, status=CLUE_DISPATCHED),
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
        spec = AgentRunSpec(
            prompt="",
            role=deps.worker_role,
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
            return {
                "clues": _set_clue(
                    clues,
                    clue_id,
                    retry=retry,
                    status=CLUE_BLOCKED if retry >= MAX_RETRIES else CLUE_OPEN,
                )
            }
        # run 结果落盘（harvest 据此提取新线索，resume 后也能重读）。
        write_json_durable(_result_path(deps.run_root, clue_id), status.result or {})

        declared: dict[str, Any] | None = None
        if status.ok and status.result is not None:
            try:
                parsed = parse_envelope(status.result)
                if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
                    declared = parsed
            except Exception:
                # 信封解析失败按 clue 失败处理（retry/block），绝不 fault 整图。
                declared = None

        if declared is not None:
            for finding in declared["findings"]:
                _append_evidence(deps.run_root, clue_id, clue["depth"], str(finding), deps.now())
            new_clues = _set_clue(clues, clue_id, status=CLUE_DONE)
        else:
            retry = clue["retry"] + 1
            new_clues = _set_clue(
                clues,
                clue_id,
                retry=retry,
                status=CLUE_BLOCKED if retry >= MAX_RETRIES else CLUE_OPEN,
            )

        _observe(deps, {"event": "collect", "clue_id": clue_id, "run_id": run_id, "ok": status.ok})
        return {"clues": new_clues}

    return collect


def _harvest_node(deps: ResearchDeps):
    def harvest(state: ResearchState) -> ResearchState:
        clue_id = state.get("pending_clue_id", "")
        if not clue_id:
            return {}

        clues = state.get("clues", [])
        clue = _clue(clues, clue_id)
        if clue is not None and clue["status"] == CLUE_DONE:
            raw = json.loads(_result_path(deps.run_root, clue_id).read_text(encoding="utf-8"))
            try:
                structured = parse_envelope(raw)
            except Exception:
                # collect 已判定 done 说明信封本可解析；这里解不出就按无子线索继续，
                # 绝不 fault 整图（与 collect 的降级同义，保续跑健壮性）。
                structured = {}
            new_clues = structured.get("new_clues", []) if isinstance(structured, dict) else []
            for text in new_clues:
                if not isinstance(text, str) or not text.strip():
                    continue
                child_id = derive_clue_id(text.strip())
                if _clue(clues, child_id) is not None:
                    continue
                depth = clue["depth"] + 1
                _write_clue_file(deps.run_root, child_id, text.strip(), depth=depth)
                clues = [
                    *clues,
                    {"id": child_id, "status": CLUE_OPEN, "depth": depth, "retry": 0},
                ]

        coverage = _done_count(clues)
        zero_growth = (
            0 if coverage > state.get("coverage", 0) else state.get("zero_growth_rounds", 0) + 1
        )
        rounds = state.get("rounds", 0) + 1

        _observe(deps, {"event": "harvest", "rounds": rounds, "coverage": coverage})
        return {
            "clues": clues,
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

        run_id = synthesis_run_id(deps.thread_id)
        spec = AgentRunSpec(
            prompt="",
            role=deps.synthesis_role,
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
            report = declared.get("report") if isinstance(declared, dict) else None
        except Exception as exc:
            return {"terminal": TERMINAL_FAULT, "terminal_reason": f"synthesis 信封不可解析：{exc}"}
        if not isinstance(report, str):
            return {"terminal": TERMINAL_FAULT, "terminal_reason": "synthesis 未返回 report 正文"}

        (deps.run_root / REPORT_FILE).write_text(report, encoding="utf-8")
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
    "DISPATCHER_LABEL",
    "EVIDENCE_FILE",
    "MAX_RETRIES",
    "REPORT_FILE",
    "SEED_FILE",
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
