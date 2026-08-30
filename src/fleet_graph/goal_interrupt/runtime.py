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
from pathlib import Path
from typing import Any

from langgraph.types import Command

from fleet_graph.bus.board import (
    goal_line_card_key,
    goal_line_card_payload,
    parked_question_key,
)
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
        run_id: str = "",
        stall_state_path: str | Path | None = None,
    ) -> None:
        self.folder_id = folder_id
        self._generation = int(generation)
        self.store = store
        self.board = board
        self.card_entity_id = card_entity_id
        self.run_id = run_id
        #: The scheduler's per-line stall-state file
        #: (``.scheduler/<folder_id>.json``). When provided, ``persist`` writes
        #: the interrupt's question note / card into the stall state's
        #: ``board_question_note_id`` / ``board_card_entity_id`` fields, so the
        #: decision bridge's ``LineOwnerSource`` (which only reads the stall
        #: state) can map a ``work.decision.v1`` answering this E2 question back
        #: to the parked line. None (tests, offline) keeps the old behaviour:
        #: only the goal-interrupt store is written.
        self.stall_state_path = Path(stall_state_path) if stall_state_path else None

    def generation(self) -> int:
        return self._generation

    def ask(self, round_no: int, blocker: str) -> tuple[str, str]:
        """Materialise the question, idempotently across a resume re-execution.

        A resume re-execution of the interrupt node re-enters ``ask``; it must
        answer with the *same* question note it persisted at suspension rather
        than publishing a second one. The stable lookup is the already-persisted
        interrupt checkpoint for ``(folder_id, generation, round_no)``.

        When a board is present the card and question reuse the scheduler's
        escalation idempotency keys (``goal-line-card:<folder>`` and the
        content-variant ``parked:<folder>:<run_id>:<variant>``) so the line's
        own question and the scheduler's parking escalation converge on one
        note -- a human answering it resumes the very interrupt that asked
        (spec items 1 and 5, and the one-resume-per-question property the
        otherwise-double question breaks). The variant folds the final note
        body into the key, so a changed body (blocker / round) is a new key
        (never a 409) and an unchanged body stays the same key (dedup).

        The card is published through the *shared* constructor and the *shared*
        key the scheduler daemon uses, so the two producers converge on one card
        per line: a passed-in ``card_entity_id`` (the scheduler's card, threaded
        through ``--board-card``) is reused, and a first ask with none falls back
        to publishing through the same constructor/key and adopts the returned
        entity id -- including a ``deduplicated=True`` result from a concurrent
        first-create. The title collapses to ``folder_id`` on both producers (the
        design's sanctioned alternative to threading the alias), so the payload is
        byte-identical even when the alias never reaches the line process.
        """
        existing = self.store.checkpoint_for_round(self.folder_id, self._generation, round_no)
        if existing is not None and existing.get("question_note_id"):
            card_entity_id = str(existing.get("card_entity_id") or "")
            if card_entity_id:
                self.card_entity_id = card_entity_id
            return str(existing["question_note_id"]), self.card_entity_id

        if self.board is None:
            # No board surface: derive a deterministic, stable question id from
            # the resume identity so tests and offline resumes stay reproducible.
            idempotency_key = f"e2-question:{self.folder_id}:{self._generation}:{round_no}"
            return f"{idempotency_key}:q", self.card_entity_id

        card_entity_id = self.card_entity_id
        if not card_entity_id:
            card = self.board.publish_card(
                goal_line_card_payload(folder_id=self.folder_id, title=self.folder_id),
                idempotency_key=goal_line_card_key(self.folder_id),
            )
            card_entity_id = card.entity_id
            self.card_entity_id = card_entity_id

        # The scheduler's escalation key: ``parked:<folder_id>:<run_id>`` with a
        # content-variant suffix derived from the note body (blocker / round_no),
        # so a re-park or retry that changes the note never reuses a key with a
        # different intent (agent-bus 409 IDEMPOTENCY_CONFLICT). When this line's
        # run id is threaded through (production), the line's own ask and the
        # scheduler's later escalation share one question note. A line whose run
        # id is unknown (tests, offline) keeps the stable generation/round key so
        # the re-ask still idempotently re-finds itself.
        question = f"line {self.folder_id} waiting on a human decision (round {round_no})."
        question_key = (
            parked_question_key(folder_id=self.folder_id, run_id=self.run_id, note_text=question)
            if self.run_id
            else f"e2-question:{self.folder_id}:{self._generation}:{round_no}"
        )
        ticket = self.board.ask(
            card_entity_id=card_entity_id,
            question=question,
            idempotency_key=question_key,
        )
        return ticket.question_note_id, card_entity_id

    def persist(self, checkpoint: InterruptCheckpoint) -> None:
        self.store.put_interrupt(checkpoint.as_dict())
        self._sync_scheduler_stall(checkpoint)

    def _sync_scheduler_stall(self, checkpoint: InterruptCheckpoint) -> None:
        """Write-side convergence: mirror the interrupt's board facts into the
        scheduler's stall-state file.

        The decision bridge's ``LineOwnerSource`` discovers parked lines by
        reading *only* ``.scheduler/<folder_id>.json`` and mapping on
        ``board_question_note_id``. The E2 interrupt used to persist its
        question note to ``goal-interrupt.sqlite3`` alone, so a
        ``work.decision.v1`` answering it resolved nobody and the human's
        verdict was swallowed as ``no_waiting_owner`` (the #170 follow-up
        bug). This read-modify-write sets the stall state's board fields to the
        same question note / card the interrupt asked, idempotently, while
        preserving every other field the scheduler owns (streak, generation,
        parking snapshot). Fail-soft like the daemon's own stall write: the
        goal-interrupt store remains the durable source of truth, so a stall
        file that cannot be written never faults the suspension.
        """
        if self.stall_state_path is None:
            return
        path = self.stall_state_path
        try:
            raw = path.read_text(encoding="utf-8") if path.exists() else None
        except OSError:
            return
        state: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except (OSError, json.JSONDecodeError):
                return
            if isinstance(parsed, dict):
                state = parsed
        if checkpoint.question_note_id:
            state["board_question_note_id"] = checkpoint.question_note_id
        if checkpoint.card_entity_id:
            state["board_card_entity_id"] = checkpoint.card_entity_id
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        except OSError:
            pass

    def record_turn_result(self, turn_id: str, result: dict[str, Any]) -> None:
        self.store.record_turn_result(turn_id, result)

    def turn_result(self, turn_id: str) -> dict[str, Any] | None:
        return self.store.turn_result(turn_id)

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
