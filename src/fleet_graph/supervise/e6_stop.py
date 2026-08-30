"""M4 交付 A：E6 处置反应器（stop → 代谢重拉）。

输入是 E6 `heartbeat_stale` 事件（payload: folder_id / heartbeat_age_s / round /
phase）。一条线的 heartbeat 超龄（:7494 `/v1/lines` 派生，未 terminal、未
parked），这里把它的 line unit 停掉——「代谢重拉」由 scheduler 下一 tick 自然
re-launch（stop 后 unit 不 active、无 terminal -> ignition 重拉起）。本单**不实现
第二调度**：反应器不 import `scheduler.ignition` / `scheduler.launcher`（Guard A
不动）。

SOP（全部是 script 节点，机械判定，不采信任何自述）：

1. `intake`        —— 解析 E6 payload，取 folder_id（非空且 `wf-` 前缀，否则
                      escalated）。
2. `resolve_unit`  —— 机械解析目标 line unit：前缀
                      `fleet-graph-line-<folder_id>-*` 下唯一 active 单元
                      （`systemctl --user list-units` 输出解析），或读 scheduler
                      stall-state 的 generation 构造
                      `fleet-graph-line-<folder_id>-g<gen>`；解析不到/多解 ->
                      escalated，**绝不任意 stop**。
3. `gate`          —— E6 停止权判定：目标 unit 必须是 event.folder_id 自己的
                      line unit（前缀精确匹配，越界 -> refused + 留痕）。
4. `stop`          —— `systemctl --user stop <unit>`；机械写动作走注入 ops 层，
                      写函数必须先过 gate（Guard E 同 Guard D 纪律）。
5. `postconditions`—— 代码核验 stop 后 unit 不再 active（`is-active` 非 0）或
                      :7494 `/v1/lines` 该线心跳龄回落；不采信自述；未达成 ->
                      escalated。
6. `evidence`      —— evidence note 挂卡（best-effort）。
7. `receipt`       —— 结果落 supervisor 自己的 state root。

**生成-验证分离**（同 harvest）：编排层不直接执行任何 systemctl 命令，机械操作
委托给 `supervise/e6_ops.DefaultE6Ops`；AST 守卫 Guard E 钉死编排层每个含写原语
的函数必须先调用 stop 权 gate（`authorize_e6_stop`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from fleet_graph.bus.board import NOTE_KIND, WORK_NOTES
from fleet_graph.bus.client import BusClient
from fleet_graph.state.run_artifacts import iso, write_json_durable
from fleet_graph.supervise.e6_ops import HEARTBEAT_STALE_THRESHOLD_SECONDS, E6Ops
from fleet_graph.supervise.events import (
    EVENT_HEARTBEAT_STALE,
    SupervisorEvent,
    validate_event,
)

#: E6 终态词汇（outcome）。REFUSED = gate 拒绝（无写动作）；STOPPED = stop 成功
#: 且 postcondition 达成；ESCALATED = 失败/升报。
OUTCOME_REFUSED = "refused"
OUTCOME_STOPPED = "stopped"
OUTCOME_ESCALATED = "escalated"

#: SOP 步骤名封闭枚举——测试据此断言「编排步骤齐全」。
SOP_STEPS = (
    "intake",
    "resolve_unit",
    "gate",
    "stop",
    "postconditions",
    "evidence_note",
)


class E6StopDeps:
    """E6 反应器对外只依赖这几个端口，全部注入以便测试替换。"""

    ops: E6Ops
    state_root: Path
    run_root: Path
    thread_id: str
    bus: BusClient | None = None
    publish_notes: bool = True
    #: katana-wiki-mcp 客户端（可选）。命中成功收口时向「舰队开发阶段性成果报告」
    #: 页追加缺陷闭环分节；None -> 不汇报。wiki 是 telemetry，失败必须不咬反应器。
    wiki: Any | None = None

    def __init__(
        self,
        *,
        ops: E6Ops,
        state_root: Path,
        run_root: Path,
        thread_id: str,
        bus: BusClient | None = None,
        publish_notes: bool = True,
        wiki: Any | None = None,
    ) -> None:
        self.ops = ops
        self.state_root = state_root
        self.run_root = run_root
        self.thread_id = thread_id
        self.bus = bus
        self.publish_notes = publish_notes
        self.wiki = wiki


class E6StopState(TypedDict, total=False):
    event: dict[str, Any]
    folder_id: str
    unit: str
    unit_source: str
    gate_auth: dict[str, Any]
    steps: list[dict[str, Any]]
    stop_exit_code: int
    active_after: bool
    read_model_age: float | None
    evidence_note_id: str
    outcome: str
    receipt_path: str
    _gaps: list[str]


def _event_of(state: E6StopState) -> SupervisorEvent:
    return validate_event(state.get("event") or {})


def _record_step(state: E6StopState, step: str, **facts: Any) -> list[dict[str, Any]]:
    steps = list(state.get("steps") or [])
    steps.append({"step": step, **facts})
    return steps


@dataclass(frozen=True)
class E6StopAuthorization:
    """一次 stop 权判定的结果。granted=False 时 reasons 即留痕内容。"""

    granted: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"granted": self.granted, "reasons": list(self.reasons)}


def authorize_e6_stop(folder_id: str, unit: str) -> E6StopAuthorization:
    """E6 停止权（唯一的写门，Guard E 钉死的 gate 名）。

    目标 unit 必须是 event.folder_id 自己的 line unit：前缀精确匹配
    `fleet-graph-line-<folder_id>-`。越界/空 -> 拒绝 + 留痕。拒绝是结果不是异常：
    调用方把它记进 steps/evidence，并跳过该写动作。
    """
    reasons: list[str] = []
    if not folder_id.startswith("wf-"):
        reasons.append(f"folder_id {folder_id!r} 不是 wf- 前缀——不是目标线")
    if not unit or not unit.startswith(f"fleet-graph-line-{folder_id}-"):
        reasons.append(
            f"unit {unit!r} 不是 {folder_id} 自己的 line unit"
            "（前缀精确匹配失败）——禁 arbitrary stop"
        )
    return E6StopAuthorization(granted=not reasons, reasons=tuple(reasons))


def build_e6_stop_graph(deps: E6StopDeps) -> StateGraph:
    def intake(state: E6StopState) -> E6StopState:
        event = _event_of(state)
        payload = event.payload or {}
        folder_id = str(payload.get("folder_id") or "")
        gaps: list[str] = []
        if not folder_id:
            gaps.append("E6 payload 缺 folder_id——事件不完整")
        elif not folder_id.startswith("wf-"):
            gaps.append(f"E6 payload folder_id {folder_id!r} 不是 wf- 前缀——不是目标线")
        steps = _record_step(
            state,
            "intake",
            ok=not gaps,
            folder_id=folder_id,
            heartbeat_age_s=payload.get("heartbeat_age_s"),
            round=payload.get("round"),
            phase=payload.get("phase"),
        )
        return {
            "folder_id": folder_id,
            "steps": steps,
            "_gaps": gaps,
            "outcome": OUTCOME_ESCALATED if gaps else None,
        }

    def resolve_unit(state: E6StopState) -> E6StopState:
        folder_id = state.get("folder_id") or ""
        try:
            result = deps.ops.resolve_line_unit(folder_id, deps.run_root)
        except Exception as exc:
            return {
                "steps": _record_step(
                    state, "resolve_unit", ok=False, detail=f"resolve 失败: {repr(exc)[:300]}"
                ),
                "outcome": OUTCOME_ESCALATED,
            }
        ok = bool(result.get("ok"))
        unit = str(result.get("unit") or "")
        steps = _record_step(
            state,
            "resolve_unit",
            ok=ok,
            unit=unit,
            source=result.get("source"),
            detail=result.get("detail") or "",
        )
        if not ok or not unit:
            return {"steps": steps, "outcome": OUTCOME_ESCALATED}
        return {"steps": steps, "unit": unit, "unit_source": result.get("source")}

    def gate(state: E6StopState) -> E6StopState:
        auth = authorize_e6_stop(state.get("folder_id") or "", state.get("unit") or "")
        steps = _record_step(
            state,
            "gate",
            ok=auth.granted,
            evidence=auth.as_dict(),
        )
        if not auth.granted:
            return {"steps": steps, "gate_auth": auth.as_dict(), "outcome": OUTCOME_REFUSED}
        return {"steps": steps, "gate_auth": auth.as_dict()}

    def stop(state: E6StopState) -> E6StopState:
        # Guard E：写原语必须先过 gate。gate 已在上一节点判定，这里再做一次
        # 幂等复查（belt and braces，同 Guard D 逐写步骤 authorize 纪律）。
        auth = authorize_e6_stop(state.get("folder_id") or "", state.get("unit") or "")
        if not auth.granted:
            return {
                "steps": _record_step(
                    state,
                    "stop",
                    ok=False,
                    evidence=auth.as_dict(),
                    detail="gate 拒绝，未执行 stop",
                ),
                "outcome": OUTCOME_REFUSED,
            }
        unit = state.get("unit") or ""
        try:
            exit_code = int(deps.ops.stop_unit(unit))
        except Exception as exc:
            return {
                "steps": _record_step(
                    state, "stop", ok=False, detail=f"stop 执行失败: {repr(exc)[:300]}"
                )
            }
        steps = _record_step(state, "stop", ok=exit_code == 0, unit=unit, exit_code=exit_code)
        return {"steps": steps, "stop_exit_code": exit_code}

    def postconditions(state: E6StopState) -> E6StopState:
        """stop 后代码核验：unit 不再 active，或 :7494 心跳龄已回落。不采信自述。"""
        missing: list[str] = []
        unit = state.get("unit") or ""
        active_after = False
        try:
            active_after = bool(deps.ops.is_active(unit))
        except Exception as exc:
            missing.append(f"is-active 探测失败: {repr(exc)[:200]}")
        read_model_age: float | None = None
        if active_after:
            try:
                read_model_age = deps.ops.line_heartbeat_age_s(state.get("folder_id") or "")
            except Exception as exc:
                missing.append(f":7494 心跳龄读取失败: {repr(exc)[:200]}")
        age_ok = read_model_age is not None and read_model_age <= HEARTBEAT_STALE_THRESHOLD_SECONDS
        if active_after and not age_ok:
            missing.append(
                f"unit {unit} stop 后仍 active，且 :7494 心跳龄 "
                f"{read_model_age if read_model_age is not None else '不可读'} 未回落"
            )
        steps = _record_step(
            state,
            "postconditions",
            ok=not missing,
            active_after=active_after,
            read_model_age=read_model_age,
            missing=missing,
        )
        outcome = OUTCOME_STOPPED if not missing else OUTCOME_ESCALATED
        return {
            "steps": steps,
            "active_after": active_after,
            "read_model_age": read_model_age,
            "outcome": outcome,
        }

    def evidence(state: E6StopState) -> E6StopState:
        event = _event_of(state)
        if not deps.publish_notes or deps.bus is None:
            return {
                "steps": _record_step(
                    state, "evidence_note", ok=False, detail="无 bus 凭证——note 未挂卡"
                )
            }
        folder_id = state.get("folder_id") or ""
        unit = state.get("unit") or ""
        note = (
            f"E6 处置 {event.type} {event.key}: {state.get('outcome') or 'in_progress'}\n"
            f"folder={folder_id} unit={unit} "
            f"heartbeat_age_s={_event_of(state).payload.get('heartbeat_age_s')}\n"
            f"steps: {[s.get('step') for s in state.get('steps') or []]}"
        )
        try:
            published = deps.bus.publish(
                WORK_NOTES,
                NOTE_KIND,
                {"card_entity_id": folder_id, "note": note, "note_type": "evidence"},
                f"e6-stop:{event.key}",
                refs=[{"target_entity": folder_id}] if folder_id else [],
            )
        except Exception as exc:
            return {
                "steps": _record_step(
                    state, "evidence_note", ok=False, detail=f"board note 被拒: {repr(exc)[:300]}"
                )
            }
        steps = _record_step(state, "evidence_note", ok=True, evidence_note_id=published.message_id)
        return {"steps": steps, "evidence_note_id": published.message_id}

    def receipt(state: E6StopState) -> E6StopState:
        event = _event_of(state)
        outcome = state.get("outcome")
        # 交付 C：缺陷闭环触发挂在反应器终止路径。best-effort——wiki 是 telemetry，
        # 失败必须不咬反应器（吞掉并仅留痕）。
        if deps.wiki is not None and outcome == OUTCOME_STOPPED:
            try:
                from fleet_graph.supervise.wiki_report import record_defect_closed

                record_defect_closed(
                    deps.wiki,
                    defect_name=f"E6 停牌 {state.get('folder_id') or ''}",
                    background=(
                        "一条线的 heartbeat 超龄停摆（:7494 派生），处置反应器将其 "
                        "line unit 停止，scheduler 下一 tick 自然重拉。"
                    ),
                    delivery=(
                        f"unit {state.get('unit') or ''} 已 stop，is-active 回落；outcome={outcome}"
                    ),
                    evidence=(f"event {event.key}",),
                    at=iso(time.time()),
                    skeleton="# 舰队开发阶段性成果报告\n\n按「报告更新约定」追加分节。\n",
                )
            except Exception as exc:  # telemetry must not bite
                steps = list(state.get("steps") or [])
                steps.append(
                    {
                        "step": "wiki_report",
                        "ok": False,
                        "detail": f"wiki 追加失败: {repr(exc)[:200]}",
                    }
                )
                state = {**state, "steps": steps}
        path = write_json_durable(
            deps.state_root / "reports" / f"{event.key}.json",
            {
                "event": event.as_dict(),
                "thread_id": deps.thread_id,
                "folder_id": state.get("folder_id"),
                "unit": state.get("unit"),
                "unit_source": state.get("unit_source"),
                "gate_auth": state.get("gate_auth") or {},
                "steps": state.get("steps") or [],
                "stop_exit_code": state.get("stop_exit_code"),
                "active_after": state.get("active_after"),
                "read_model_age": state.get("read_model_age"),
                "evidence_note_id": state.get("evidence_note_id"),
                "outcome": outcome,
            },
        )
        return {"receipt_path": str(path)}

    def after_intake(state: E6StopState) -> str:
        return "resolve_unit" if state.get("outcome") is None else "receipt"

    def after_resolve(state: E6StopState) -> str:
        return "gate" if state.get("outcome") is None else "receipt"

    def after_gate(state: E6StopState) -> str:
        return "stop" if state.get("outcome") is None else "receipt"

    def after_stop(state: E6StopState) -> str:
        # stop 失败/被 gate 拒绝 -> 直接 receipt（不跑 postconditions 的误导性结论）；
        # stop 成功 -> postconditions 核验。
        return "postconditions" if state.get("outcome") is None else "receipt"

    def after_postconditions(state: E6StopState) -> str:
        return "evidence"

    graph: StateGraph = StateGraph(E6StopState)
    graph.add_node("intake", intake)
    graph.add_node("resolve_unit", resolve_unit)
    graph.add_node("gate", gate)
    graph.add_node("stop", stop)
    graph.add_node("postconditions", postconditions)
    graph.add_node("evidence", evidence)
    graph.add_node("receipt", receipt)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", after_intake, {"resolve_unit", "receipt"})
    graph.add_conditional_edges("resolve_unit", after_resolve, {"gate", "receipt"})
    graph.add_conditional_edges("gate", after_gate, {"stop", "receipt"})
    graph.add_conditional_edges("stop", after_stop, {"postconditions", "receipt"})
    graph.add_edge("postconditions", "evidence")
    graph.add_edge("evidence", "receipt")
    graph.add_edge("receipt", END)
    return graph


# --- assembly ---------------------------------------------------------------


@dataclass
class E6StopRunConfig:
    event: dict[str, Any]
    state_root: Path = Path("/data/fleet-graph/supervisor")
    run_root: Path = Path("/data/fleet-graph/runs")
    checkpoint_path: str | None = None
    ops: E6Ops | None = None
    bus: BusClient | None = None
    publish_notes: bool = True
    wiki: Any | None = None

    @property
    def resolved_checkpoint_path(self) -> str:
        return self.checkpoint_path or str(self.state_root / "checkpoint.sqlite3")


def build_e6_stop(config: E6StopRunConfig) -> tuple[Any, E6StopDeps, SupervisorEvent]:
    from fleet_graph.supervise.e6_ops import DefaultE6Ops

    event = validate_event(config.event)
    deps = E6StopDeps(
        ops=config.ops or DefaultE6Ops(),
        state_root=config.state_root,
        run_root=config.run_root,
        thread_id=event.thread_id,
        bus=config.bus,
        publish_notes=config.publish_notes,
        wiki=config.wiki,
    )
    return build_e6_stop_graph(deps), deps, event


def run_e6_stop(config: E6StopRunConfig) -> dict[str, Any]:
    """跑一次 E6 处置到 receipt，线程已终局则 no-op（与 run_harvest 同语义）。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    graph, _deps, event = build_e6_stop(config)
    invoke_config: dict[str, Any] = {
        "configurable": {"thread_id": event.thread_id},
        "recursion_limit": 50,
    }

    checkpoint = config.resolved_checkpoint_path
    if checkpoint != ":memory:":
        Path(checkpoint).parent.mkdir(parents=True, exist_ok=True)

    with SqliteSaver.from_conn_string(checkpoint) as saver:
        compiled = graph.compile(checkpointer=saver)
        snapshot = compiled.get_state(invoke_config)
        if snapshot.next:
            start: dict[str, Any] | None = None  # resume in place
        elif snapshot.values and snapshot.values.get("receipt_path"):
            return {
                "event": event.as_dict(),
                "thread_id": event.thread_id,
                "outcome": snapshot.values.get("outcome"),
                "receipt_path": snapshot.values.get("receipt_path"),
                "resumed": "already_complete",
            }
        else:
            start = {"event": event.as_dict()}
        state = compiled.invoke(start, config=invoke_config)

    return {
        "event": event.as_dict(),
        "thread_id": event.thread_id,
        "outcome": state.get("outcome"),
        "steps": state.get("steps"),
        "receipt_path": state.get("receipt_path"),
    }


__all__ = [
    "EVENT_HEARTBEAT_STALE",
    "OUTCOME_ESCALATED",
    "OUTCOME_REFUSED",
    "OUTCOME_STOPPED",
    "SOP_STEPS",
    "E6StopAuthorization",
    "E6StopDeps",
    "E6StopRunConfig",
    "E6StopState",
    "authorize_e6_stop",
    "build_e6_stop",
    "build_e6_stop_graph",
    "run_e6_stop",
]
