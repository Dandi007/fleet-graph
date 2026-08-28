"""The line-side runtime of the E2 interrupt: the concrete port and the resume.

The graph node in ``goal_line.py`` talks to :class:`DecisionInterruptPort`; this
module supplies the production implementation and the single entry that resumes
a suspended line with a validated :class:`DecisionInput`.

The resume is deliberately split from the bridge: the bridge decides *which*
decision resumes *which* line and constructs the immutable input; this module
only re-enters the line's own checkpoint at the exact interrupt and injects that
input -- it never casts a vote, never reads the verdict out of prose, and never
advances a generation.
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.types import Command

from fleet_graph.goal_interrupt.contract import (
    DecisionInput,
    DecisionRef,
    InterruptCheckpoint,
    resume_key_for,
)
from fleet_graph.goal_interrupt.store import GoalInterruptStore

RESUME_STATUS_RESUMED = "resumed"
RESUME_STATUS_ALREADY = "already_resumed"


class LineInterruptPort:
    """The production ``DecisionInterruptPort``: store-backed, board-optional.

    ``ask`` materialises a question note through the board with a stable
    idempotency key, so a resume re-execution of the interrupt node re-asks the
    same question rather than posting a duplicate. ``persist`` and
    ``load_resume`` go through the durable store, and ``claim_turn`` guards the
    per-turn usage ledger so a duplicate delivery cannot charge a second model
    invocation (spec item 8).
    """

    def __init__(
        self,
        *,
        folder_id: str,
        generation: int,
        store: GoalInterruptStore,
        board: Any = None,
        card_entity_id: str = "",
    ) -> None:
        self.folder_id = folder_id
        self._generation = int(generation)
        self.store = store
        self.board = board
        self.card_entity_id = card_entity_id

    def generation(self) -> int:
        return self._generation

    def ask(self, round_no: int, blocker: str) -> tuple[str, str]:
        """Materialise the question, idempotently across a resume re-execution."""
        idempotency_key = f"e2-question:{self.folder_id}:{self._generation}:{round_no}"
        if self.board is None:
            # No board surface: derive a deterministic, stable question id from
            # the resume identity so tests and offline resumes stay reproducible.
            return f"{idempotency_key}:q", self.card_entity_id
        card_entity_id = self.card_entity_id
        if not card_entity_id:
            card = self.board.publish_card(
                {
                    "title": self.folder_id,
                    "status": "doing",
                    "intent": f"goal-line decision interrupt for {self.folder_id}",
                },
                idempotency_key=f"e2-card:{self.folder_id}",
            )
            card_entity_id = card.entity_id
            self.card_entity_id = card_entity_id
        ticket = self.board.ask(
            card_entity_id=card_entity_id,
            question=f"line {self.folder_id} waiting on a human decision (round {round_no}).",
            idempotency_key=idempotency_key,
        )
        return ticket.question_note_id, card_entity_id

    def persist(self, checkpoint: InterruptCheckpoint) -> None:
        self.store.put_interrupt(checkpoint.as_dict())

    def load_resume(self, resume_key: str) -> DecisionInput | None:
        receipt = self.store.resume_receipt(resume_key)
        if receipt is None:
            return None

        refs = json.loads(receipt.get("refs") or "[]")
        return DecisionInput(
            message_id=str(receipt["message_id"]),
            channel_seq=int(receipt["channel_seq"]),
            decision=str(receipt["decision"]),
            rationale=str(receipt.get("rationale") or ""),
            decided_by=str(receipt.get("decided_by") or ""),
            question_note_id=str(receipt["question_note_id"]),
            card_entity_id=str(receipt.get("card_entity_id") or ""),
            refs=tuple(
                DecisionRef(
                    message_id=str(ref.get("message_id") or ""),
                    target_entity=str(ref.get("target_entity") or ""),
                )
                for ref in refs
            ),
            decided_at=str(receipt.get("decided_at") or ""),
            resume_key=resume_key,
        )

    def claim_turn(self, turn_id: str) -> bool:
        return self.store.claim_turn(turn_id)


def resume_key_for_interrupt(folder_id: str, generation: int, question_note_id: str) -> str:
    """Re-exported convenience: the resume_key of one suspended question."""
    return resume_key_for(folder_id, generation, question_note_id)


def is_suspended(compiled: Any, config: dict[str, Any], node: str = "decision_interrupt") -> bool:
    """Is this thread still sitting on an interrupt, ready to resume?"""
    snapshot = compiled.get_state(config)
    if not snapshot.next:
        return False
    if node in snapshot.next:
        return True
    values = getattr(snapshot, "values", {}) or {}
    return bool(values.get("__interrupt__"))


def resume_line(
    compiled: Any,
    *,
    config: dict[str, Any],
    decision: DecisionInput,
    store: GoalInterruptStore,
) -> tuple[dict[str, Any], str]:
    """Resume one suspended line with a validated decision, exactly once.

    Records the resume receipt (one resumed envelope per ``resume_key``) before
    the model is re-invoked, so a duplicate delivery of the same decision --
    whether from cursor compensation, a republish, or a crash-and-restart --
    is deduplicated at the envelope and re-adopts the continuation rather than
    re-invoking it. Returns ``(state, status)``: ``resumed`` when this call
    moved the line forward, ``already_resumed`` when the thread had already
    completed past the interrupt.
    """
    store.record_resume(decision.as_dict())

    if not is_suspended(compiled, config):
        state = compiled.get_state(config)
        return dict(state.values or {}), RESUME_STATUS_ALREADY

    state = compiled.invoke(
        Command(resume={"resume_key": decision.resume_key}),
        config=config,
    )
    return state, RESUME_STATUS_RESUMED


__all__ = [
    "RESUME_STATUS_ALREADY",
    "RESUME_STATUS_RESUMED",
    "LineInterruptPort",
    "is_suspended",
    "resume_key_for_interrupt",
    "resume_line",
]
