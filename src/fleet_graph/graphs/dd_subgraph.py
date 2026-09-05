"""R2 图合一（wf-4601c8）：dd 流水线降为 goal_line 的子图，由图的边实例化。

判据锚：goal.md §二 R2；design.md §1（宪法第十三条 通信协议化对外一个入口、
第八条 账随事走）、§3 自决「checkpoint A 方案」；findings【同型缺陷三连】。

三件事（spec 行为契约 1+2）：

- **线到单是图边**：coordinator 节点产出派单意图后，父图用 LangGraph ``Send``
  按单 fan-out——每单一次子图调用、state 互相隔离（本模块的
  ``DdSubgraphState`` 不是 ``LineState``，channel 不相交）。版本依据：仓内
  LangGraph == 1.2.11（pyproject 钉死），``langgraph.types.Send(node, arg)``
  是该版本 fan-out 的官方 API；仓内同款先例见 research_pipeline.py 的
  dispatch → collect 扇出。
- **development_create 内部函数化**：线内派单全程图内调用
  ``DdControlPlane.create``（经 :class:`ControlPlaneGateway`），零 MCP 往返、
  无 fleet-graph-dd-mcp 工具调用记录。MCP 面上的同名工具只留给外门（监督者
  principal），见 dd/service.py 的 outer gate。
- **单到线是子图返回值**：dd 终态（complete/failed、result.json 摘要、
  output_commit、代际）由子图返回值汇合进线状态（reducer channel），调度器
  唤醒路径上没有任何分支再读盘面文件当 dd 终态事件。

权威件纪律：gateway 的观察面只读 dd 两权威件（record.json / result.json，
经 ``DdControlPlane.get`` 的权威投影）——status.json / terminal.json /
.scheduler 不是唤醒或终态信号，磁盘退回纯持久化。
"""

from __future__ import annotations

import time
from typing import Any, Protocol, TypedDict


def merge_dd_results(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """The fan-out merge for the line's ``dd_results`` channel.

    每个 Send 任务只写自己那一单（development_id -> result），按 key 合并；
    合并是纯函数，重放不产生副作用（重复派发由准入幂等键拦截，见
    ControlPlaneGateway.admit）。
    """
    return {**(left or {}), **(right or {})}


class DdDispatchIntent(TypedDict, total=False):
    """Coordinator 产出的派单意图（development_create 内部函数的入参投影）。"""

    repo_path: str
    target_base: str
    spec_text: str
    spec_path: str
    timeouts: dict[str, int]
    stage_models: dict[str, str]


class DdDevelopmentResult(TypedDict, total=False):
    """子图返回值：一单的终态投影（可含诚实的中途态 awaiting_gate/in_flight）。"""

    development_id: str
    state: str
    terminal: str
    terminal_reason: str
    #: result.json 的 head_commit —— dd 侧权威产出 commit（终态权威）。
    output_commit: str
    stage: str
    generation: int


class DdSubgraphState(TypedDict, total=False):
    """子图 state：与 LineState 完全隔离，只装一单的事实。"""

    #: Send 命令携带的输入：线身份 + 派单意图。
    line_folder: str
    intent: DdDispatchIntent
    #: admit 节点回填的准入事实（development_id、already_admitted、generation）。
    record: dict[str, Any]
    #: observe 节点产出的终态投影——父图经返回值汇合的唯一 dd 终态信道。
    dd_result: DdDevelopmentResult


class DevelopmentGateway(Protocol):
    """线对 dd 的唯一入口，两个真实阶段各一个方法：

    - ``admit``：development_create 内部函数化（图内直调 ``create``），非重复
      准入时随后 start。准入幂等键 = (repo, spec, base) → 同一 development_id；
      已派单事实（record.json）在，就绝不二次建单——checkpoint A 方案的
      「删库重建后不重复派发」立在这里。
    - ``observe``：按权威投影观察唤醒点（awaiting_gate）或终态，超预算如实回
      ``in_flight``。绝不读 status.json / terminal.json / .scheduler 当事件。

    测试用 fake 替换；生产实现 :class:`ControlPlaneGateway`。
    """

    def admit(self, intent: DdDispatchIntent, *, line_folder: str) -> dict[str, Any]: ...

    def observe(self, record: dict[str, Any], *, line_folder: str) -> DdDevelopmentResult: ...


class ControlPlaneGateway:
    """生产 gateway：图内直调 dd 控制面（in-process，零 MCP 往返）。"""

    #: 观察唤醒点的上限与间隔（秒）。dd 单的中位生命周期是小时级——这里只负责
    # 「快单（脚本/缓存命中）当场汇合、慢单如实回报在途」；慢单的续跑由 dd 自身
    # 的 transient unit 与线的 waiting_dd 唤醒事实（LiveDdWakeFacts，权威投影）
    # 承担，图状态可从权威件重建（A 方案），没有状态只活在这次观察里。
    DEFAULT_MAX_OBSERVATIONS = 3
    DEFAULT_OBSERVE_INTERVAL_SECONDS = 2.0

    def __init__(
        self,
        plane: Any,
        *,
        max_observations: int = DEFAULT_MAX_OBSERVATIONS,
        observe_interval: float = DEFAULT_OBSERVE_INTERVAL_SECONDS,
        sleeper: Any = None,
    ) -> None:
        self.plane = plane
        self.max_observations = max_observations
        self.observe_interval = observe_interval
        self._sleep = sleeper or time.sleep

    @staticmethod
    def _project(development_id: str, status: dict[str, Any]) -> DdDevelopmentResult:
        """权威投影 → 子图返回值（只搬运机械字段，绝无新 prose）。"""
        try:
            generation = int(status.get("generation") or 1)
        except (TypeError, ValueError):
            generation = 1
        return DdDevelopmentResult(
            development_id=development_id,
            state=str(status.get("state") or ""),
            terminal=str(status.get("terminal") or ""),
            terminal_reason=str(status.get("terminal_reason") or ""),
            output_commit=str(status.get("head_commit") or ""),
            stage=str(status.get("stage") or ""),
            generation=generation,
        )

    def admit(self, intent: DdDispatchIntent, *, line_folder: str) -> dict[str, Any]:
        """development_create 内部函数化：``dispatched_by`` 恒为派单线本身。"""
        payload = {**intent, "dispatched_by": line_folder}
        record = dict(self.plane.create(**payload))
        if not record.get("already_admitted"):
            self.plane.start(str(record["development_id"]))
        return record

    def observe(self, record: dict[str, Any], *, line_folder: str) -> DdDevelopmentResult:
        development_id = str(record.get("development_id") or "")
        for attempt in range(self.max_observations):
            # 权威投影：DdControlPlane.get 的 state 从 record.json + 当代
            # result.json 重建——不是盘面缓存，也不是唤醒路径的事件读。
            status = self.plane.get(development_id)
            state = str(status.get("state") or "")
            if state == "awaiting_gate" or status.get("terminal"):
                return self._project(development_id, status)
            if attempt + 1 < self.max_observations:
                self._sleep(self.observe_interval)
        # 观察预算耗尽：如实回报在途——线状态可从权威件重建（A 方案），
        # 不编造终态，也不把在途伪装成失败。
        return self._project(
            development_id,
            {"state": "in_flight", "generation": record.get("generation") or 1},
        )

    def dispatch(self, intent: DdDispatchIntent, *, line_folder: str) -> DdDevelopmentResult:
        """admit + observe 的组合，一次性语义（测试与运维便利面）。"""
        record = self.admit(intent, line_folder=line_folder)
        return self.observe(record, line_folder=line_folder)


def build_dd_subgraph(gateway: DevelopmentGateway) -> Any:
    """编译 dd 子图：admit（内部函数准入）→ observe（权威投影到返回值）。

    子图以 ``DdSubgraphState`` 为 channel——与父图 ``LineState`` channel 不相交，
    满足「子图 state 隔离」；父图的 Send 任务把返回值（``dd_result``）汇合进
    线的 reducer channel（``dd_results``）。
    """
    from langgraph.graph import END, START, StateGraph

    def admit(state: DdSubgraphState) -> DdSubgraphState:
        return {
            "record": dict(
                gateway.admit(
                    state.get("intent") or {},
                    line_folder=str(state.get("line_folder") or ""),
                )
            )
        }

    def observe(state: DdSubgraphState) -> DdSubgraphState:
        return {
            "dd_result": dict(
                gateway.observe(
                    state.get("record") or {},
                    line_folder=str(state.get("line_folder") or ""),
                )
            )
        }

    graph: StateGraph = StateGraph(DdSubgraphState)
    graph.add_node("admit", admit)
    graph.add_node("observe", observe)
    graph.add_edge(START, "admit")
    graph.add_edge("admit", "observe")
    graph.add_edge("observe", END)
    return graph.compile()


class DdSubgraphPort(Protocol):
    """父图 dd_dispatch 节点握住的子图入口：一次 invoke = 一单一次子图执行。"""

    def invoke(self, payload: dict[str, Any], *, config: Any = None) -> dict[str, Any]: ...


class DdSubgraph:
    """The compiled subgraph wrapped as the parent's port (invoke = one Send)."""

    def __init__(self, gateway: DevelopmentGateway) -> None:
        self.gateway = gateway
        self.compiled = build_dd_subgraph(gateway)

    def invoke(self, payload: dict[str, Any], *, config: Any = None) -> dict[str, Any]:
        return dict(self.compiled.invoke(payload, config=config))


__all__ = [
    "ControlPlaneGateway",
    "DdDevelopmentResult",
    "DdDispatchIntent",
    "DdSubgraph",
    "DdSubgraphPort",
    "DdSubgraphState",
    "DevelopmentGateway",
    "build_dd_subgraph",
    "merge_dd_results",
]
