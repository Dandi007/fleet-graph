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
    ROLE_STAGE,
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


SCHEMAS = Path("/data/code/self/agent-runtime/profiles/roles/schemas")


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
    def test_a_review_result_is_forwarded_whole(self, tmp_path: Path) -> None:
        """The role returns a `review.result.v2`; that *is* the actor result.

        Forwarding it unfiltered is what lets the plugin validate it against
        its own schema and name the missing field, instead of a translation
        layer here getting between the agent and that answer.
        """
        declared = {"verdict": "APPROVE", "findings": [], "review_phase": "continuous"}
        launcher = RecordingLauncher(RunStatus("succeeded", {"structured_result": declared}))
        outcome = make_actor(tmp_path, launcher).act(REVIEW, dispatch_for(REVIEW))

        assert outcome.event == "APPROVE"
        assert outcome.receipt == {"review_result": declared}

    def test_an_implement_result_is_forwarded_whole(self, tmp_path: Path) -> None:
        declared = {
            "actor_job_id": "job-1",
            "input_commit": "1" * 40,
            "work_head_commit": "2" * 40,
        }
        launcher = RecordingLauncher(RunStatus("succeeded", {"structured_result": declared}))
        outcome = make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert outcome.event == SPINE_EVENT, "implement declares no verdict"
        assert outcome.receipt == declared

    def test_the_input_travels_in_a_file_not_in_argv(self, tmp_path: Path) -> None:
        """`/proc` makes argv world-readable, and the input names commits."""
        launcher = RecordingLauncher()
        make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))

        spec, _ = launcher.launched[0]
        assert spec.prompt == ""
        assert spec.input_path and spec.prompt_file
        written = json.loads(Path(spec.input_path).read_text(encoding="utf-8"))
        assert set(written) == {
            "attempt_id",
            "development_id",
            "spec_commit",
            "stage",
            "worktree_path",
            "run_id",
        }

    @pytest.mark.parametrize(
        "stage_id", ["implement", "continuous_review", "final_review"], ids=lambda s: s
    )
    def test_the_input_is_what_the_roles_own_schema_asks_for(
        self, tmp_path: Path, stage_id: str
    ) -> None:
        """Every dispatched stage, not just the first one.

        Checking only `implement` passed for a year of nothing, because
        `implement` happens to be a legal value in both vocabularies. The two
        review stages are where they diverge, and a real run is an expensive
        place to find that out.
        """
        schema_path = SCHEMAS / "attempt-context.v1.json"
        if not schema_path.is_file():
            pytest.skip("agent-runtime is not on this machine")
        import jsonschema

        stage = LIFECYCLE.stages[stage_id]
        launcher = RecordingLauncher()
        make_actor(tmp_path, launcher).act(stage, dispatch_for(stage))
        spec, _ = launcher.launched[0]
        written = json.loads(Path(spec.input_path).read_text(encoding="utf-8"))
        jsonschema.validate(written, json.loads(schema_path.read_text(encoding="utf-8")))

    def test_a_stage_model_override_names_its_runtime_too(self, tmp_path: Path) -> None:
        """`--model` resolves through a chain and agent-run wants the runtime
        named alongside it."""
        launcher = RecordingLauncher()
        actor = make_actor(tmp_path, launcher)
        actor.models = {"continuous_review": "deepseek-v4-pro"}

        actor.act(REVIEW, dispatch_for(REVIEW))
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))

        review_spec, implement_spec = (spec for spec, _ in launcher.launched)
        assert review_spec.model == "deepseek-v4-pro"
        assert review_spec.runtime == "opencode"
        assert implement_spec.model is None, "no override means the role's own selector"
        assert implement_spec.runtime is None

    def test_write_is_asked_for_only_where_the_product_changes(self, tmp_path: Path) -> None:
        """A role's `write` is a ceiling, not a grant. Asking for it where the
        role forbids it is refused outright, and reviewers forbid it -- a
        reviewer that writes to the subject workspace has its verdict
        discarded."""
        launcher = RecordingLauncher()
        actor = make_actor(tmp_path, launcher)
        for stage_id in ("implement", "continuous_review", "final_review"):
            actor.act(LIFECYCLE.stages[stage_id], dispatch_for(LIFECYCLE.stages[stage_id]))

        asked = {spec.labels["stage"]: spec.write for spec, _ in launcher.launched}
        assert asked == {
            "implement": True,
            "continuous_review": False,
            "final_review": False,
        }

    def test_the_roles_own_declarations_agree(self) -> None:
        """If a role ever stops allowing write, this is where we find out."""
        if not SCHEMAS.parent.is_dir():
            pytest.skip("agent-runtime is not on this machine")
        declared = {}
        for stage_id in ("implement", "continuous_review", "final_review"):
            role = SCHEMAS.parent / f"{stage_role(LIFECYCLE.stages[stage_id], {})}.yaml"
            text = role.read_text(encoding="utf-8")
            declared[stage_id] = "write: true" in text

        actor = AgentRunStageActor(
            launcher=None,  # type: ignore[arg-type]
            development_id="d",
            run_root=Path("/tmp"),
        )
        for stage_id, allows in declared.items():
            assert actor.writes(LIFECYCLE.stages[stage_id]) == allows, stage_id

    def test_the_stage_vocabulary_is_translated_not_assumed(self) -> None:
        """`review` where dd says `continuous_review`, `final-review` -- hyphen
        -- where dd says `final_review`. Two vocabularies, one translation."""
        schema_path = SCHEMAS / "attempt-context.v1.json"
        if not schema_path.is_file():
            pytest.skip("agent-runtime is not on this machine")
        enum = set(
            json.loads(schema_path.read_text(encoding="utf-8"))["properties"]["stage"]["enum"]
        )
        assert set(ROLE_STAGE.values()) <= enum, ROLE_STAGE
        assert set(ROLE_STAGE) == {n for n, s in LIFECYCLE.stages.items() if s.is_llm}

    def test_the_run_is_labelled_for_attribution(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))

        spec, _ = launcher.launched[0]
        assert spec.labels["development"] == "dev-1"
        assert spec.labels["stage"] == "implement"
        assert spec.labels["dispatcher"] == "fleet-graph"
        assert spec.structured is True

    def test_the_stage_labels_carry_role_order_attempt_and_dispatched_by(
        self, tmp_path: Path
    ) -> None:
        """role/order/attempt/dispatched_by are the upstream observability
        contract labels for a dd-spawned worker run; `development` stays
        (INV-2) and `attempt` is the stage's own attempt ordinal."""
        launcher = RecordingLauncher()
        actor = make_actor(tmp_path, launcher)
        actor.dispatched_by = "ronin-model-switch"
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT, attempt=2))

        spec, _ = launcher.launched[0]
        assert spec.labels["role"] == "dd-worker"
        assert spec.labels["order"] == "dev-1"
        assert spec.labels["development"] == "dev-1"
        assert spec.labels["attempt"] == "2"
        assert spec.labels["dispatched_by"] == "ronin-model-switch"

    def test_dispatched_by_defaults_to_the_bounded_dispatcher(self, tmp_path: Path) -> None:
        """Without recorded provenance the label falls back to the one bounded
        system subject, never a run_id/uuid."""
        launcher = RecordingLauncher()
        make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))

        spec, _ = launcher.launched[0]
        assert spec.labels["dispatched_by"] == "fleet-graph"

    def test_the_same_attempt_always_names_the_same_run(self, tmp_path: Path) -> None:
        """Derived ids are what let a restarted graph adopt instead of re-pay."""
        launcher = RecordingLauncher()
        actor = make_actor(tmp_path, launcher)
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert launcher.launched[0][1] == launcher.launched[1][1]

    def test_a_retry_names_a_different_run(self, tmp_path: Path) -> None:
        """Otherwise a bounded retry re-adopts the completed run it is retrying
        and returns the same answer, which makes the bound decorative."""
        launcher = RecordingLauncher()
        actor = make_actor(tmp_path, launcher)
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))
        actor.act(IMPLEMENT, {**dispatch_for(IMPLEMENT), "retry": 1})
        actor.act(IMPLEMENT, {**dispatch_for(IMPLEMENT), "retry": 2})

        ids = [run_id for _, run_id in launcher.launched]
        assert len(set(ids)) == 3, ids

    def test_the_same_retry_still_re_adopts(self, tmp_path: Path) -> None:
        """Re-adoption is the point of a derived id; only a *deliberate* retry
        should get a new one."""
        launcher = RecordingLauncher()
        actor = make_actor(tmp_path, launcher)
        actor.act(IMPLEMENT, {**dispatch_for(IMPLEMENT), "retry": 1})
        actor.act(IMPLEMENT, {**dispatch_for(IMPLEMENT), "retry": 1})

        assert launcher.launched[0][1] == launcher.launched[1][1]

    def test_a_later_attempt_names_a_different_run(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        actor = make_actor(tmp_path, launcher)
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT, attempt=1))
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT, attempt=2))

        assert launcher.launched[0][1] != launcher.launched[1][1]

    def test_the_roles_are_the_ones_agent_runtime_already_ships(self) -> None:
        """Reused rather than minted: a parallel role would drift from the
        personas the plugin bundle carries."""
        assert stage_role(IMPLEMENT, {}) == "implementer"
        assert stage_role(REVIEW, {}) == "continuous_reviewer"
        assert stage_role(LIFECYCLE.stages["final_review"], {}) == "final_reviewer"
        assert stage_role(IMPLEMENT, {"implement": "implementer_v2"}) == "implementer_v2"

    def test_those_roles_exist_where_agent_runtime_keeps_them(self) -> None:
        roles = Path("/data/code/self/agent-runtime/profiles/roles")
        if not roles.is_dir():
            pytest.skip("agent-runtime is not on this machine")
        for stage in (IMPLEMENT, REVIEW, LIFECYCLE.stages["final_review"]):
            assert (roles / f"{stage_role(stage, {})}.yaml").is_file(), stage.id


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


class FenceSimulatingLauncher:
    """A launcher that re-adopts by run_id exactly like the real one.

    ``launch`` dispatches once per distinct run_id; a second launch of the
    same run_id re-adopts instead of spawning a second run. ``wait`` is
    scripted: ``"timeout"`` raises the fence (the run is still going), any
    other entry is the ``RunStatus`` the run ended with. This is the "simulated
    launcher" the fence fix's regression tests drive.
    """

    def __init__(self, wait_script: list[Any]) -> None:
        self.wait_script = list(wait_script)
        self.spawned_run_ids: list[str] = []
        self.launched: list[str] = []

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        self.launched.append(run_id)
        if run_id in self.spawned_run_ids:
            return RunTicket(run_id, f"/tmp/{run_id}", None, adopted=True)
        self.spawned_run_ids.append(run_id)
        return RunTicket(run_id, f"/tmp/{run_id}", None, adopted=False)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        entry = self.wait_script.pop(0)
        if entry == "timeout":
            raise RunWaitTimeout(ticket, 90.0)
        return entry


def implement_success() -> RunStatus:
    return RunStatus(
        "succeeded",
        {
            "structured_result": {
                "actor_job_id": "job-implement",
                "input_commit": "1" * 40,
                "work_head_commit": "2" * 40,
            }
        },
    )


class TestTimeoutRetryReAdopts:
    """The fence fix: a timeout must re-adopt the run in flight, not abandon it.

    Before the fix the retry bumped the run_id derivation factor, so the
    retry *paid for a second run* while the first one kept burning tokens --
    the double-burn window this class closes. Both tests pin the two allowed
    outcomes: re-adopt the still-going run, and -- only once it is truly
    terminal/lost -- dispatch a fresh one.
    """

    def test_a_timeout_retry_re_adopts_the_run_still_in_flight(self, tmp_path: Path) -> None:
        """First wait hits the fence, the run then completes: the retry must
        continue waiting on the SAME run, so the launch total stays 1."""
        launcher = FenceSimulatingLauncher(["timeout", implement_success()])
        actor = make_actor(tmp_path, launcher)

        first = actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))
        assert first.event == FAILURE_EVENT
        assert first.run_in_flight is True, "a timeout leaves the run in flight"
        assert LIFECYCLE.is_retryable(first.failure_code)

        retry = actor.act(
            IMPLEMENT,
            {**dispatch_for(IMPLEMENT), "retry": 1, "re_adopt": True},
        )
        assert retry.event == SPINE_EVENT
        assert retry.run_in_flight is False

        # The retry derived the ORIGINAL run id and re-adopted it: the same
        # run id was launched twice but only one run was ever spawned.
        assert launcher.launched[0] == launcher.launched[1]
        assert len(launcher.spawned_run_ids) == 1, "a timeout retry must not spawn a second run"

    def test_a_lost_adopted_run_earns_the_second_launch(self, tmp_path: Path) -> None:
        """Re-adopt first; only once the adopted run is truly lost may the
        retry dispatch a fresh run -- the two-lines of the fence."""
        launcher = FenceSimulatingLauncher(["timeout", RunStatus("lost"), implement_success()])
        actor = make_actor(tmp_path, launcher)

        first = actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))
        assert first.run_in_flight is True

        adopted = actor.act(
            IMPLEMENT,
            {**dispatch_for(IMPLEMENT), "retry": 1, "re_adopt": True},
        )
        assert adopted.event == FAILURE_EVENT
        assert adopted.run_in_flight is False, "a lost run is not in flight"
        # The adopted run was re-adopted (same run id), not re-spawned.
        assert launcher.launched[0] == launcher.launched[1]
        assert len(launcher.spawned_run_ids) == 1

        fresh = actor.act(
            IMPLEMENT,
            {**dispatch_for(IMPLEMENT), "retry": 2, "re_adopt": False},
        )
        assert fresh.event == SPINE_EVENT
        # Now -- and only now -- a second launch is allowed, with a fresh id.
        assert launcher.launched[2] != launcher.launched[0]
        assert len(launcher.spawned_run_ids) == 2, "only a lost run earns a second launch"


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

    def test_the_verdict_is_written_into_the_product_tree(self, tmp_path: Path) -> None:
        """The run ends and its history goes with it. Who let a development
        through has to survive that."""
        from fleet_graph.graphs.dd_scripts import GATE_PATH

        board = FakeBoard(a_decision(GATE_APPROVE))
        gate = BoardGate(
            board=board,  # type: ignore[arg-type]
            card_entity_id="card-1",
            development_id="dev-1",
            repo=tmp_path,
        )
        gate.act(GATE, dispatch_for(GATE, generation=2))

        sealed = json.loads((tmp_path / GATE_PATH.format(generation=2)).read_text(encoding="utf-8"))
        assert sealed["decision"] == GATE_APPROVE
        assert sealed["decided_by"] == "青林"
        # The two ids that make it auditable: the verdict message, and the
        # question it answered.
        assert sealed["decision_message_id"] == "msg-decision"
        assert sealed["question_note_id"] == "note-1"
        assert sealed["development_id"] == "dev-1"

    def test_a_refused_gate_writes_nothing(self, tmp_path: Path) -> None:
        """A REJECT is not a gate_decision artifact; the stage produced none."""
        from fleet_graph.graphs.dd_scripts import GATE_PATH

        board = FakeBoard(a_decision("REJECT"))
        gate = BoardGate(
            board=board,  # type: ignore[arg-type]
            card_entity_id="card-1",
            development_id="dev-1",
            repo=tmp_path,
        )
        with pytest.raises(StageRefused):
            gate.act(GATE, dispatch_for(GATE))

        assert not (tmp_path / GATE_PATH.format(generation=1)).exists()

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


class TestGenerationDerivedIdentities:
    """R1-c: a fresh generation derives fresh run identities, so a rerun of
    the same development never collides with -- or silently re-adopts -- its
    previous generation's runs. The gate's bus idempotency key is covered by
    test_a_new_generation_asks_a_new_question above."""

    def test_a_new_generation_names_a_new_run(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        actor = make_actor(tmp_path, launcher)
        actor.act(IMPLEMENT, {**dispatch_for(IMPLEMENT), "generation": 1})
        actor.act(IMPLEMENT, {**dispatch_for(IMPLEMENT), "generation": 2})

        ids = [run_id for _, run_id in launcher.launched]
        assert len(set(ids)) == 2, ids

    def test_a_rejection_carries_the_implementation_class_code(self) -> None:
        board = FakeBoard(a_decision("REJECT"))
        with pytest.raises(StageRefused) as refused:
            make_gate(board).act(GATE, dispatch_for(GATE))
        assert refused.value.code == "GATE_REJECTED"
