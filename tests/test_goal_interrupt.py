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

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

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

# --- fakes ------------------------------------------------------------------


class ScriptedCoordinator:
    """Round 1 blocks on a decision; the resume turn either acknowledges the
    decision (then continues) or ignores it (then re-blocks, for the N7 test)."""

    def __init__(self, *, ignore_decision: bool = False, acknowledge: bool = True) -> None:
        self.ignore_decision = ignore_decision
        self.acknowledge = acknowledge
        self.calls: list[tuple[int, dict[str, Any]]] = []

    def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
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

    def turn(self, prompt: str, round_no: int) -> str:
        self.calls.append(round_no)
        return f"did {prompt}"


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
            generation=2,
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
            generation=2,
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
            generation=2,
        )
        assert resolution.outcome == LEGACY_OUTCOME_AMBIGUOUS

    def test_legacy_fallback_excludes_stale_owners(self) -> None:
        owner = OwnerTarget("line", "wf-abc", 2, "", "card-1", "complete")
        resolution = legacy_owner_fallback(
            folder_id="wf-abc",
            referenced_question_ids=["q-1"],
            question_texts={"q-1": "line wf-abc question"},
            legacy_owners=[owner],
            generation=2,
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
    def test_bridge_resumes_a_suspended_question_and_records_compensation(
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
