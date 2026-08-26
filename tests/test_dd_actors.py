"""The wiring: llm stages to agent-run, the human gate to the work board."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.bus.board import Decision, GateTicket
from fleet_graph.dd.lifecycle import Lifecycle, Stage
from fleet_graph.executors.agent_run import RunStatus, RunTicket, RunWaitTimeout
from fleet_graph.graphs.dd_actors import (
    GATE_APPROVE,
    AgentRunStageActor,
    BoardGate,
    stage_role,
)
from fleet_graph.graphs.dd_pipeline import (
    FAILURE_EVENT,
    SPINE_EVENT,
    Dispatch,
    GatePending,
    StageRefused,
)
from source_tools import executable_source

LIFECYCLE = Lifecycle.load()
IMPLEMENT = LIFECYCLE.stages["implement"]
REVIEW = LIFECYCLE.stages["continuous_review"]
GATE = LIFECYCLE.stages["human_gate"]
COMMIT = "a" * 40


def dispatch_for(stage: Stage, *, attempt: int = 1, generation: int = 1) -> Dispatch:
    return {
        "development_id": "dev-1",
        "stage": stage.id,
        "mode": "initial",
        "generation": generation,
        "attempt": attempt,
        "input_commit": COMMIT,
        "required_artifacts": list(stage.required_artifacts),
        "produced_artifacts": list(stage.produced_artifacts),
        "contract_version": LIFECYCLE.contract_version,
    }


class RecordingLauncher:
    """Records what was launched and answers with a canned status."""

    def __init__(self, status: RunStatus | None = None) -> None:
        self.status = status or RunStatus("succeeded", {"structured_result": {}})
        self.launched: list[tuple[Any, str]] = []
        self.raise_on_wait: Exception | None = None

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        self.launched.append((spec, run_id))
        return RunTicket(run_id, f"/tmp/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        if self.raise_on_wait is not None:
            raise self.raise_on_wait
        return self.status


def make_actor(tmp_path: Path, launcher: RecordingLauncher) -> AgentRunStageActor:
    return AgentRunStageActor(
        launcher=launcher,  # type: ignore[arg-type]
        development_id="dev-1",
        run_root=tmp_path,
    )


class TestDispatchingAnLlmStage:
    def test_the_declared_result_becomes_the_outcome(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher(
            RunStatus(
                "succeeded",
                {
                    "structured_result": {
                        "verdict": "APPROVE",
                        "receipt": {"output_commit": COMMIT},
                        "produced_artifacts": ["continuous_verdict", "feedback"],
                    }
                },
            )
        )
        outcome = make_actor(tmp_path, launcher).act(REVIEW, dispatch_for(REVIEW))

        assert outcome.event == "APPROVE"
        assert outcome.receipt == {"output_commit": COMMIT}
        assert outcome.produced == ("continuous_verdict", "feedback")

    def test_the_dispatch_travels_in_a_file_not_in_argv(self, tmp_path: Path) -> None:
        """`/proc` makes argv world-readable, and a dispatch names commits."""
        launcher = RecordingLauncher()
        dispatch = dispatch_for(IMPLEMENT)
        make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch)

        spec, _ = launcher.launched[0]
        assert spec.prompt == ""
        assert spec.input_path and spec.prompt_file
        assert json.loads(Path(spec.input_path).read_text(encoding="utf-8")) == dispatch

    def test_the_run_is_labelled_for_attribution(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))

        spec, _ = launcher.launched[0]
        assert spec.labels["development"] == "dev-1"
        assert spec.labels["stage"] == "implement"
        assert spec.labels["dispatcher"] == "fleet-graph"
        assert spec.structured is True

    def test_the_same_attempt_always_names_the_same_run(self, tmp_path: Path) -> None:
        """Derived ids are what let a restarted graph adopt instead of re-pay."""
        launcher = RecordingLauncher()
        actor = make_actor(tmp_path, launcher)
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert launcher.launched[0][1] == launcher.launched[1][1]

    def test_a_later_attempt_names_a_different_run(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        actor = make_actor(tmp_path, launcher)
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT, attempt=1))
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT, attempt=2))

        assert launcher.launched[0][1] != launcher.launched[1][1]

    def test_the_role_defaults_per_stage_and_can_be_overridden(self) -> None:
        assert stage_role(IMPLEMENT, {}) == "dd_implement"
        assert stage_role(IMPLEMENT, {"implement": "dd_impl_v2"}) == "dd_impl_v2"


class TestFailuresUseTheContractsOwnTaxonomy:
    def test_a_timeout_reports_a_code_the_contract_calls_retryable(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        launcher.raise_on_wait = RunWaitTimeout(RunTicket("run-1", "/tmp/run-1", None), 90.0)
        outcome = make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert outcome.event == FAILURE_EVENT
        assert LIFECYCLE.is_retryable(outcome.failure_code), outcome.failure_code

    def test_a_failed_run_reports_a_failure_event(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher(RunStatus("failed", {"exit_code": 1}))
        outcome = make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))
        assert outcome.event == FAILURE_EVENT

    def test_an_unreadable_envelope_is_not_retryable(self, tmp_path: Path) -> None:
        """A run that answered in a shape we will not guess at is a schema
        failure, and the contract says schema failures are not retried."""
        launcher = RecordingLauncher(RunStatus("succeeded", {"stdout": "looks fine to me"}))
        outcome = make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert outcome.event == FAILURE_EVENT
        assert LIFECYCLE.is_retryable(outcome.failure_code) is False

    def test_no_declared_verdict_reports_the_spine_event(self, tmp_path: Path) -> None:
        """Which the walker faults on for a stage that owes a verdict -- the
        point is that this layer does not invent one."""
        launcher = RecordingLauncher(
            RunStatus("succeeded", {"structured_result": {"produced_artifacts": ["feedback"]}})
        )
        outcome = make_actor(tmp_path, launcher).act(REVIEW, dispatch_for(REVIEW))
        assert outcome.event == SPINE_EVENT


class FakeBoard:
    """Records questions; answers only what has been put on it."""

    def __init__(self, decision: Decision | None = None) -> None:
        self.decision = decision
        self.asked: list[dict[str, str]] = []

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> GateTicket:
        self.asked.append(
            {
                "card_entity_id": card_entity_id,
                "question": question,
                "idempotency_key": idempotency_key,
            }
        )
        return GateTicket(question_note_id="note-1", card_entity_id=card_entity_id)

    def decision_for(self, ticket: GateTicket) -> Decision | None:
        return self.decision


def a_decision(value: str, *, by: str = "青林") -> Decision:
    return Decision(
        message_id="msg-decision",
        decision=value,
        decided_by=by,
        question="放行吗",
        rationale="",
        card_entity_id="card-1",
        raw={},
    )


def make_gate(board: FakeBoard) -> BoardGate:
    return BoardGate(
        board=board,  # type: ignore[arg-type]
        card_entity_id="card-1",
        development_id="dev-1",
    )


class TestTheHumanGateWaitsRatherThanDecides:
    def test_an_unanswered_question_pends(self) -> None:
        board = FakeBoard()
        with pytest.raises(GatePending) as pending:
            make_gate(board).act(GATE, dispatch_for(GATE))

        assert pending.value.ticket["question_note_id"] == "note-1"
        assert board.asked, "the question has to reach the board before we wait on it"

    def test_re_asking_lands_on_the_same_note(self) -> None:
        """A graph that dies mid-wait must not post a second question."""
        board = FakeBoard()
        gate = make_gate(board)
        for _ in range(3):
            with pytest.raises(GatePending):
                gate.act(GATE, dispatch_for(GATE))

        keys = {entry["idempotency_key"] for entry in board.asked}
        assert len(keys) == 1

    def test_a_new_generation_asks_a_new_question(self) -> None:
        board = FakeBoard()
        gate = make_gate(board)
        for generation in (1, 2):
            with pytest.raises(GatePending):
                gate.act(GATE, dispatch_for(GATE, generation=generation))

        assert len({entry["idempotency_key"] for entry in board.asked}) == 2

    def test_an_approval_produces_what_the_stage_declares(self) -> None:
        board = FakeBoard(a_decision(GATE_APPROVE))
        outcome = make_gate(board).act(GATE, dispatch_for(GATE))

        assert outcome.event == SPINE_EVENT
        assert outcome.produced == GATE.produced_artifacts
        assert outcome.receipt is not None
        assert outcome.receipt["decided_by"] == "青林"
        assert outcome.receipt["decision_message_id"] == "msg-decision"

    def test_a_rejection_refuses_without_calling_it_a_fault(self) -> None:
        board = FakeBoard(a_decision("REJECT"))
        with pytest.raises(StageRefused, match="REJECT"):
            make_gate(board).act(GATE, dispatch_for(GATE))

    def test_an_unrecognised_verdict_is_never_rounded_towards_proceed(self) -> None:
        board = FakeBoard(a_decision("looks good to me"))
        with pytest.raises(StageRefused, match="refusing to interpret"):
            make_gate(board).act(GATE, dispatch_for(GATE))

    def test_lowercase_approval_still_counts(self) -> None:
        board = FakeBoard(a_decision("approve"))
        assert make_gate(board).act(GATE, dispatch_for(GATE)).event == SPINE_EVENT

    def test_the_gate_has_no_way_to_cast_a_vote(self) -> None:
        """Structural, not behavioural: there is no method to misuse."""
        from fleet_graph.bus import board as board_module
        from fleet_graph.graphs import dd_actors

        source = executable_source(Path(dd_actors.__file__))
        assert board_module.DECISION_KIND not in source
        assert not any(
            "decision" in name and name.startswith(("publish", "post", "cast"))
            for name in dir(board_module.Board)
        )
