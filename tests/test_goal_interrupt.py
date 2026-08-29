"""E2: the durable in-graph decision interrupt, end to end.

These tests pin the four load-bearing properties the spec names, using the real
durable surfaces -- a real SQLite checkpointer, a real ``GoalInterruptStore``
(SQLite, WAL, fail-closed), and the real graph nodes -- and only the coordinator
/ worker / bus at the edge are fakes:

- the interrupt checkpoint is atomic, unique per ``resume_key``, and carries the
  exact ``folder_id``/``generation``/``round_id``/``question_note_id``/
  ``card_entity_id``/``prior_terminal_digest``/``resume_key`` fields;
- the immutable ``DecisionInput`` is injected into the resumed coordinator
  envelope and a round-zero re-park that ignores it is rejected (N7);
- the legacy-owner fallback resolves exactly one owner or refuses loudly, and
  never fabricates a question id;
- cursor compensation picks the newest decision by ``(channel_seq, message_id)``
  and records a receipt without rolling back;
- one resume per ``resume_key``, one charge per ``turn_id``, and a duplicate
  delivery never invokes the model a second time.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.decision_bridge.bridge import DecisionBridge, DecisionBridgeConfig
from fleet_graph.decision_bridge.owners import (
    RESUME_RESUMED,
    LineOwnerSource,
    OwnerResult,
    OwnerTarget,
)
from fleet_graph.decision_bridge.store import BridgeStore
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
from fleet_graph.goal_interrupt.runtime import (
    RESUME_STATUS_ALREADY,
    RESUME_STATUS_RESUMED,
    LineInterruptPort,
    resume_line,
)
from fleet_graph.goal_interrupt.store import GoalInterruptStore
from fleet_graph.graphs.goal_line import (
    LineDeps,
    acknowledges_decision,
    build_goal_line_graph,
    n7_rejects_round_zero_repark,
)
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.work_report import SCHEMA_VERSION

# --- fakes ------------------------------------------------------------------


class ScriptedCoordinator:
    """Round 1 blocks on a decision; the resume turn either acknowledges the
    decision (then continues) or ignores it (then re-blocks, for the N7 test)."""

    def __init__(self, *, ignore_decision: bool = False, acknowledge: bool = True) -> None:
        self.ignore_decision = ignore_decision
        self.acknowledge = acknowledge
        self.calls: list[tuple[int, dict[str, Any]]] = []

    def turn(
        self, round_no: int, coord_input: dict[str, Any], *, resume: bool = False
    ) -> dict[str, Any]:
        self.calls.append((round_no, dict(coord_input)))
        has_decision = "decision" in coord_input
        if round_no == 1 and not has_decision:
            return {"verdict": "blocked", "waiting_on": "decision", "reason": "need human"}
        if has_decision and not self.ignore_decision:
            verdict = "continue"
            result = {
                "verdict": verdict,
                "next_prompt": "proceed with the decision",
            }
            if self.acknowledge:
                result["acknowledged_message_id"] = coord_input["decision"]["message_id"]
            return result
        if has_decision and self.ignore_decision:
            return {"verdict": "blocked", "waiting_on": "decision", "reason": "need human"}
        return {"verdict": "done", "reason": "finished"}


class RecordingWorker:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def turn(self, prompt: str, round_no: int) -> dict[str, Any]:
        self.calls.append(round_no)
        return {
            "schema_version": SCHEMA_VERSION,
            "turn_id": f"t-{round_no}",
            "outcome": "completed",
            "summary": f"did {prompt}",
            "did": [prompt],
            "files": [],
            "self_tests": [],
            "blocker": None,
        }


class NullInbox:
    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


class RecordingArtifacts:
    def __init__(self) -> None:
        self.terminals: list[dict[str, Any]] = []
        self.rounds: list[dict[str, Any]] = []

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        self.rounds.append(line)
        return True

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> str:
        return "worker-report.json"

    def write_terminal(self, **kwargs: Any) -> str:
        self.terminals.append(kwargs)
        return "terminal.json"

    def write_fault_terminal(self, **kwargs: Any) -> str:
        return "fault"


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


def make_line(
    tmp_path: Path, coordinator: ScriptedCoordinator
) -> tuple[Any, LineInterruptPort, GoalInterruptStore, RecordingWorker, ScriptedCoordinator]:
    store = GoalInterruptStore(tmp_path / "gi").open()
    worker = RecordingWorker()
    port = LineInterruptPort(folder_id="wf-1", generation=1, store=store)
    deps = LineDeps(
        coordinator=coordinator,
        worker=worker,
        inbox=NullInbox(),
        artifacts=RecordingArtifacts(),
        guards=LineGuards(bounds=LineBounds(max_rounds=50)),
        folder_id="wf-1",
        interrupt=port,
    )
    return build_goal_line_graph(deps), port, store, worker, coordinator


CFG = {"configurable": {"thread_id": "wf-1:g1"}, "recursion_limit": 200}

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

        assert question_note_id == "note-parked:wf-1:run-1"
        assert card_entity_id == "card-e2-goal-line-card:wf-1"
        assert "e2-goal-line-card:wf-1" in board.cards
        assert "parked:wf-1:run-1" in board.questions
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


# --- graph integration -------------------------------------------------------


class TestGraphInterrupt:
    def test_blocked_decision_suspends_and_persists_the_checkpoint(self, tmp_path: Path) -> None:
        graph, _port, store, _worker, coordinator = make_line(tmp_path, ScriptedCoordinator())
        with SqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as saver:
            compiled = graph.compile(checkpointer=saver)
            state = compiled.invoke({"round_no": 1}, config=CFG)

        assert state.get("__interrupt__")
        checkpoint = store.interrupt(RESUME_KEY)
        assert checkpoint is not None
        assert checkpoint["folder_id"] == "wf-1"
        assert checkpoint["generation"] == 1
        assert checkpoint["question_note_id"] == QUESTION_ID
        assert checkpoint["resume_key"] == RESUME_KEY
        assert len(coordinator.calls) == 1
        assert coordinator.calls[0][0] == 1

    def test_resume_injects_the_decision_and_continues_the_same_generation(
        self, tmp_path: Path
    ) -> None:
        graph, _port, store, worker, coordinator = make_line(tmp_path, ScriptedCoordinator())
        with SqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as saver:
            compiled = graph.compile(checkpointer=saver)
            compiled.invoke({"round_no": 1}, config=CFG)

            decision = a_decision()
            state, status = resume_line(compiled, config=CFG, decision=decision, store=store)

        assert status == RESUME_STATUS_RESUMED
        assert state["terminal"] == "done"
        assert state["round_no"] == 2
        # the resumed coordinator turn carried the decision and its resume key
        resume_turn = next(c for c in coordinator.calls if "decision" in c[1])
        assert resume_turn[1]["decision"]["message_id"] == "d-1"
        assert resume_turn[1]["resume_key"] == RESUME_KEY
        assert worker.calls == [1]

    def test_round_zero_repark_is_rejected_when_unacknowledged(self, tmp_path: Path) -> None:
        """N7: the coordinator answers the injected decision by re-declaring the
        old blocked+decision verdict without acknowledging it -- the round is
        rejected rather than suspending again."""
        graph, _port, store, _worker, _coordinator = make_line(
            tmp_path, ScriptedCoordinator(ignore_decision=True)
        )
        with SqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as saver:
            compiled = graph.compile(checkpointer=saver)
            compiled.invoke({"round_no": 1}, config=CFG)
            state, _status = resume_line(
                compiled,
                config=CFG,
                decision=a_decision(),
                store=store,
            )
        # The unacknowledged re-block is rejected (round advanced) and the line
        # is not left re-suspended on the same stale blocker.
        assert not state.get("__interrupt__")
        assert state["round_no"] > 1


class TestAcknowledge:
    def test_acknowledges_decision_only_accepts_the_machine_field(self) -> None:
        assert acknowledges_decision({"acknowledged_message_id": "d-1"}, "d-1")
        assert acknowledges_decision({"decision_message_id": "d-1"}, "d-1")
        assert not acknowledges_decision({"reason": "saw d-1"}, "d-1")

    def test_n7_rejects_repark_without_acknowledgement(self) -> None:
        assert n7_rejects_round_zero_repark(
            {"verdict": "blocked"}, decision_message_id="d-1", waiting_on="decision"
        )
        assert not n7_rejects_round_zero_repark(
            {"acknowledged_message_id": "d-1"},
            decision_message_id="d-1",
            waiting_on="decision",
        )
        assert not n7_rejects_round_zero_repark(
            {"verdict": "blocked"}, decision_message_id="d-1", waiting_on="external"
        )


# --- runtime dedup -----------------------------------------------------------


class TestRuntimeDedup:
    def test_a_duplicate_delivery_does_not_reinvoke_the_model(self, tmp_path: Path) -> None:
        graph, _port, store, _worker, coordinator = make_line(tmp_path, ScriptedCoordinator())
        with SqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as saver:
            compiled = graph.compile(checkpointer=saver)
            compiled.invoke({"round_no": 1}, config=CFG)
            state, first = resume_line(compiled, config=CFG, decision=a_decision(), store=store)
            calls_after_first = len(coordinator.calls)
            state, second = resume_line(compiled, config=CFG, decision=a_decision(), store=store)

        assert first == RESUME_STATUS_RESUMED
        assert second == RESUME_STATUS_ALREADY
        assert state["terminal"] == "done"
        # the second delivery added no new coordinator (model) invocation
        assert len(coordinator.calls) == calls_after_first
        assert store.turn_invocations(f"{RESUME_KEY}:turn:1") == 1


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


# --- legacy-owner fallback, on the real decision bridge ---------------------
#
# Spec item 6 and the test requirements demand proof that the *wired* path
# resolves a legacy parked owner, not the pure ``legacy_owner_fallback`` helper
# in isolation. These tests drive the real ``DecisionBridge`` -- real
# ``_question_texts`` bus scan, the real ``resolve_decision`` with its
# ``_legacy_resolve`` branch, the real ``BridgeStore`` intent/receipt -- over a
# legacy owner (a parked line whose ``question_note_id`` was never persisted).


def _question_note(message_id: str, seq: int, *, text: str) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "channel_seq": seq,
        "kind": "work.note.v1",
        "created_at": "2026-08-29T00:00:00Z",
        "payload": {"note": text, "note_type": "question", "card_entity_id": "card-1"},
    }


def _legacy_line_source(tmp_path: Path, *, folder_id: str, generation: int) -> LineOwnerSource:
    """A real ``LineOwnerSource`` over a parked line whose ``board_question_note_id``
    was never persisted -- the legacy gap the fallback exists to close."""
    run_root = tmp_path / "runs"
    stall = run_root / ".scheduler" / f"{folder_id}.json"
    stall.parent.mkdir(parents=True, exist_ok=True)
    stall.write_text(
        json.dumps(
            {
                "generation": generation,
                "parked_run_id": f"run-{folder_id}",
                "parked_at": 1699999999.0,
                "board_card_entity_id": "card-1",
                "board_question_note_id": "",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return LineOwnerSource(run_root, lines=[{"folder_id": folder_id, "generation": 1}])


class _FakeLegacyOwners:
    """A seam owner source returning the exact legacy owners a test staged."""

    def __init__(self, owners: list[OwnerTarget]) -> None:
        self.owners = owners
        self.resumed: list[tuple[str, str]] = []

    def discover(self, question_note_id: str) -> list[OwnerTarget]:
        return [t for t in self.owners if t.question_note_id == question_note_id]

    def discover_all(self) -> list[OwnerTarget]:
        return list(self.owners)

    def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
        self.resumed.append((target.id, action_key))
        return OwnerResult(RESUME_RESUMED, "ok")


class TestLegacyOwnerBridge:
    def _bridge(self, tmp_path: Path, *, owner_source: Any) -> tuple[DecisionBridge, BridgeStore]:
        bus = FakeBus(
            [
                _question_note("q-1", 1, text="line wf-abc needs a human decision"),
                decision("d-1", 2),
            ]
        )
        bus.link("q-1", "d-1")
        store = BridgeStore(tmp_path / "bridge").open()
        bridge = DecisionBridge(
            DecisionBridgeConfig(state_dir=tmp_path / "bridge"),
            bus=bus,
            owner_source=owner_source,
            store=store,
        )
        return bridge, store

    def test_unique_legacy_owner_resumes_through_the_real_bridge(self, tmp_path: Path) -> None:
        # The wired path: _question_texts -> resolve_decision -> _legacy_resolve
        # -> legacy_owner_resolution intent -> LineOwnerSource.resume (wake).
        source = _legacy_line_source(tmp_path, folder_id="wf-abc", generation=2)
        bridge, store = self._bridge(tmp_path, owner_source=source)

        record = bridge.run_once()
        receipt = store.receipt("d-1")

        assert record["resumed"] == 1
        assert receipt is not None
        assert receipt["status"] == "resumed"
        assert receipt["reason"] == "legacy_owner_resolution"
        assert receipt["target_kind"] == "line"
        assert receipt["target_id"] == "wf-abc"
        # The real resume woke the parked line: the stall snapshot was cleared.
        stall = json.loads(
            (tmp_path / "runs" / ".scheduler" / "wf-abc.json").read_text(encoding="utf-8")
        )
        assert stall.get("parked_run_id") is None
        store.close()

    def test_ambiguous_legacy_owner_performs_no_resume(self, tmp_path: Path) -> None:
        owners = _FakeLegacyOwners(
            [
                OwnerTarget("line", "wf-abc", 2, "", "card-1", "parked"),
                OwnerTarget("line", "wf-abc", 3, "", "card-1", "parked"),
            ]
        )
        bridge, store = self._bridge(tmp_path, owner_source=owners)

        record = bridge.run_once()
        receipt = store.receipt("d-1")

        assert record["resumed"] == 0
        assert owners.resumed == []
        assert receipt is not None
        assert receipt["status"] == "noop"
        assert receipt["reason"] == LEGACY_OUTCOME_AMBIGUOUS
        store.close()

    def test_question_texts_pages_past_the_oldest_window(self, tmp_path: Path) -> None:
        """A question note posted after 250 older work-notes messages must still
        be seen by ``_question_texts``: the old ascending-page read would miss it
        and degrade to ``legacy_owner_ambiguous``."""
        source = _legacy_line_source(tmp_path, folder_id="wf-abc", generation=2)
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
        messages.extend(
            [
                _question_note("q-late", 251, text="line wf-abc needs a human decision"),
                decision("d-late", 252),
            ]
        )
        bus = FakeBus(messages)
        bus.link("q-late", "d-late")
        store = BridgeStore(tmp_path / "bridge").open()
        bridge = DecisionBridge(
            DecisionBridgeConfig(state_dir=tmp_path / "bridge"),
            bus=bus,
            owner_source=source,
            store=store,
        )

        bridge.run_once()  # first cycle reads the 250 filler notes and advances the cursor
        record = bridge.run_once()  # second cycle reaches the question + decision
        receipt = store.receipt("d-late")

        assert record["resumed"] == 1
        assert receipt is not None
        assert receipt["status"] == "resumed"
        assert receipt["reason"] == "legacy_owner_resolution"
        store.close()
