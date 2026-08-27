"""The closed vocabulary of facts that wake the supervisor graph.

Four events, each a mechanical signal no supervised party controls
(r4-design §1). The vocabulary is closed on purpose and unknown names are
rejected out loud rather than mapped onto a neighbour -- the old supervisor's
events.py refused v0 names explicitly, and that discipline is inherited here:
a silently-mapped event is an audit that runs against the wrong facts.

This module deliberately imports nothing from `fleet_graph.scheduler`. The
observer that *emits* these events lives on the scheduler's side and may hold
a launcher; the graph that consumes them must not be able to reach ignition
or launching at all (guarded by scripts/check_supervisor_conformance.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: E1 -- a question note on the board that no `work.decision.v1` references.
EVENT_BOARD_QUESTION = "board_question"
#: E2 -- a line's terminal is `blocked` with `waiting_on: "decision"`.
EVENT_BLOCKED_DECISION = "blocked_decision"
#: E3 -- a line's terminal is `fault`, or its pump declared a fault.
EVENT_LINE_FAULT = "line_fault"
#: E4 -- the scheduler's global breaker tripped (TOTAL_CAP_REACHED).
EVENT_CAP_BREAKER = "cap_breaker"

EVENT_TYPES = frozenset(
    {
        EVENT_BOARD_QUESTION,
        EVENT_BLOCKED_DECISION,
        EVENT_LINE_FAULT,
        EVENT_CAP_BREAKER,
    }
)

_KEY_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class SupervisorEventError(ValueError):
    """The event is not one this graph knows how to audit. Refused, not mapped."""


def sanitize_key(raw: str) -> str:
    """A dedup key that is safe as a systemd unit suffix and a file name."""
    cleaned = _KEY_SAFE.sub("-", str(raw).strip())
    return cleaned[:120] or "empty"


@dataclass(frozen=True)
class SupervisorEvent:
    """One fact worth an audit, plus the identity that makes retries idempotent.

    `key` is the dedup key from r4-design §1's table, prefixed with the event
    number so the four keyspaces cannot collide. `thread_id` derives from it,
    which is what makes a kill-restart of the same event re-adopt its in-flight
    audit run instead of dispatching a second one.
    """

    type: str
    key: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def thread_id(self) -> str:
        return f"supervisor:{self.key}"

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "key": self.key, "payload": dict(self.payload)}


def validate_event(raw: dict[str, Any]) -> SupervisorEvent:
    """Parse and validate one event dict. Unknown names are an error, loudly."""
    if not isinstance(raw, dict):
        raise SupervisorEventError(f"event must be an object, got {type(raw).__name__}")
    kind = raw.get("type")
    if kind not in EVENT_TYPES:
        raise SupervisorEventError(
            f"unknown event type {kind!r}; the vocabulary is closed: "
            f"{sorted(EVENT_TYPES)}. Unknown names are refused, never mapped."
        )
    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        raise SupervisorEventError("event.key must be a non-empty string")
    if key != sanitize_key(key):
        raise SupervisorEventError(
            f"event.key {key!r} is not file/unit-safe; expected {sanitize_key(key)!r}"
        )
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        raise SupervisorEventError("event.payload must be an object")
    return SupervisorEvent(type=str(kind), key=key, payload=payload)


# --- constructors -----------------------------------------------------------
#
# The observer builds events only through these, so the key shape is defined
# once. E2/E3 key on the run id that wrote the terminal (the engine's own
# pump wrote it -- not agent prose); E1 on the question note's message id;
# E4 on the cap window's time bucket, one audit per window.


def board_question_event(question_note_id: str, card_entity_id: str) -> SupervisorEvent:
    return SupervisorEvent(
        type=EVENT_BOARD_QUESTION,
        key=sanitize_key(f"e1-{question_note_id}"),
        payload={"question_note_id": question_note_id, "card_entity_id": card_entity_id},
    )


def blocked_decision_event(folder_id: str, run_id: str) -> SupervisorEvent:
    return SupervisorEvent(
        type=EVENT_BLOCKED_DECISION,
        key=sanitize_key(f"e2-{run_id}"),
        payload={"folder_id": folder_id, "run_id": run_id},
    )


def line_fault_event(folder_id: str, run_id: str) -> SupervisorEvent:
    return SupervisorEvent(
        type=EVENT_LINE_FAULT,
        key=sanitize_key(f"e3-{run_id}"),
        payload={"folder_id": folder_id, "run_id": run_id},
    )


def cap_breaker_event(bucket: int, detail: str, folder_ids: list[str]) -> SupervisorEvent:
    return SupervisorEvent(
        type=EVENT_CAP_BREAKER,
        key=sanitize_key(f"e4-cap-{bucket}"),
        payload={"bucket": bucket, "detail": detail, "folder_ids": folder_ids},
    )


__all__ = [
    "EVENT_BLOCKED_DECISION",
    "EVENT_BOARD_QUESTION",
    "EVENT_CAP_BREAKER",
    "EVENT_LINE_FAULT",
    "EVENT_TYPES",
    "SupervisorEvent",
    "SupervisorEventError",
    "blocked_decision_event",
    "board_question_event",
    "cap_breaker_event",
    "line_fault_event",
    "sanitize_key",
    "validate_event",
]
