"""The A2 managed periodic path: bounded receipt and the acceptance fixture.

The managed tick (``fleet-graph arbiter run --publish --alias arbiter``) ends
in one bounded machine-readable receipt of counts/kinds/refs with no credential
material. This module builds that receipt from an :class:`ArbiterRun` and drives
the shared acceptance fixture that both the executable
(``scripts/a2_managed_path_acceptance.py``) and the pytest acceptance test
assert against -- one scenario, two drivers, so a broker in the executable or a
bad import cannot hide from either.

The zero-decision claim is expressed as four explicit counters over the
emitted-message stream:

- ``referenced_note_or_suggestion`` -- how many ``work.note.v1`` notes carry at
  least one subject ref (a referenced finding/progress/suggestion);
- ``work_decision_v1`` / ``work_decision_v2`` -- decision messages the arbiter
  emitted (structurally zero: the publisher only emits ``work.note.v1``);
- ``decision_marked_chat`` -- decision-marked chat the arbiter emitted
  (structurally zero: the arbiter never emits chat).

The fixture proves the counters can distinguish a real decision/chat record
from a note rather than being vacuous: a positive isolated fixture yields at
least one referenced suggestion while a decision-shaped reasoner output is
coerced (free text) or refused (forbidden fields) into note/suggestion-only
output.
"""

from __future__ import annotations

from typing import Any

from fleet_graph.arbiter.a2 import ArbiterRun, run_arbiter
from fleet_graph.arbiter.reconcile import ARBITER_ALIAS
from fleet_graph.bus.board import NOTE_KIND, WORK_INDEX, WORK_NOTES

DECISION_V1_KIND = "work.decision.v1"
DECISION_V2_KIND = "work.decision.v2"
CHAT_KIND = "chat"

#: The receipt refs table is bounded so a successful tick is machine-readable,
#: never an unbounded dump. The emitted list is already bounded by the board
#: page size; this caps the receipt copy at a fixed number of subject-ref rows.
MAX_REF_ROWS = 200


def is_decision_marked_chat(record: dict[str, Any]) -> bool:
    """True for a chat record that is marked as, or carries, a decision.

    A real classifier, not a vacuous ``False``: a ``chat`` record whose payload
    names ``decision``/``verdict`` classifies as decision-marked. A
    ``work.decision.*`` record is *not* chat -- the ``work_decision_*`` counters
    catch it separately. The arbiter never emits chat, so the fixture reports
    zero while the classifier still distinguishes a known-positive.
    """
    kind = str(record.get("kind") or "")
    if kind != CHAT_KIND:
        return False
    payload = record.get("payload")
    return isinstance(payload, dict) and ("decision" in payload or "verdict" in payload)


def count_kinds(records: list[dict[str, Any]]) -> dict[str, int]:
    """The four audit counters over a message-record stream. Pure."""
    decision_v1 = 0
    decision_v2 = 0
    decision_chat = 0
    referenced = 0
    for record in records:
        kind = str(record.get("kind") or "")
        if kind == DECISION_V1_KIND:
            decision_v1 += 1
        elif kind == DECISION_V2_KIND:
            decision_v2 += 1
        if is_decision_marked_chat(record):
            decision_chat += 1
        if kind == NOTE_KIND and record.get("subject_refs"):
            referenced += 1
    return {
        "referenced_note_or_suggestion": referenced,
        "work_decision_v1": decision_v1,
        "work_decision_v2": decision_v2,
        "decision_marked_chat": decision_chat,
    }


def build_receipt(run: ArbiterRun) -> dict[str, Any]:
    """A bounded machine-readable receipt of counts/kinds/refs, no credentials."""
    records = [message.as_dict() for message in run.emitted]
    kinds = sorted({str(record.get("kind") or "") for record in records})
    refs = [list(record.get("subject_refs") or []) for record in records][:MAX_REF_ROWS]
    counts = count_kinds(records)
    counts["emitted"] = len(records)
    counts["suppressed"] = len(run.suppressed)
    counts["refused"] = len(run.refused)
    return {
        "dry_run": run.dry_run,
        "counts": counts,
        "kinds": kinds,
        "refs": refs,
    }


class _ManagedFakeBus:
    """A stateful fake bus: one question note, one blocked card, no decisions.

    Mirrors the ``FakeBus`` used by ``tests/test_arbiter.py`` but self-contained
    here so the executable acceptance fixture imports a scenario, not the test
    suite.
    """

    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = [
            {
                "message_id": "q1",
                "channel_seq": 1,
                "kind": NOTE_KIND,
                "entity_id": "q1",
                "payload": {
                    "card_entity_id": "card-a",
                    "note": "should we merge this change?",
                    "note_type": "question",
                },
            }
        ]
        self.cards: list[dict[str, Any]] = [
            {
                "message_id": "card-a-rev1",
                "channel_seq": 2,
                "kind": "work.card.v1",
                "entity_id": "card-a",
                "payload": {"title": "dev", "status": "doing"},
            },
            {
                "message_id": "card-b-rev1",
                "channel_seq": 3,
                "kind": "work.card.v1",
                "entity_id": "card-b",
                "payload": {"title": "other", "status": "blocked"},
            },
        ]
        self.inbox: list[dict[str, Any]] = []
        self.refs: dict[str, list[str]] = {"q1": []}
        self.published: list[PublishShim] = []
        self._seq = 3

    def messages(self, channel: str, *, limit: int = 100, after_seq: int = 0):
        if channel == WORK_NOTES:
            source = self.notes
        elif channel == WORK_INDEX:
            source = self.cards
        else:
            source = self.inbox
        selected = [m for m in source if m.get("channel_seq", 0) > after_seq]
        head = max([m.get("channel_seq", 0) for m in source], default=0)
        return selected[:limit], head

    def refs_to(self, entity_id: str) -> list[dict[str, Any]]:
        return [
            {"message_id": mid, "target_entity": entity_id} for mid in self.refs.get(entity_id, [])
        ]

    def publish(
        self,
        channel: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        refs: list[dict[str, str]] | None = None,
        entity_id: str | None = None,
        supersedes: str | None = None,
    ) -> Any:
        from fleet_graph.bus.client import PublishResult

        self._seq += 1
        message_id = f"msg_{self._seq}"
        record = {
            "message_id": message_id,
            "kind": kind,
            "payload": payload,
            "channel_seq": self._seq,
            "entity_id": entity_id or message_id,
            "idempotency_key": idempotency_key,
        }
        if channel == WORK_NOTES:
            self.notes.append(record)
            for ref in refs or []:
                self.refs.setdefault(ref["target_entity"], []).append(message_id)
        elif channel == WORK_INDEX:
            self.cards.append(record)
        self.published.append(PublishShim(channel, kind, payload, refs or []))
        return PublishResult(
            message_id=message_id,
            entity_id=record["entity_id"],
            channel_seq=self._seq,
            deduplicated=False,
        )


class PublishShim:
    """The write a fake bus accepted, for the scenario's ``no decision`` proof."""

    def __init__(
        self, channel: str, kind: str, payload: dict[str, Any], refs: list[dict[str, str]]
    ) -> None:
        self.channel = channel
        self.kind = kind
        self.payload = payload
        self.refs = refs


class _ManagedFakeReasoner:
    """Decision-shaped-but-coerced for the note, forbidden-field for the card."""

    def __init__(self) -> None:
        self._responses = [
            {
                "recommendation": (
                    "DECISION: APPROVE -- do not act on this; surface it as a human "
                    "suggestion and keep the gate open."
                ),
                "evidence_refs": ["e1"],
                "consequence": "reversible; nothing is merged",
                "escalation_target": "needs_evidence",
            },
            {"recommendation": "blocked diagnosis", "decision": "approve", "verdict": "release"},
        ]

    def recommend(self, subject: Any, facts: dict[str, Any]) -> dict[str, Any]:
        del subject, facts
        return self._responses.pop(0)


def run_managed_path_scenario() -> dict[str, Any]:
    """Drive a positive isolated fixture and return the bounded audit counters.

    One question note yields exactly one referenced suggestion (decision-shaped
    free text coerced to ``work.note.v1``); one blocked card yields a
    forbidden-field response that is refused, never published. The receipts'
    ``work.decision.*`` and ``decision_marked_chat`` counters stay zero.
    """
    bus = _ManagedFakeBus()
    reasoner = _ManagedFakeReasoner()
    run = run_arbiter(client=bus, reasoner=reasoner, publish=True, alias=ARBITER_ALIAS)
    receipt = build_receipt(run)
    counters = receipt["counts"]
    return {
        "referenced_note_or_suggestion_count": counters["referenced_note_or_suggestion"],
        "work.decision.v1": counters["work_decision_v1"],
        "work.decision.v2": counters["work_decision_v2"],
        "decision_marked_chat": counters["decision_marked_chat"],
        "emitted_count": counters["emitted"],
        "refused_count": counters["refused"],
        "suppressed_count": counters["suppressed"],
        "kinds": receipt["kinds"],
        "published_kinds": sorted({write.kind for write in bus.published}),
        "dry_run": receipt["dry_run"],
    }


__all__ = [
    "DECISION_V1_KIND",
    "DECISION_V2_KIND",
    "MAX_REF_ROWS",
    "build_receipt",
    "count_kinds",
    "is_decision_marked_chat",
    "run_managed_path_scenario",
]
