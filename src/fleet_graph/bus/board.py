"""The work board: cards, notes, and the human gate.

One structural rule shapes this module: **there is no method here that
publishes a `work.decision.v1`.** Verdicts are the human's to cast, and the
cheapest way to guarantee an agent never casts one is to give it no way to. If
you find yourself wanting to add one, that is the bug.

What an agent *may* do is ask (a `question` note), report (`progress` /
`evidence` / `finding` notes), and claim or advance a card (a `work.card.v1`
revision, CAS-guarded on the entity head).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fleet_graph.bus.client import BusClient, BusConflict, PublishResult

WORK_INDEX = "board:work-index"
WORK_NOTES = "board:work-notes"

CARD_KIND = "work.card.v1"
NOTE_KIND = "work.note.v1"
DECISION_KIND = "work.decision.v1"

NoteType = str  # "progress" | "evidence" | "finding" | "question"


@dataclass(frozen=True)
class Decision:
    """A human verdict answering one question note."""

    message_id: str
    decision: str
    decided_by: str
    question: str
    rationale: str
    card_entity_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class GateTicket:
    """A question that is waiting on a human. Cheap to checkpoint."""

    question_note_id: str
    card_entity_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "question_note_id": self.question_note_id,
            "card_entity_id": self.card_entity_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> GateTicket:
        return cls(
            question_note_id=data["question_note_id"],
            card_entity_id=data["card_entity_id"],
        )


class Board:
    def __init__(
        self,
        client: BusClient,
        *,
        index_channel: str = WORK_INDEX,
        notes_channel: str = WORK_NOTES,
        observability_channel: str | None = None,
    ) -> None:
        self.client = client
        self.index_channel = index_channel
        self.notes_channel = notes_channel
        self.observability_channel = observability_channel

    # --- cards -----------------------------------------------------------

    def publish_card(self, payload: dict[str, Any], idempotency_key: str) -> PublishResult:
        return self.client.publish(self.index_channel, CARD_KIND, payload, idempotency_key)

    def revise_card(
        self,
        *,
        entity_id: str,
        supersedes: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> PublishResult:
        """Publish a new revision of a card.

        Raises BusConflict when `supersedes` is no longer the head -- someone
        claimed or advanced the card first. Re-read with `card_head` and decide
        again rather than clobbering their revision.
        """
        return self.client.publish(
            self.index_channel,
            CARD_KIND,
            payload,
            idempotency_key,
            entity_id=entity_id,
            supersedes=supersedes,
        )

    def card_head(self, entity_id: str) -> dict[str, Any] | None:
        """The newest revision of one card, by channel order."""
        messages, _ = self.client.messages(self.index_channel, limit=1000)
        revisions = [
            m for m in messages if m.get("entity_id") == entity_id and m.get("kind") == CARD_KIND
        ]
        if not revisions:
            return None
        return max(revisions, key=lambda m: m["channel_seq"])

    # --- notes -----------------------------------------------------------

    def note(
        self,
        *,
        card_entity_id: str,
        text: str,
        note_type: NoteType,
        idempotency_key: str,
    ) -> PublishResult:
        return self.client.publish(
            self.notes_channel,
            NOTE_KIND,
            {"card_entity_id": card_entity_id, "note": text, "note_type": note_type},
            idempotency_key,
            refs=[{"target_entity": card_entity_id}],
        )

    def evidence(self, *, card_entity_id: str, text: str, idempotency_key: str) -> PublishResult:
        return self.note(
            card_entity_id=card_entity_id,
            text=text,
            note_type="evidence",
            idempotency_key=idempotency_key,
        )

    def progress(self, *, card_entity_id: str, text: str, idempotency_key: str) -> PublishResult:
        return self.note(
            card_entity_id=card_entity_id,
            text=text,
            note_type="progress",
            idempotency_key=idempotency_key,
        )

    # --- human gate ------------------------------------------------------

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> GateTicket:
        """Raise a question for a human and return a ticket to wait on.

        The ticket is the whole state: checkpoint it, and a restarted graph can
        resume waiting without re-asking. The idempotency key is what stops a
        retry from posting the same question twice.
        """
        result = self.note(
            card_entity_id=card_entity_id,
            text=question,
            note_type="question",
            idempotency_key=idempotency_key,
        )
        return GateTicket(question_note_id=result.message_id, card_entity_id=card_entity_id)

    def decision_for(self, ticket: GateTicket) -> Decision | None:
        """The verdict answering this question, or None while it is still open.

        Resolution goes through the ref graph rather than text matching: a
        decision is an answer to *this* question only if it references it.
        """
        referencing = self.client.refs_to(ticket.question_note_id)
        if not referencing:
            return None
        candidate_ids = {ref["message_id"] for ref in referencing}

        messages, _ = self.client.messages(self.notes_channel, limit=1000)
        decisions = [
            m
            for m in messages
            if m["message_id"] in candidate_ids and m.get("kind") == DECISION_KIND
        ]
        if not decisions:
            return None
        newest = max(decisions, key=lambda m: m["channel_seq"])
        payload = newest.get("payload", {})
        return Decision(
            message_id=newest["message_id"],
            decision=str(payload.get("decision", "")),
            decided_by=str(payload.get("decided_by", "")),
            question=str(payload.get("question", "")),
            rationale=str(payload.get("rationale", "")),
            card_entity_id=str(payload.get("card_entity_id", ticket.card_entity_id)),
            raw=newest,
        )

    # --- observability ---------------------------------------------------

    def observe(self, event: dict[str, Any], idempotency_key: str) -> None:
        """Best-effort telemetry to the bypass channel.

        Inherited from the pump (findings-recon 3a): the observability write
        must never be able to stall or fail the work it is observing. Every
        error here is swallowed on purpose.
        """
        if not self.observability_channel:
            return
        try:
            self.client.publish(self.observability_channel, "gd.event.v1", event, idempotency_key)
        except Exception:
            # Swallowed on purpose: see docstring. Telemetry must not bite.
            return


__all__ = [
    "Board",
    "BusConflict",
    "Decision",
    "GateTicket",
]
