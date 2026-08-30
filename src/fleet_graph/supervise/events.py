"""The closed vocabulary of facts that wake the supervisor graph.

Seven events, each a mechanical signal no supervised party controls
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
#: E5 -- a development passed its gate but its product commit has not landed
#: on the default branch (read-model /v1/harvestable; no direct file scan).
EVENT_APPROVED_UNHARVESTED = "approved_unharvested"
#: E6 -- a line's heartbeat is stale past the threshold (read-model /v1/lines).
EVENT_HEARTBEAT_STALE = "heartbeat_stale"
#: E7 -- a decision was swallowed (read-model /v1/decisions).
EVENT_DECISION_SWALLOWED = "decision_swallowed"

EVENT_TYPES = frozenset(
    {
        EVENT_BOARD_QUESTION,
        EVENT_BLOCKED_DECISION,
        EVENT_LINE_FAULT,
        EVENT_CAP_BREAKER,
        EVENT_APPROVED_UNHARVESTED,
        EVENT_HEARTBEAT_STALE,
        EVENT_DECISION_SWALLOWED,
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
    number so the four keyspaces cannot collide. `thread_id` derives from it
    *and* from `attempt` -- the generation semantics the ronin lines already
    have (`{folder_id}:g{n}`), ported here after a production night of sqlite
    surgery: a re-run is a new attempt with a fresh checkpoint thread, never
    a knife fight with the old thread's rows. Within one attempt the identity
    is stable, so a kill-restart of the same launch still re-adopts its
    in-flight audit run instead of dispatching a second one.

    `attempt` is the observer's per-key lifetime launch counter (the cursor's
    `attempts` value at launch time), starting at 1. Old-format threads
    (`supervisor:{key}`, no suffix) are simply abandoned in the shared
    checkpoint db -- new launches never resolve to them, so no migration.
    """

    type: str
    key: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempt: int = 1

    @property
    def thread_id(self) -> str:
        return f"supervisor:{self.key}:a{self.attempt}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "key": self.key,
            "payload": dict(self.payload),
            "attempt": self.attempt,
        }


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
    attempt = raw.get("attempt", 1)
    # bool is an int subclass; refuse it explicitly rather than minting a
    # thread called ...:aTrue.
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise SupervisorEventError(f"event.attempt must be an integer >= 1, got {attempt!r}")
    return SupervisorEvent(type=str(kind), key=key, payload=payload, attempt=attempt)


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


def approved_unharvested_event(
    development_id: str, head_commit: str, stage: str
) -> SupervisorEvent:
    return SupervisorEvent(
        type=EVENT_APPROVED_UNHARVESTED,
        key=sanitize_key(f"e5-{development_id}"),
        payload={"development_id": development_id, "head_commit": head_commit, "stage": stage},
    )


def heartbeat_stale_event(
    folder_id: str, heartbeat_age_s: float, round: Any, phase: str
) -> SupervisorEvent:
    return SupervisorEvent(
        type=EVENT_HEARTBEAT_STALE,
        key=sanitize_key(f"e6-{folder_id}"),
        payload={
            "folder_id": folder_id,
            "heartbeat_age_s": heartbeat_age_s,
            "round": round,
            "phase": phase,
        },
    )


def decision_swallowed_event(source_message_id: str, reason: str) -> SupervisorEvent:
    return SupervisorEvent(
        type=EVENT_DECISION_SWALLOWED,
        key=sanitize_key(f"e7-{source_message_id}"),
        payload={"source_message_id": source_message_id, "reason": reason},
    )


__all__ = [
    "EVENT_APPROVED_UNHARVESTED",
    "EVENT_BLOCKED_DECISION",
    "EVENT_BOARD_QUESTION",
    "EVENT_CAP_BREAKER",
    "EVENT_DECISION_SWALLOWED",
    "EVENT_HEARTBEAT_STALE",
    "EVENT_LINE_FAULT",
    "EVENT_TYPES",
    "SupervisorEvent",
    "SupervisorEventError",
    "approved_unharvested_event",
    "blocked_decision_event",
    "board_question_event",
    "cap_breaker_event",
    "decision_swallowed_event",
    "heartbeat_stale_event",
    "line_fault_event",
    "sanitize_key",
    "validate_event",
]
