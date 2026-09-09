"""E2: the durable goal-interrupt package, at the package surface.

These tests pin the load-bearing properties of ``fleet_graph.goal_interrupt``
using the real durable surfaces -- a real ``GoalInterruptStore`` (SQLite, WAL,
fail-closed) and the real resolver/bridge -- with only the bus at the edge
faked:

- the interrupt contract is atomic and immutable: ``resume_key`` shape, the
  checkpoint fields, and the ``DecisionInput`` envelope;
- the store is idempotent: one resume per ``resume_key``, one charge per
  ``turn_id``, monotonic cursor, no-rollback compensation;
- the runtime port reuses the scheduler's card/question idempotency keys;
- the resolver picks the newest decision by ``(channel_seq, message_id)`` and
  the legacy-owner fallback resolves exactly one owner or refuses loudly;
- the resident bridge reads a decision from behind the cursor and drives a
  validated resume without rolling back.

The in-graph interrupt integration (``graphs/goal_line.py``) and the decision
bridge's legacy-owner fallback were decoupled in dd-41-3; their tests were
removed there.
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fleet_graph.bus.board import parked_question_key
from fleet_graph.decision_bridge.owners import OwnerTarget
from fleet_graph.goal_interrupt.bridge import GoalInterruptBridge, GoalInterruptBridgeConfig
from fleet_graph.goal_interrupt.contract import (
    NO_PRIOR_TERMINAL_DIGEST,
    DecisionInput,
    DecisionRef,
    InterruptCheckpoint,
    prior_terminal_digest,
    resume_key_for,
)
from fleet_graph.goal_interrupt.resolver import (
    LEGACY_OUTCOME_AMBIGUOUS,
    LEGACY_OUTCOME_RESOLVED,
    compensate_decision,
    decision_input_from_message,
    legacy_owner_fallback,
    newest_decision,
)
from fleet_graph.goal_interrupt.runtime import LineInterruptPort
from fleet_graph.goal_interrupt.store import GoalInterruptStore

# --- fakes ------------------------------------------------------------------


class FakeBus:
    """A board that serves ``messages`` + ``refs_to`` as the real bus does.

    ``messages()`` returns the full channel (no inline refs); ``refs_to`` serves
    the reverse references so a decision's question is discovered the way the
    resolver/board expects.
    """

    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages_list: list[dict[str, Any]] = messages or []
        self.refs: dict[str, list[str]] = {}

    def link(self, question_id: str, message_id: str) -> None:
        self.refs.setdefault(question_id, []).append(message_id)

    def messages(self, channel: str, *, limit: int = 100, after_seq: int = 0):
        selected = [m for m in self.messages_list if int(m["channel_seq"]) > after_seq][:limit]
        head = max((int(m["channel_seq"]) for m in self.messages_list), default=0)
        return selected, head

    def refs_to(self, entity_id: str) -> list[dict[str, Any]]:
        return [
            {"message_id": mid, "target_entity": entity_id} for mid in self.refs.get(entity_id, [])
        ]


def decision(message_id: str, seq: int, *, question: str = "q-1") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "channel_seq": seq,
        "kind": "work.decision.v1",
        "created_at": "2026-08-29T00:00:00Z",
        "payload": {
            "decision": "APPROVE",
            "rationale": "looks good",
            "decided_by": "human",
            "card_entity_id": "card-1",
        },
    }


QUESTION_ID = "e2-question:wf-1:1:1:q"
RESUME_KEY = resume_key_for("wf-1", 1, QUESTION_ID)


def a_decision(message_id: str = "d-1", *, question: str = QUESTION_ID) -> DecisionInput:
    return DecisionInput(
        message_id=message_id,
        channel_seq=1,
        decision="APPROVE",
        rationale="r",
        decided_by="human",
        question_note_id=question,
        card_entity_id="card-1",
        refs=(DecisionRef(message_id, question),),
        decided_at="2026-08-29T00:00:00Z",
        resume_key=resume_key_for("wf-1", 1, question),
    )


# --- contract ---------------------------------------------------------------


class TestContract:
    def test_resume_key_shape(self) -> None:
        assert resume_key_for("wf-1", 2, "q-9") == "e2:wf-1:2:q-9"

    def test_no_prior_terminal_digest_is_distinct_from_empty(self) -> None:
        assert prior_terminal_digest(None) == NO_PRIOR_TERMINAL_DIGEST
        assert prior_terminal_digest({}) != NO_PRIOR_TERMINAL_DIGEST
        assert prior_terminal_digest({}) != prior_terminal_digest({"a": 1})

    def test_prior_terminal_digest_is_reproducible_across_key_order(self) -> None:
        assert prior_terminal_digest({"b": 1, "a": 2}) == prior_terminal_digest({"a": 2, "b": 1})

    def test_decision_input_is_immutable(self) -> None:
        decision = a_decision()
        with pytest.raises(FrozenInstanceError):
            decision.message_id = "other"  # type: ignore[misc]

    def test_decision_input_as_dict_has_the_exact_fields(self) -> None:
        payload = a_decision().as_dict()
        assert set(payload) == {
            "message_id",
            "channel_seq",
            "decision",
            "rationale",
            "decided_by",
            "question_note_id",
            "card_entity_id",
            "refs",
            "decided_at",
            "resume_key",
        }

    def test_interrupt_checkpoint_has_the_exact_fields(self) -> None:
        checkpoint = InterruptCheckpoint("wf-1", 1, 3, "q-1", "card-1", "digest", "key")
        assert set(checkpoint.as_dict()) == {
            "folder_id",
            "generation",
            "round_id",
            "question_note_id",
            "card_entity_id",
            "prior_terminal_digest",
            "resume_key",
        }


# --- store -------------------------------------------------------------------


class TestStore:
    def test_checkpoint_is_idempotent_per_resume_key(self, tmp_path: Path) -> None:
        store = GoalInterruptStore(tmp_path / "db").open()
        checkpoint = {
            "resume_key": RESUME_KEY,
            "folder_id": "wf-1",
            "generation": 1,
            "round_id": 1,
            "question_note_id": QUESTION_ID,
            "card_entity_id": "card-1",
            "prior_terminal_digest": "d",
        }
        assert store.put_interrupt(checkpoint) is True
        assert store.put_interrupt(checkpoint) is False  # re-state, not a new row
        assert store.interrupt(RESUME_KEY)["round_id"] == 1
        store.close()

    def test_resume_receipt_is_unique_per_resume_key(self, tmp_path: Path) -> None:
        store = GoalInterruptStore(tmp_path / "db").open()
        resume = a_decision().as_dict()
        assert store.record_resume(resume) is True
        assert store.record_resume(resume) is False  # duplicate delivery
        assert store.resume_receipt(RESUME_KEY)["message_id"] == "d-1"
        store.close()

    def test_charge_is_at_most_once_per_turn_id(self, tmp_path: Path) -> None:
        store = GoalInterruptStore(tmp_path / "db").open()
        assert store.claim_turn("turn-1") is True
        assert store.claim_turn("turn-1") is False
        assert store.turn_invocations("turn-1") == 1
        store.close()

    def test_compensation_never_rolls_back(self, tmp_path: Path) -> None:
        store = GoalInterruptStore(tmp_path / "db").open()
        assert store.record_compensation(RESUME_KEY, "d-2", 2) is True
        assert store.record_compensation(RESUME_KEY, "d-1", 1) is False  # older
        assert store.compensation_receipt(RESUME_KEY)["last_decision_message_id"] == "d-2"
        store.close()

    def test_cursor_is_monotonic(self, tmp_path: Path) -> None:
        store = GoalInterruptStore(tmp_path / "db").open()
        store.advance_cursor(5)
        store.advance_cursor(3)
        assert store.cursor() == 5
        store.close()


class FakeBoard:
    """A board seam over ``publish_card``/``ask`` idempotency.

    Idempotency keys resolve to stable entities, mirroring the real bus: asking
    twice under the same key returns the same card / question note."""

    def __init__(self) -> None:
        self.cards: dict[str, Any] = {}
        self.questions: dict[str, Any] = {}
        self.publishes: list[str] = []

    def publish_card(self, payload: dict[str, Any], idempotency_key: str) -> Any:
        if idempotency_key not in self.cards:
            self.cards[idempotency_key] = SimpleNamespace(
                entity_id=f"card-{idempotency_key}", payload=payload
            )
        self.publishes.append("card")
        return self.cards[idempotency_key]

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> Any:
        if idempotency_key not in self.questions:
            self.questions[idempotency_key] = SimpleNamespace(
                question_note_id=f"note-{idempotency_key}", card_entity_id=card_entity_id
            )
        self.publishes.append("question")
        return self.questions[idempotency_key]


class TestLineInterruptPortAsk:
    def test_ask_reuses_the_scheduler_escalation_keys(self, tmp_path: Path) -> None:
        """One question for one human-decision wait (spec item 1 + 5): the line's
        own ask and the scheduler's parking escalation must converge on the same
        card and question note, otherwise a human answering the escalation note
        cannot resume the interrupt."""
        store = GoalInterruptStore(tmp_path / "gi").open()
        board = FakeBoard()
        port = LineInterruptPort(
            folder_id="wf-1", generation=1, store=store, board=board, run_id="run-1"
        )

        question_note_id, card_entity_id = port.ask(1, "blocker")

        assert question_note_id == "note-" + parked_question_key(
            folder_id="wf-1",
            run_id="run-1",
            note_text="line wf-1 waiting on a human decision (round 1).",
        )
        assert card_entity_id == "card-goal-line-card:wf-1"
        assert "goal-line-card:wf-1" in board.cards
        assert (
            parked_question_key(
                folder_id="wf-1",
                run_id="run-1",
                note_text="line wf-1 waiting on a human decision (round 1).",
            )
            in board.questions
        )
        store.close()

    def test_ask_is_stable_across_a_resume_reexecution(self, tmp_path: Path) -> None:
        """A resume re-execution of the interrupt node re-asks but must not
        publish a second note: the persisted checkpoint is re-found by
        ``(folder_id, generation, round_id)``."""
        store = GoalInterruptStore(tmp_path / "gi").open()
        board = FakeBoard()
        port = LineInterruptPort(
            folder_id="wf-1", generation=1, store=store, board=board, run_id="run-1"
        )

        first_qid, first_card = port.ask(1, "blocker")
        port.persist(
            InterruptCheckpoint(
                folder_id="wf-1",
                generation=1,
                round_id=1,
                question_note_id=first_qid,
                card_entity_id=first_card,
                prior_terminal_digest="d",
                resume_key=resume_key_for("wf-1", 1, first_qid),
            )
        )

        second_qid, second_card = port.ask(1, "blocker")

        assert second_qid == first_qid
        assert second_card == first_card
        # The re-ask published nothing: same card, same question, one wake path.
        assert board.publishes == ["card", "question"]
        store.close()


class TestParkQuestionKeyContentVariant:
    """#170 write point: the runtime's ``ask`` must fold the note body into the
    ``parked:`` idempotency key so a changed round_no is a new key (never a
    same-key-different-intent 409) while an unchanged body stays the same key."""

    def test_changed_round_no_changes_the_key(self, tmp_path: Path) -> None:
        store = GoalInterruptStore(tmp_path / "gi").open()
        board = FakeBoard()
        port = LineInterruptPort(
            folder_id="wf-1", generation=1, store=store, board=board, run_id="run-1"
        )

        port.ask(1, "blocker")
        port.ask(2, "blocker")

        parked_keys = [k for k in board.questions if k.startswith("parked:wf-1:run-1:")]
        assert len(parked_keys) == 2
        assert parked_keys[0] != parked_keys[1]
        store.close()

    def test_unchanged_round_no_reuses_the_same_key(self, tmp_path: Path) -> None:
        store = GoalInterruptStore(tmp_path / "gi").open()
        board = FakeBoard()
        port = LineInterruptPort(
            folder_id="wf-1", generation=1, store=store, board=board, run_id="run-1"
        )

        first_qid, _ = port.ask(1, "blocker")
        second_qid, _ = port.ask(1, "blocker")

        assert second_qid == first_qid
        parked_keys = [k for k in board.questions if k.startswith("parked:wf-1:run-1:")]
        assert len(parked_keys) == 1
        store.close()

    def test_the_runtime_write_point_always_emits_the_content_variant(self, tmp_path: Path) -> None:
        """Write-point enumeration: the runtime's ``parked:`` key must keep the
        content-variant; a regression to ``parked:<folder>:<run_id>`` turns red."""
        store = GoalInterruptStore(tmp_path / "gi").open()
        board = FakeBoard()
        port = LineInterruptPort(
            folder_id="wf-1", generation=1, store=store, board=board, run_id="run-1"
        )

        port.ask(1, "blocker")

        parked_keys = [k for k in board.questions if k.startswith("parked:wf-1:run-1:")]
        assert len(parked_keys) == 1
        key = parked_keys[0]
        assert key.startswith("parked:wf-1:run-1:")
        assert re.fullmatch(r"parked:wf-1:run-1:[0-9a-f]{12}", key)
        assert key != "parked:wf-1:run-1"
        store.close()


# --- resolver ---------------------------------------------------------------


class TestResolver:
    def test_newest_decision_wins_by_seq_then_message_id(self) -> None:
        newer_seq = decision("d-1", 5)
        older_seq = decision("d-2", 2)
        assert newest_decision([older_seq, newer_seq]) == newer_seq

    def test_compensate_reports_newer_than_last(self) -> None:
        newest, need = compensate_decision([decision("d-1", 7)], last_decision_message_id="d-0")
        assert newest is not None and need is True
        _, already = compensate_decision([decision("d-1", 7)], last_decision_message_id="d-1")
        assert already is False

    def test_decision_input_is_built_from_the_message_only(self) -> None:
        message = decision("d-9", 3)
        built = decision_input_from_message(
            message,
            resume_key=RESUME_KEY,
            question_note_id=QUESTION_ID,
            references=[{"message_id": "d-9", "target_entity": QUESTION_ID}],
        )
        assert built.message_id == "d-9"
        assert built.channel_seq == 3
        assert built.decision == "APPROVE"
        assert built.refs[0].target_entity == QUESTION_ID
        assert built.resume_key == RESUME_KEY

    def test_legacy_fallback_resolves_exactly_one_owner(self) -> None:
        owner = OwnerTarget("line", "wf-abc", 2, "", "card-1", "parked")
        resolution = legacy_owner_fallback(
            folder_id="wf-abc",
            referenced_question_ids=["q-1"],
            question_texts={"q-1": "line wf-abc needs a human decision"},
            legacy_owners=[owner],
        )
        assert resolution.outcome == LEGACY_OUTCOME_RESOLVED
        assert resolution.target is not None and resolution.target.id == "wf-abc"
        assert resolution.question_note_id == "q-1"

    def test_legacy_fallback_ambiguous_on_multiple_owners(self) -> None:
        owners = [
            OwnerTarget("line", "wf-abc", 2, "", "card-1", "parked"),
            OwnerTarget("line", "wf-abc", 3, "", "card-1", "parked"),
        ]
        resolution = legacy_owner_fallback(
            folder_id="wf-abc",
            referenced_question_ids=["q-1"],
            question_texts={"q-1": "line wf-abc question"},
            legacy_owners=owners,
        )
        assert resolution.outcome == LEGACY_OUTCOME_AMBIGUOUS
        assert resolution.target is None
        assert not resolution.resolved

    def test_legacy_fallback_ignores_a_question_without_the_folder(self) -> None:
        owner = OwnerTarget("line", "wf-abc", 2, "", "card-1", "parked")
        resolution = legacy_owner_fallback(
            folder_id="wf-abc",
            referenced_question_ids=["q-1"],
            question_texts={"q-1": "some other line's question"},
            legacy_owners=[owner],
        )
        assert resolution.outcome == LEGACY_OUTCOME_AMBIGUOUS

    def test_legacy_fallback_excludes_stale_owners(self) -> None:
        owner = OwnerTarget("line", "wf-abc", 2, "", "card-1", "complete")
        resolution = legacy_owner_fallback(
            folder_id="wf-abc",
            referenced_question_ids=["q-1"],
            question_texts={"q-1": "line wf-abc question"},
            legacy_owners=[owner],
        )
        assert resolution.outcome == LEGACY_OUTCOME_AMBIGUOUS


# --- bridge ------------------------------------------------------------------


class TestBridge:
    def test_bridge_finds_a_decision_on_a_board_longer_than_the_page(self, tmp_path: Path) -> None:
        """The bus pages ascending, so a plain ``limit=200`` call returns the
        *oldest* 200 messages and a decision at the new end of a 251-message
        board would be missed. The chain must be read backward from the head."""
        store = GoalInterruptStore(tmp_path / "gi").open()
        store.put_interrupt(
            {
                "resume_key": RESUME_KEY,
                "folder_id": "wf-1",
                "generation": 1,
                "round_id": 1,
                "question_note_id": QUESTION_ID,
                "card_entity_id": "card-1",
                "prior_terminal_digest": "d",
            }
        )
        messages = [
            {
                "message_id": f"n-{i}",
                "channel_seq": i,
                "kind": "work.note.v1",
                "created_at": "2026-08-29T00:00:00Z",
                "payload": {"note": "filler", "note_type": "progress"},
            }
            for i in range(1, 251)
        ]
        messages.append(decision("d-1", 251))
        bus = FakeBus(messages)
        bus.link(QUESTION_ID, "d-1")

        resumes: list[str] = []
        bridge = GoalInterruptBridge(
            GoalInterruptBridgeConfig(),
            store=store,
            bus=bus,
            resumer=lambda d: resumes.append(d.message_id) or "resumed",
        )
        record = bridge.run_once()
        assert record["resumed"] == 1
        assert resumes == ["d-1"]
        store.close()

    def test_bridge_records_compensation_for_a_decision_behind_the_cursor(
        self, tmp_path: Path
    ) -> None:
        store = GoalInterruptStore(tmp_path / "gi").open()
        store.put_interrupt(
            {
                "resume_key": RESUME_KEY,
                "folder_id": "wf-1",
                "generation": 1,
                "round_id": 1,
                "question_note_id": QUESTION_ID,
                "card_entity_id": "card-1",
                "prior_terminal_digest": "d",
            }
        )
        # The cursor has already paged past the decision's position (a restart
        # or event-page gap), so recovering it records a compensation receipt.
        store.advance_cursor(5)
        bus = FakeBus([decision("d-1", 1)])
        bus.link(QUESTION_ID, "d-1")

        async_resumes: list[DecisionInput] = []

        def resumer(decision_input: DecisionInput) -> str:
            async_resumes.append(decision_input)
            return "resumed"

        bridge = GoalInterruptBridge(
            GoalInterruptBridgeConfig(), store=store, bus=bus, resumer=resumer
        )
        record = bridge.run_once()

        assert record["resumed"] == 1
        assert len(async_resumes) == 1
        assert async_resumes[0].message_id == "d-1"
        assert async_resumes[0].resume_key == RESUME_KEY
        assert store.compensation_receipt(RESUME_KEY)["last_decision_message_id"] == "d-1"
        assert store.cursor() >= 5  # never rolled back

    def test_bridge_does_not_record_compensation_when_observed_in_order(
        self, tmp_path: Path
    ) -> None:
        """A decision still ahead of the cursor is an ordinary in-order resume,
        not a compensated gap: no cursor_compensation receipt is recorded."""
        store = GoalInterruptStore(tmp_path / "gi").open()
        store.put_interrupt(
            {
                "resume_key": RESUME_KEY,
                "folder_id": "wf-1",
                "generation": 1,
                "round_id": 1,
                "question_note_id": QUESTION_ID,
                "card_entity_id": "card-1",
                "prior_terminal_digest": "d",
            }
        )
        bus = FakeBus([decision("d-1", 1)])
        bus.link(QUESTION_ID, "d-1")

        resumes: list[str] = []

        def resumer(decision_input: DecisionInput) -> str:
            resumes.append(decision_input.message_id)
            return "resumed"

        bridge = GoalInterruptBridge(
            GoalInterruptBridgeConfig(), store=store, bus=bus, resumer=resumer
        )
        record = bridge.run_once()

        assert record["resumed"] == 1
        assert resumes == ["d-1"]
        assert store.compensation_receipt(RESUME_KEY) is None

    def test_bridge_recovers_a_decision_the_cursor_missed(self, tmp_path: Path) -> None:
        """Cursor compensation: the decision is served only through the reverse
        refs chain -- the bridge queries the chain, never the cursor -- so a
        decision already paged past is still recovered without a rollback."""
        store = GoalInterruptStore(tmp_path / "gi").open()
        store.put_interrupt(
            {
                "resume_key": RESUME_KEY,
                "folder_id": "wf-1",
                "generation": 1,
                "round_id": 1,
                "question_note_id": QUESTION_ID,
                "card_entity_id": "card-1",
                "prior_terminal_digest": "d",
            }
        )
        # The cursor is already past seq 9; the decision at 9 is recovered from
        # the chain (never by rolling the cursor back).
        store.advance_cursor(10)
        bus = FakeBus([decision("d-missed", 9)])
        bus.link(QUESTION_ID, "d-missed")

        resumes: list[str] = []

        def resumer(decision_input: DecisionInput) -> str:
            resumes.append(decision_input.message_id)
            return "resumed"

        bridge = GoalInterruptBridge(
            GoalInterruptBridgeConfig(), store=store, bus=bus, resumer=resumer
        )
        bridge.run_once()

        assert resumes == ["d-missed"]
        assert store.compensation_receipt(RESUME_KEY)["last_decision_message_id"] == "d-missed"
        assert store.cursor() >= 10  # never rolled back

    def test_bridge_skips_an_already_resumed_question(self, tmp_path: Path) -> None:
        store = GoalInterruptStore(tmp_path / "gi").open()
        store.put_interrupt(
            {
                "resume_key": RESUME_KEY,
                "folder_id": "wf-1",
                "generation": 1,
                "round_id": 1,
                "question_note_id": QUESTION_ID,
                "card_entity_id": "card-1",
                "prior_terminal_digest": "d",
            }
        )
        store.record_resume(a_decision().as_dict())
        bus = FakeBus([decision("d-1", 1)])
        bus.link(QUESTION_ID, "d-1")

        resumes: list[str] = []
        bridge = GoalInterruptBridge(
            GoalInterruptBridgeConfig(),
            store=store,
            bus=bus,
            resumer=lambda d: resumes.append(d.message_id) or "resumed",
        )
        record = bridge.run_once()
        assert record["resumed"] == 0
        assert resumes == []
