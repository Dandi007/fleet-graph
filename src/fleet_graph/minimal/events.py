"""minimal event 日志：append+fsync 写入与 fold 回放派生状态。

``events.jsonl`` 是唯一状态来源，恢复 = 回放（docs/specs/minimal/protocol.md §8/§11）。
本模块只做纯粹的日志层与 fold 层，不做引擎调度。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GOAL_KINDS: frozenset[str] = frozenset(
    {
        "goal.enrolled",
        "goal.turn.started",
        "goal.turn.finished",
        "goal.done",
        "goal.blocked",
        "goal.warning",
        "goal.message",
        "goal.steered",
        "goal.merged_to_target",
    }
)

DD_KINDS: frozenset[str] = frozenset(
    {
        "dd.dispatched",
        "dd.stage.started",
        "dd.stage.finished",
        "dd.acceptance",
        "dd.review_requested",
        "dd.approved",
        "dd.rejected",
        "dd.merged",
        "dd.failed",
    }
)

AGENT_KINDS: frozenset[str] = frozenset(
    {
        "agent.spawned",
        "agent.exited",
        "agent.failed",
        "agent.compacted",
    }
)

ENGINE_KINDS: frozenset[str] = frozenset(
    {
        "engine.started",
        "engine.resumed",
        "engine.exiting",
    }
)

CONTROL_KINDS: frozenset[str] = frozenset({"control.received"})

KINDS: frozenset[str] = frozenset(
    GOAL_KINDS | DD_KINDS | AGENT_KINDS | ENGINE_KINDS | CONTROL_KINDS
)

_EVENT_KEY_ORDER = ("ts", "goal_id", "dd_id", "kind", "seq", "payload")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Event:
    """一条 event，序列化为一行的 JSON（键顺序固定、紧凑、不转义非 ASCII）。"""

    ts: str
    goal_id: str
    dd_id: str | None
    kind: str
    seq: int
    payload: dict[str, Any]

    def to_json_line(self) -> str:
        return json.dumps(
            {key: getattr(self, key) for key in _EVENT_KEY_ORDER},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json_line(cls, line: str) -> Event:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"event line is not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError("event line is not a JSON object")
        try:
            ts = obj["ts"]
            goal_id = obj["goal_id"]
            kind = obj["kind"]
            seq = obj["seq"]
        except KeyError as exc:
            raise ValueError(f"event line missing key {exc.args[0]!r}") from None
        dd_id = obj.get("dd_id")
        payload = obj.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("event payload must be a JSON object")
        try:
            seq = int(seq)
        except (TypeError, ValueError) as exc:
            raise ValueError("event seq must be an integer") from exc
        return cls(
            ts=str(ts),
            goal_id=str(goal_id),
            dd_id=dd_id,
            kind=str(kind),
            seq=seq,
            payload=payload,
        )


class EventLog:
    """某个 goal 的 event 日志，落在 ``<goal_run_root>/events.jsonl``。

    ``goal_run_root`` 由调用方给定（形如 ``/data/fleet/goals/<goal_id>/``），不在这里
    硬编码；goal_id 取目录最后一级的名字。seq 每个 goal 独立计数，从 1 起，打开已有
    文件时从最后一个可解析 event 的 seq 续下去。
    """

    def __init__(self, goal_run_root: str | os.PathLike[str]) -> None:
        self.goal_run_root = Path(goal_run_root)
        self.path = self.goal_run_root / "events.jsonl"
        self.goal_id = self.goal_run_root.name
        self._next_seq = self._load_next_seq()

    def _load_next_seq(self) -> int:
        if not self.path.exists():
            return 1
        last_seq = 0
        with self.path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = Event.from_json_line(line)
                except ValueError:
                    continue
                last_seq = ev.seq
        return last_seq + 1

    def append(self, kind: str, payload: dict[str, Any], *, dd_id: str | None = None) -> Event:
        if kind not in KINDS:
            raise ValueError(f"unknown event kind: {kind!r}")
        ev = Event(
            ts=_now_iso(),
            goal_id=self.goal_id,
            dd_id=dd_id,
            kind=kind,
            seq=self._next_seq,
            payload=dict(payload),
        )
        self._next_seq += 1
        self.goal_run_root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(ev.to_json_line() + "\n")
            f.flush()
            os.fsync(f.fileno())
        return ev

    def read(self, since_seq: int = 0) -> Iterator[Event]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for idx, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue
            try:
                ev = Event.from_json_line(line)
            except ValueError as exc:
                if idx == len(lines) - 1:
                    return
                raise ValueError(f"corrupt event line {idx + 1}: {exc}") from exc
            if ev.seq > since_seq:
                yield ev


@dataclass
class CurrentDD:
    """当前在途的 DD：dd_id 与本轮 round。没有在途 DD 时 dd_id 为 None。"""

    dd_id: str | None
    round: int


@dataclass
class DDSummary:
    """一张 DD 的结果摘要。"""

    dd_id: str
    outcome: str
    rounds: int


@dataclass
class DerivedState:
    """fold 出的派生状态。"""

    state: str
    turn_no: int
    current_dd: CurrentDD
    dd_history: list[DDSummary]
    goal_version: int
    last_seq: int
    warnings: list[str]
    terminal: bool


def _warning_text(payload: dict[str, Any]) -> str:
    for key in ("message", "detail", "warning", "text"):
        if key in payload:
            return str(payload[key])
    return str(payload)


def fold(events: Iterable[Event]) -> DerivedState:
    state = "running"
    terminal = False
    turn_no = 0
    goal_version = 0
    last_seq = 0
    warnings: list[str] = []
    current_dd_id: str | None = None
    current_round = 0
    round_counts: dict[str, int] = {}
    outcomes: dict[str, str] = {}
    order: list[str] = []
    last_kind = ""
    last_payload: dict[str, Any] = {}

    for ev in events:
        last_seq = ev.seq
        last_kind = ev.kind
        last_payload = ev.payload or {}

        if ev.kind == "goal.enrolled":
            if goal_version == 0:
                goal_version = 1
        elif ev.kind == "goal.steered":
            goal_version += 1
        elif ev.kind == "goal.turn.started":
            turn_no += 1
        elif ev.kind == "goal.warning":
            warnings.append(_warning_text(ev.payload or {}))
        elif ev.kind == "dd.dispatched":
            if ev.dd_id is not None:
                if ev.dd_id not in order:
                    order.append(ev.dd_id)
                round_counts[ev.dd_id] = round_counts.get(ev.dd_id, 0) + 1
                current_dd_id = ev.dd_id
                current_round = round_counts[ev.dd_id]
        elif ev.kind in ("dd.merged", "dd.failed") and ev.dd_id is not None:
            outcomes[ev.dd_id] = "merged" if ev.kind == "dd.merged" else "failed"
            current_dd_id = None
            current_round = 0

    if last_kind == "goal.done":
        state, terminal = "done", True
    elif last_kind == "goal.blocked":
        state, terminal = "blocked", True
    elif last_kind == "engine.exiting":
        reason = last_payload.get("reason")
        if reason == "stop":
            state, terminal = "stopped", True
        elif reason == "done":
            state, terminal = "done", True
        elif reason == "blocked":
            state, terminal = "blocked", True

    dd_history = [
        DDSummary(
            dd_id=dd_id,
            outcome=outcomes.get(dd_id, "in_progress"),
            rounds=round_counts.get(dd_id, 0),
        )
        for dd_id in order
    ]

    return DerivedState(
        state=state,
        turn_no=turn_no,
        current_dd=CurrentDD(dd_id=current_dd_id, round=current_round),
        dd_history=dd_history,
        goal_version=goal_version,
        last_seq=last_seq,
        warnings=warnings,
        terminal=terminal,
    )


@dataclass
class ResumePoint:
    """protocol §11 的续跑点。"""

    action: str
    stage: str | None
    dd_id: str | None
    detail: str | None


def resume_point(events: Iterable[Event]) -> ResumePoint:
    events = list(events)
    if not events:
        return ResumePoint(action="exit", stage=None, dd_id=None, detail="no events")

    open_stage: str | None = None
    for ev in events:
        if ev.kind == "goal.turn.started":
            open_stage = "goal_turn"
        elif ev.kind == "goal.turn.finished":
            open_stage = None
        elif ev.kind == "dd.stage.started":
            open_stage = ev.payload.get("stage")
        elif ev.kind == "dd.stage.finished":
            open_stage = None

    last = events[-1]
    kind = last.kind
    payload = last.payload or {}
    dd_id = last.dd_id

    if kind in ("goal.done", "goal.blocked"):
        return ResumePoint(action="exit", stage=None, dd_id=dd_id, detail=kind)
    if kind == "engine.exiting" and payload.get("reason") in ("stop", "done", "blocked"):
        return ResumePoint(action="exit", stage=None, dd_id=dd_id, detail=payload.get("reason"))

    if kind in ("goal.turn.started", "dd.stage.started", "agent.spawned"):
        if kind == "goal.turn.started":
            stage = "goal_turn"
        elif kind == "dd.stage.started":
            stage = payload.get("stage")
        else:
            stage = open_stage
        return ResumePoint(
            action="restart_step", stage=stage, dd_id=dd_id, detail="lost_on_restart"
        )

    if kind == "dd.acceptance":
        return ResumePoint(action="rerun_acceptance", stage="acceptance", dd_id=dd_id, detail=None)

    stage = payload.get("stage") if kind.endswith(".finished") else None
    return ResumePoint(action="next_step", stage=stage, dd_id=dd_id, detail=None)
