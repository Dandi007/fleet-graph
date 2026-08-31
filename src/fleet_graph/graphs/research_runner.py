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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.executors.agent_run import AgentRunLauncher
from fleet_graph.executors.text_node import TextNode
from fleet_graph.graphs.research_pipeline import (
    DEFAULT_SOURCES,
    REPORT_FILE,
    TERMINAL_FAULT,
    ResearchBounds,
    ResearchDeps,
    build_research_graph,
    derive_research_id,
    derive_run_instance,
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
    #: R3 并发度：dispatch 每个 wave 至多并发派发这么多 open clue（缺省 4，W=4）。
    #: 只影响「同 wave 派几个」，不影响 clue id / input / run id 的派生。
    concurrency: int = 4
    #: None 表示持久化：run_root / "checkpoint.sqlite3"。":memory:" 留给一次性测试。
    checkpoint_path: str | None = None
    #: 测试接缝：指向 fake binary。生产保持 None，用 DEFAULT_AGENT_RUN_BIN。
    agent_run_bin: str | None = None
    seed_model: str = "deepseek-v4-flash"
    #: 多源矩阵词汇（R2，规格第 8 条）：默认固定顺序，取首元素为默认源。空列表时
    #: 回退到 DEFAULT_SOURCES。
    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    #: 显式 run 实例分量（R3-fix，规格第 1 条）：缺省 None = 由 run_root 内容寻址派生
    #: （`derive_run_instance`，稳定非随机）。显式给定用于跨不同 run_root 也需保持
    #: 同一身份的边界情况（rare），值必须由调用方保证稳定，不得掺 uuid4/时间戳。
    instance: str | None = None

    @property
    def default_source(self) -> str:
        return self.sources[0] if self.sources else DEFAULT_SOURCES[0]

    @property
    def research_id(self) -> str:
        return derive_research_id(self.question)

    @property
    def run_instance(self) -> str:
        """稳定的 run 实例分量（R3-fix，规格第 1 条）。

        缺省由 run_root 内容寻址派生（同 run_root 恒同、不同 run_root 恒不同），
        显式 ``instance`` 优先。稳定非随机，kill-restart 不漂移。
        """
        return self.instance if self.instance is not None else derive_run_instance(self.run_root)

    @property
    def thread_id(self) -> str:
        """跨重启稳定的线程身份（规格第 2 条）：`{research_id}:g{generation}:{run_instance}`。

        R3-fix（规格第 1 条）：thread 身份注入 **run 实例**分量——同一题两次独立跑
        （不同 run_root）派生不同 thread_id/run_id，不再撞 bus 409；同一次 run 的
        kill-restart（同 run_root）仍得相同身份。任何随机量都不得进入此串，否则
        derived run id 会随重启漂移，re-adopt 失效。
        """
        return f"{self.research_id}:g{self.generation}:{self.run_instance}"

    @property
    def resolved_checkpoint_path(self) -> str:
        return self.checkpoint_path or str(self.run_root / "checkpoint.sqlite3")


def build_research(
    config: ResearchConfig,
    *,
    text_node: Any = None,
    launcher: Any = None,
    observe: Any = None,
    publisher: Any = None,
) -> tuple[Any, ResearchDeps]:
    """接线一个 research 工单。返回编译前的图与 deps。

    ``publisher`` 是发布端口（协议上等价 ``BusClient.publish``）：生产装配真实
    ``BusClient``（无凭据时降级为 None = 不发布），测试注入 fake transport。
    """
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
            concurrency=config.concurrency,
        ),
        seed_model=config.seed_model,
        sources=config.sources,
        observe=observe,
        publisher=publisher,
    )
    return build_research_graph(deps), deps


def default_publisher() -> Any:
    """生产装配真实 ``BusClient``；无凭据/无法构造时返回 None（不发布，降级）。

    与 scheduler/board 的惯例一致：能连才发布，连不上不拖垮工作。

    R1-返工（委托头根修）：服务 token 就是 fleet-graph 自身——``agent_id`` 与
    ``own_agent_id`` 同置 ``fleet-graph``，client 端据此**不发**
    ``X-Bus-On-Behalf-Of``（发它会被 bus 当成无权委托，403
    DELEGATION_NOT_PERMITTED，全部 publish 静默吞掉——生产实锤根因）。
    """
    try:
        from fleet_graph.bus.client import BusClient

        return BusClient(agent_id="fleet-graph", own_agent_id="fleet-graph")
    except Exception:
        return None


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
    publisher: Any = None,
) -> dict[str, Any]:
    """跑一个 research 工单到终态，写 events.jsonl 与 result.json 后返回摘要。

    terminal ∈ {converged, capped, partial} 才算跑通；fault（seed/debate 故障、
    意外异常）非零退出由 CLI 侧决定。单 clue 失败绝不 fault 整图——那是图内的
    retry/block 状态机，不是这里的异常路径。

    ``publisher`` 是发布端口：None = 不发布（库函数缺省不自动碰真实 bus，测试
    全程 hermetic）；生产装配由 CLI 经 ``default_publisher()`` 传入真实 BusClient，
    测试注入 fake transport。
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

    graph, deps = build_research(
        config,
        text_node=text_node,
        launcher=launcher,
        observe=persist_event,
        publisher=publisher,
    )
    # R1-返工：发布目标频道必须已存在（缺失频道 publish 直接 404）——真实 run
    # 先幂等建好三个 research 频道。创建失败照样 loud（累计进 publish_degraded）。
    from fleet_graph.research_bus import ensure_research_channels

    ensure_research_channels(publisher, config.research_id, degraded=deps.publish_degraded)

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
        "publish_degraded": deps.publish_degraded.as_dict(),
    }
    write_json_durable(run_root / RESULT, {**result, "written_at": iso(now())})
    return result


__all__ = [
    "EVENTS",
    "RESULT",
    "ResearchConfig",
    "build_research",
    "default_publisher",
    "resume_start",
    "run_research",
]
