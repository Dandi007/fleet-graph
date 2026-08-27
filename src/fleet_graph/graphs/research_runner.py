"""装配并运行一个 research 工单。

与 `graphs/runner.py` 对 ronin line 的拆分一致：这里把 parts 接成图，跑通到终态。
`research_pipeline.py` 保持可测（fake text node / fake launcher 注入），真实协作方
（TextNode 走网关、AgentRunLauncher 派 agent-run）只在这里出现。

run artifacts 走 dd 形状（规格第 4 条）：`events.jsonl` + `result.json` 由 runner
侧写；不使用 `state/run_artifacts.py` 的 RunArtifacts/heartbeat（其 phase 封闭枚举
不含 research 阶段）。checkpoint 用 SqliteSaver 落 `run_root/checkpoint.sqlite3`，
resume 语义参照 `graphs/runner.py` 的 `resume_start`：snapshot.next 非空 -> invoke(None)
精确续跑，绝不重放。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.executors.agent_run import AgentRunLauncher
from fleet_graph.executors.text_node import TextNode
from fleet_graph.graphs.research_pipeline import (
    REPORT_FILE,
    TERMINAL_FAULT,
    ResearchBounds,
    ResearchDeps,
    build_research_graph,
    derive_research_id,
    initial_state,
)
from fleet_graph.state.run_artifacts import iso, write_json_durable

EVENTS = "events.jsonl"
RESULT = "result.json"


@dataclass
class ResearchConfig:
    """一个 research 工单，及它的 run 落点。形状参照 runner.py 的 LineConfig。"""

    question: str
    run_root: Path
    generation: int = 1
    max_clues: int = 12
    max_depth: int = 6
    zero_growth_rounds: int = 3
    max_rounds: int = 24
    #: None 表示持久化：run_root / "checkpoint.sqlite3"。":memory:" 留给一次性测试。
    checkpoint_path: str | None = None
    #: 测试接缝：指向 fake binary。生产保持 None，用 DEFAULT_AGENT_RUN_BIN。
    agent_run_bin: str | None = None
    seed_model: str = "deepseek-v4-flash"

    @property
    def research_id(self) -> str:
        return derive_research_id(self.question)

    @property
    def thread_id(self) -> str:
        """跨重启稳定的线程身份（规格第 2 条）：`{research_id}:g{generation}`。

        同 runner.py 的 LineConfig.thread_id 形状。任何随机量都不得进入此串，
        否则 derived run id 会随重启漂移，re-adopt 失效。
        """
        return f"{self.research_id}:g{self.generation}"

    @property
    def resolved_checkpoint_path(self) -> str:
        return self.checkpoint_path or str(self.run_root / "checkpoint.sqlite3")


def build_research(
    config: ResearchConfig,
    *,
    text_node: Any = None,
    launcher: Any = None,
    observe: Any = None,
) -> tuple[Any, ResearchDeps]:
    """接线一个 research 工单。返回编译前的图与 deps。"""
    run_root = config.run_root
    launcher_kwargs: dict[str, Any] = {"state_root": str(run_root / "agent-runs")}
    if config.agent_run_bin:
        launcher_kwargs["bin_path"] = config.agent_run_bin
    launcher = launcher or AgentRunLauncher(**launcher_kwargs)
    text_node = text_node or TextNode()

    deps = ResearchDeps(
        question=config.question,
        research_id=config.research_id,
        thread_id=config.thread_id,
        run_root=run_root,
        text_node=text_node,
        launcher=launcher,
        bounds=ResearchBounds(
            max_clues=config.max_clues,
            max_depth=config.max_depth,
            zero_growth_rounds=config.zero_growth_rounds,
            max_rounds=config.max_rounds,
        ),
        seed_model=config.seed_model,
        observe=observe,
    )
    return build_research_graph(deps), deps


def resume_start(
    compiled: Any, invoke_config: dict[str, Any], config: ResearchConfig
) -> dict[str, Any] | None:
    """续跑或开新的一单，语义参照 runner.py 的 resume_start。

    - snapshot.next 非空 -> None：在 pending 节点处精确续跑（invoke(None)），绝不重放。
    - 否则 -> initial_state：全新线程从 seed 开始。
    """
    snapshot = compiled.get_state(invoke_config)
    if snapshot.next:
        return None
    return initial_state(config.research_id, config.question, config.generation)


def run_research(
    config: ResearchConfig,
    *,
    text_node: Any = None,
    launcher: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """跑一个 research 工单到终态，写 events.jsonl 与 result.json 后返回摘要。

    terminal ∈ {converged, capped, partial} 才算跑通；fault（seed/synthesis 故障、
    意外异常）非零退出由 CLI 侧决定。单 clue 失败绝不 fault 整图——那是图内的
    retry/block 状态机，不是这里的异常路径。
    """
    now = clock or time.time
    run_root = config.run_root
    run_root.mkdir(parents=True, exist_ok=True)
    events_path = run_root / EVENTS

    def persist_event(entry: dict[str, Any]) -> None:
        try:
            with events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": iso(now()), **entry}, ensure_ascii=False) + "\n")
                handle.flush()
        except OSError:
            # 可观测性不能拖垮它观测的工作。
            pass

    graph, _deps = build_research(
        config, text_node=text_node, launcher=launcher, observe=persist_event
    )
    invoke_config: dict[str, Any] = {
        "configurable": {"thread_id": config.thread_id},
        # bounds 才是真上限，这只是失控兜底。
        "recursion_limit": config.max_rounds * 6 + 20,
    }

    terminal = TERMINAL_FAULT
    reason = ""
    rounds = 0
    try:
        with SqliteSaver.from_conn_string(config.resolved_checkpoint_path) as saver:
            compiled = graph.compile(checkpointer=saver)
            state = compiled.invoke(
                resume_start(compiled, invoke_config, config), config=invoke_config
            )
        terminal = state.get("terminal", TERMINAL_FAULT)
        reason = state.get("terminal_reason", "")
        rounds = state.get("rounds", 0)
    except Exception as exc:
        terminal = TERMINAL_FAULT
        reason = f"{type(exc).__name__}: {exc}"

    result = {
        "research_id": config.research_id,
        "question": config.question,
        "generation": config.generation,
        "terminal": terminal,
        "terminal_reason": reason,
        "rounds": rounds,
        "report": str(run_root / REPORT_FILE),
        "run_root": str(run_root),
    }
    write_json_durable(run_root / RESULT, {**result, "written_at": iso(now())})
    return result


__all__ = ["EVENTS", "RESULT", "ResearchConfig", "build_research", "resume_start", "run_research"]
