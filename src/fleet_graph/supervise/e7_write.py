"""M4 交付 B：E7 处置反应器（goal.md 直写信道 + 送达自验）。

输入是 E7 `decision_swallowed` 事件（payload: source_message_id / reason）。一条
裁决被吞（noop/refused），这里向**该线自己的 goal.md** 追加固定块模板（E7 送达
失败块）——`content_revision` 变化即命中 scheduler 停车唤醒源（goal_revision），
停牌线自然被唤醒，这正是「送达」的机械闭环。

SOP（全部是 script 节点，机械判定，不采信任何自述）：

1. `intake`          —— 解析 E7 payload，取 source_message_id / reason。
2. `resolve_folder`  —— 机械决策链：source_message_id -> decision 消息 ->
                       payload.card_entity_id -> card head -> work_folder_id
                       （复用 supervisor `_folder_id` 同款读取，禁 prose 解析）；
                       解析不到 -> escalated（不猜、不降级静默）。
3. `gate`            —— E7 goal.md 直写权判定：folder_id 命中「E7 直写目标线」
                       白名单（默认 deny-all，未命中 -> refused + 留痕）。
4. `write`           —— 向该线 goal.md 追加固定块模板（source_message_id /
                       reason / at / 监督面直写署名）；块模板封闭，不写任意 prose；
                       机械写动作走注入 ops 层，写函数必须先过 gate（Guard E
                       同 Guard D 纪律）。
5. `postconditions`  —— 送达自验：写后 fs_stat content_revision 确认变化 +
                       fs_read 回读确认块正文在场；两缺任一 -> escalated。
6. `evidence`        —— evidence note 挂卡（best-effort）。
7. `receipt`         —— 结果落 supervisor 自己的 state root。

**生成-验证分离**（同 harvest）：编排层不直接执行任何 work-folder 写，机械操作
委托给 `supervise/e7_ops.DefaultE7Ops`；AST 守卫 Guard E 钉死编排层每个含写原语
的函数必须先调用直写权 gate（`authorize_e7_write`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from fleet_graph.bus.board import NOTE_KIND, WORK_NOTES
from fleet_graph.bus.client import BusClient
from fleet_graph.state.run_artifacts import iso, write_json_durable
from fleet_graph.supervise.e7_allowlist import (
    E7WriteAllowlist,
    E7WriteAuthorization,
)
from fleet_graph.supervise.e7_ops import (
    SUPERVISOR_SIGNATURE,
    E7Ops,
    E7ResolutionError,
)
from fleet_graph.supervise.events import (
    EVENT_DECISION_SWALLOWED,
    SupervisorEvent,
    validate_event,
)

#: E7 终态词汇（outcome）。REFUSED = gate 拒绝（无写动作）；DELIVERED = goal.md
#: 直写成功且送达自验达成；ESCALATED = 失败/升报。
OUTCOME_REFUSED = "refused"
OUTCOME_DELIVERED = "delivered"
OUTCOME_ESCALATED = "escalated"

#: SOP 步骤名封闭枚举——测试据此断言「编排步骤齐全」。
SOP_STEPS = (
    "intake",
    "resolve_folder",
    "gate",
    "write",
    "postconditions",
    "evidence_note",
)

#: E7 送达失败块标题（固定模板，回读送达自验的 marker）。
DELIVERY_FAIL_BLOCK_TITLE = "E7 送达失败（监督面直写）"


def build_delivery_fail_block(source_message_id: str, reason: str, at: str | None = None) -> str:
    """封闭块模板：字段固定，不写任意 prose。

    ``at`` 缺省为当前 UTC ISO（调用方也可显式注入以便测试）。signature 固定。
    """
    when = at or iso(time.time())
    return (
        f"## {DELIVERY_FAIL_BLOCK_TITLE}\n"
        f"\n"
        f"- source_message_id: {source_message_id}\n"
        f"- reason: {reason}\n"
        f"- at: {when}\n"
        f"- 署名: {SUPERVISOR_SIGNATURE}\n"
    )


class E7WriteDeps:
    """E7 反应器对外只依赖这几个端口，全部注入以便测试替换。"""

    allowlist: E7WriteAllowlist
    ops: E7Ops
    state_root: Path
    run_root: Path
    thread_id: str
    bus: BusClient | None = None
    publish_notes: bool = True
    clock: Any = time.time
    #: katana-wiki-mcp 客户端（可选）。命中成功收口时向「舰队发展阶段性成果报告」
    #: 页追加缺陷闭环分节；None -> 不汇报。wiki 是 telemetry，失败必须不咬反应器。
    wiki: Any | None = None

    def __init__(
        self,
        *,
        allowlist: E7WriteAllowlist,
        ops: E7Ops,
        state_root: Path,
        run_root: Path,
        thread_id: str,
        bus: BusClient | None = None,
        publish_notes: bool = True,
        clock: Any = time.time,
        wiki: Any | None = None,
    ) -> None:
        self.allowlist = allowlist
        self.ops = ops
        self.state_root = state_root
        self.run_root = run_root
        self.thread_id = thread_id
        self.bus = bus
        self.publish_notes = publish_notes
        self.clock = clock
        self.wiki = wiki


class E7WriteState(TypedDict, total=False):
    event: dict[str, Any]
    source_message_id: str
    reason: str
    folder_id: str
    block: str
    gate_auth: dict[str, Any]
    steps: list[dict[str, Any]]
    write_facts: dict[str, Any]
    evidence_note_id: str
    outcome: str
    receipt_path: str
    _gaps: list[str]


def _event_of(state: E7WriteState) -> SupervisorEvent:
    return validate_event(state.get("event") or {})


def _record_step(state: E7WriteState, step: str, **facts: Any) -> list[dict[str, Any]]:
    steps = list(state.get("steps") or [])
    steps.append({"step": step, **facts})
    return steps


def authorize_e7_write(allowlist: E7WriteAllowlist, folder_id: str) -> E7WriteAuthorization:
    """E7 goal.md 直写权（唯一的写门，Guard E 钉死的 gate 名）。

    写权限唯一来源 = 命中白名单条目；命中不了 -> 拒绝 + 留痕。拒绝是结果不是
    异常：调用方把它记进 steps/evidence，并跳过该写动作。
    """
    return allowlist.authorize(folder_id)


def build_e7_write_graph(deps: E7WriteDeps) -> StateGraph:
    def intake(state: E7WriteState) -> E7WriteState:
        event = _event_of(state)
        payload = event.payload or {}
        source_message_id = str(payload.get("source_message_id") or "")
        reason = str(payload.get("reason") or "")
        gaps: list[str] = []
        if not source_message_id:
            gaps.append("E7 payload 缺 source_message_id——事件不完整")
        steps = _record_step(
            state,
            "intake",
            ok=not gaps,
            source_message_id=source_message_id,
            reason=reason,
        )
        return {
            "source_message_id": source_message_id,
            "reason": reason,
            "steps": steps,
            "_gaps": gaps,
            "outcome": OUTCOME_ESCALATED if gaps else None,
        }

    def resolve_folder(state: E7WriteState) -> E7WriteState:
        source_message_id = state.get("source_message_id") or ""
        try:
            folder_id = deps.ops.resolve_folder_id(deps.bus, source_message_id)
        except E7ResolutionError as exc:
            return {
                "steps": _record_step(state, "resolve_folder", ok=False, detail=str(exc)[:300]),
                "outcome": OUTCOME_ESCALATED,
            }
        steps = _record_step(state, "resolve_folder", ok=True, folder_id=folder_id)
        return {"steps": steps, "folder_id": folder_id}

    def gate(state: E7WriteState) -> E7WriteState:
        auth = authorize_e7_write(deps.allowlist, state.get("folder_id") or "")
        steps = _record_step(state, "gate", ok=auth.granted, evidence=auth.as_dict())
        if not auth.granted:
            return {"steps": steps, "gate_auth": auth.as_dict(), "outcome": OUTCOME_REFUSED}
        return {"steps": steps, "gate_auth": auth.as_dict()}

    def write(state: E7WriteState) -> E7WriteState:
        # Guard E：写原语必须先过 gate。gate 已在上一节点判定，这里再做一次幂等
        # 复查（belt and braces，同 Guard D 逐写步骤 authorize 纪律）。
        auth = authorize_e7_write(deps.allowlist, state.get("folder_id") or "")
        if not auth.granted:
            return {
                "steps": _record_step(
                    state,
                    "write",
                    ok=False,
                    evidence=auth.as_dict(),
                    detail="gate 拒绝，未执行直写",
                ),
                "outcome": OUTCOME_REFUSED,
            }
        folder_id = state.get("folder_id") or ""
        block = build_delivery_fail_block(
            state.get("source_message_id") or "",
            state.get("reason") or "",
            at=iso(float(deps.clock())),
        )
        try:
            facts = deps.ops.append_delivery_fail_block(folder_id, block)
        except E7ResolutionError as exc:
            return {
                "steps": _record_step(state, "write", ok=False, detail=str(exc)[:300]),
                "outcome": OUTCOME_ESCALATED,
            }
        steps = _record_step(state, "write", ok=True, **facts)
        return {"steps": steps, "block": block, "write_facts": facts}

    def postconditions(state: E7WriteState) -> E7WriteState:
        """送达自验（不采信自述）：content_revision 变化 + 回读块正文在场，两缺
        任一 -> escalated。"""
        facts = state.get("write_facts") or {}
        missing: list[str] = []
        if not facts.get("revision_changed"):
            missing.append("fs_stat content_revision 未变化——直写未落")
        if not facts.get("readback_present"):
            missing.append("fs_read 回读不含块标题——送达未证")
        steps = _record_step(state, "postconditions", ok=not missing, missing=missing)
        outcome = OUTCOME_DELIVERED if not missing else OUTCOME_ESCALATED
        return {"steps": steps, "outcome": outcome}

    def evidence(state: E7WriteState) -> E7WriteState:
        event = _event_of(state)
        if not deps.publish_notes or deps.bus is None:
            return {
                "steps": _record_step(
                    state, "evidence_note", ok=False, detail="无 bus 凭证——note 未挂卡"
                )
            }
        folder_id = state.get("folder_id") or ""
        note = (
            f"E7 处置 {event.type} {event.key}: {state.get('outcome') or 'in_progress'}\n"
            f"source_message_id={state.get('source_message_id')} folder={folder_id}\n"
            f"steps: {[s.get('step') for s in state.get('steps') or []]}"
        )
        try:
            published = deps.bus.publish(
                WORK_NOTES,
                NOTE_KIND,
                {"card_entity_id": folder_id, "note": note, "note_type": "evidence"},
                f"e7-write:{event.key}",
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

    def receipt(state: E7WriteState) -> E7WriteState:
        event = _event_of(state)
        outcome = state.get("outcome")
        # 交付 C：缺陷闭环触发挂在反应器终止路径。best-effort——wiki 是 telemetry，
        # 失败必须不咬反应器（吞掉并仅留痕）。
        if deps.wiki is not None and outcome == OUTCOME_DELIVERED:
            try:
                from fleet_graph.supervise.wiki_report import record_defect_closed

                record_defect_closed(
                    deps.wiki,
                    defect_name=f"E7 送达失败 {state.get('source_message_id') or ''}",
                    background=(
                        "一条 human 裁决被吞（swallowed），处置反应器向其线的 goal.md "
                        "直写送达失败块——content_revision 变化即命中 scheduler 停车唤醒源。"
                    ),
                    delivery=(
                        f"goal.md 已直写，content_revision 变化且回读在场；outcome={outcome}"
                    ),
                    evidence=(f"event {event.key}",),
                    at=iso(float(deps.clock())),
                    skeleton="# 舰队发展阶段性成果报告\n\n按「报告更新约定」追加分节。\n",
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
                "source_message_id": state.get("source_message_id"),
                "reason": state.get("reason"),
                "folder_id": state.get("folder_id"),
                "block": state.get("block"),
                "gate_auth": state.get("gate_auth") or {},
                "write_facts": state.get("write_facts") or {},
                "steps": state.get("steps") or [],
                "evidence_note_id": state.get("evidence_note_id"),
                "outcome": outcome,
            },
        )
        return {"receipt_path": str(path)}

    def after_intake(state: E7WriteState) -> str:
        return "resolve_folder" if state.get("outcome") is None else "receipt"

    def after_resolve(state: E7WriteState) -> str:
        return "gate" if state.get("outcome") is None else "receipt"

    def after_gate(state: E7WriteState) -> str:
        return "write" if state.get("outcome") is None else "receipt"

    def after_write(state: E7WriteState) -> str:
        # write 失败/被 gate 拒绝 -> 直接 receipt；write 成功 -> postconditions 送达自验。
        return "postconditions" if state.get("outcome") is None else "receipt"

    graph: StateGraph = StateGraph(E7WriteState)
    graph.add_node("intake", intake)
    graph.add_node("resolve_folder", resolve_folder)
    graph.add_node("gate", gate)
    graph.add_node("write", write)
    graph.add_node("postconditions", postconditions)
    graph.add_node("evidence", evidence)
    graph.add_node("receipt", receipt)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", after_intake, {"resolve_folder", "receipt"})
    graph.add_conditional_edges("resolve_folder", after_resolve, {"gate", "receipt"})
    graph.add_conditional_edges("gate", after_gate, {"write", "receipt"})
    graph.add_conditional_edges("write", after_write, {"postconditions", "receipt"})
    graph.add_edge("postconditions", "evidence")
    graph.add_edge("evidence", "receipt")
    graph.add_edge("receipt", END)
    return graph


# --- assembly ---------------------------------------------------------------


@dataclass
class E7WriteRunConfig:
    event: dict[str, Any]
    state_root: Path = Path("/data/fleet-graph/supervisor")
    run_root: Path = Path("/data/fleet-graph/runs")
    checkpoint_path: str | None = None
    allowlist: E7WriteAllowlist = field(default_factory=E7WriteAllowlist.default)
    ops: E7Ops | None = None
    bus: BusClient | None = None
    publish_notes: bool = True
    clock: Any = time.time
    wiki: Any | None = None

    @property
    def resolved_checkpoint_path(self) -> str:
        return self.checkpoint_path or str(self.state_root / "checkpoint.sqlite3")


def build_e7_write(config: E7WriteRunConfig) -> tuple[Any, E7WriteDeps, SupervisorEvent]:
    from fleet_graph.supervise.e7_ops import DefaultE7Ops

    event = validate_event(config.event)
    deps = E7WriteDeps(
        allowlist=config.allowlist,
        ops=config.ops or DefaultE7Ops(),
        state_root=config.state_root,
        run_root=config.run_root,
        thread_id=event.thread_id,
        bus=config.bus,
        publish_notes=config.publish_notes,
        clock=config.clock,
        wiki=config.wiki,
    )
    return build_e7_write_graph(deps), deps, event


def run_e7_write(config: E7WriteRunConfig) -> dict[str, Any]:
    """跑一次 E7 处置到 receipt，线程已终局则 no-op（与 run_harvest 同语义）。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    graph, _deps, event = build_e7_write(config)
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
    "DELIVERY_FAIL_BLOCK_TITLE",
    "EVENT_DECISION_SWALLOWED",
    "OUTCOME_DELIVERED",
    "OUTCOME_ESCALATED",
    "OUTCOME_REFUSED",
    "SOP_STEPS",
    "E7WriteAuthorization",
    "E7WriteDeps",
    "E7WriteRunConfig",
    "E7WriteState",
    "authorize_e7_write",
    "build_delivery_fail_block",
    "build_e7_write",
    "build_e7_write_graph",
    "run_e7_write",
]
