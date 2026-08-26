"""Walking the dd contract: the wrapper, the spine, the bindings, the gate."""

from __future__ import annotations

import json
import tokenize
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fleet_graph.dd.capability import CapabilityLock
from fleet_graph.dd.lifecycle import LIFECYCLE_PATH, Lifecycle, Stage
from fleet_graph.graphs.dd_pipeline import (
    SPINE_EVENT,
    TERMINAL_BOUNDS,
    TERMINAL_COMPLETE,
    TERMINAL_FAILED,
    TERMINAL_FAULT,
    TERMINAL_REFUSED,
    Dispatch,
    GatePending,
    PipelineBounds,
    PipelineDeps,
    StageOutcome,
    StageRefused,
    build_dd_pipeline_graph,
    initial_state,
)

SPEC_COMMIT = "0" * 40


def sealed_commit(dispatch: Dispatch) -> str:
    """What the sealer would produce. Actor and materializer derive it alike."""
    return f"{dispatch['stage']}-g{dispatch['generation']}-a{dispatch['attempt']}"


class ContractActor:
    """Produces exactly what the stage declares, and a matching receipt.

    Verdicts are scripted per stage as a queue; anything unscripted reports the
    spine event.
    """

    def __init__(self, verdicts: dict[str, list[str]] | None = None) -> None:
        self.verdicts = {k: list(v) for k, v in (verdicts or {}).items()}
        self.calls: list[tuple[str, int]] = []

    def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
        self.calls.append((stage.id, dispatch["attempt"]))
        queue = self.verdicts.get(stage.id)
        event = queue.pop(0) if queue else SPINE_EVENT
        return StageOutcome(
            event=event,
            receipt={"stage": stage.id, "verdict": event, "output_commit": sealed_commit(dispatch)},
            produced=tuple(stage.produced_artifacts),
        )


class Sealer:
    def __init__(self) -> None:
        self.commits: list[str] = []

    def materialize(self, stage: Stage, dispatch: Dispatch, outcome: StageOutcome) -> str:
        commit = sealed_commit(dispatch)
        self.commits.append(commit)
        return commit


def make_deps(**overrides: Any) -> PipelineDeps:
    lifecycle = overrides.pop("lifecycle", None) or Lifecycle.load()
    actor = overrides.pop("actor", None) or ContractActor()
    scripts = overrides.pop("scripts", None)
    if scripts is None:
        scripts = {name: actor for name, stage in lifecycle.stages.items() if not stage.is_llm}
    return PipelineDeps(
        lifecycle=lifecycle,
        dispatcher=actor,
        scripts=scripts,
        materializer=overrides.pop("materializer", None) or Sealer(),
        **overrides,
    )


def run(deps: PipelineDeps, *, stage: str = "configure", **state: Any) -> dict[str, Any]:
    graph = build_dd_pipeline_graph(deps)
    compiled = graph.compile()
    start = initial_state(
        development_id="dev-1",
        stage=stage,
        head_commit=SPEC_COMMIT,
        artifacts={"spec": SPEC_COMMIT},
    )
    start.update(state)  # type: ignore[typeddict-item]
    return compiled.invoke(start, config={"recursion_limit": 200})


class TestTheHappyPathWalksTheWholeContract:
    def test_it_reaches_the_last_stage(self) -> None:
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run(make_deps(actor=actor))

        assert state["terminal"] == TERMINAL_COMPLETE
        assert [stage for stage, _ in actor.calls] == [
            "configure",
            "implement",
            "continuous_review",
            "final_review",
            "acceptance",
            "human_gate",
            "merger",
        ]

    def test_every_stage_starts_from_what_the_last_one_sealed(self) -> None:
        """The forward chain, observed rather than assumed."""
        seen: list[str] = []

        class Recorder(ContractActor):
            def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
                seen.append(dispatch["input_commit"])
                return super().act(stage, dispatch)

        actor = Recorder({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        sealer = Sealer()
        run(make_deps(actor=actor, materializer=sealer))

        assert seen[0] == SPEC_COMMIT
        assert seen[1:] == sealer.commits[:-1]

    def test_the_history_records_one_entry_per_stage(self) -> None:
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run(make_deps(actor=actor))
        assert [entry["stage"] for entry in state["history"]] == [s for s, _ in actor.calls]


class TestReworkComesBackAsRework:
    def test_a_rejection_re_enters_the_earlier_stage_with_a_new_attempt(self) -> None:
        actor = ContractActor(
            {"continuous_review": ["REJECT", "APPROVE"], "final_review": ["APPROVE"]}
        )
        state = run(make_deps(actor=actor))

        assert state["terminal"] == TERMINAL_COMPLETE
        implements = [attempt for stage, attempt in actor.calls if stage == "implement"]
        assert implements == [1, 2], "the rework attempt must not re-enter as attempt 1"

    def test_rework_is_inherited_by_the_stages_downstream(self) -> None:
        """`next_mode: inherit` means inherit, not reset.

        A stage reviewing reworked output must know that is what it is looking
        at; resetting to normal on the next hop would hide it.
        """
        modes: list[tuple[str, str]] = []

        class Recorder(ContractActor):
            def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
                modes.append((stage.id, dispatch["mode"]))
                return super().act(stage, dispatch)

        actor = Recorder({"continuous_review": ["REJECT", "APPROVE"], "final_review": ["APPROVE"]})
        run(make_deps(actor=actor))

        after_rejection = modes[modes.index(("implement", "rework")) :]
        assert all(mode == "rework" for _, mode in after_rejection), after_rejection

    def test_an_endless_rework_loop_is_bounded(self) -> None:
        actor = ContractActor({"continuous_review": ["REJECT"] * 20})
        state = run(make_deps(actor=actor, bounds=PipelineBounds(max_rework=3)))

        assert state["terminal"] == TERMINAL_BOUNDS
        assert "3" in state["terminal_reason"]

    def test_the_step_bound_stops_a_runaway_walk(self) -> None:
        actor = ContractActor({"continuous_review": ["REJECT"] * 50})
        state = run(make_deps(actor=actor, bounds=PipelineBounds(max_steps=5, max_rework=99)))
        assert state["terminal"] == TERMINAL_BOUNDS


class TestTheWrapperIsEnforced:
    def test_a_stage_missing_its_inputs_never_runs(self) -> None:
        actor = ContractActor()
        deps = make_deps(actor=actor)
        graph = build_dd_pipeline_graph(deps).compile()
        state = graph.invoke(
            initial_state(
                development_id="dev-1",
                stage="implement",
                head_commit=SPEC_COMMIT,
                artifacts={"spec": SPEC_COMMIT},  # run_config was never produced
            )
        )

        assert state["terminal"] == TERMINAL_FAULT
        assert "run_config" in state["terminal_reason"]
        assert actor.calls == [], "input_verify must refuse before the actor runs"

    def test_a_stage_that_produces_nothing_is_not_believed(self) -> None:
        class EmptyHanded(ContractActor):
            def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
                return StageOutcome(event=SPINE_EVENT, receipt={}, produced=())

        state = run(make_deps(actor=EmptyHanded()))
        assert state["terminal"] == TERMINAL_FAULT
        assert "run_config" in state["terminal_reason"]

    def test_an_unimplemented_wrapper_step_faults_rather_than_skipping(
        self, tmp_path: Path
    ) -> None:
        """A silently skipped output_verify is an unverified stage reporting success."""
        contract = json.loads(LIFECYCLE_PATH.read_text(encoding="utf-8"))
        contract["wrapper"] = ["input_verify", "actor", "attest", "output_verify"]
        path = tmp_path / "lifecycle.json"
        path.write_text(json.dumps(contract), encoding="utf-8")

        state = run(make_deps(lifecycle=Lifecycle.load(path)))
        assert state["terminal"] == TERMINAL_FAULT
        assert "attest" in state["terminal_reason"]

    def test_a_script_stage_with_no_script_is_a_fault_not_a_guess(self) -> None:
        state = run(make_deps(scripts={}))
        assert state["terminal"] == TERMINAL_FAULT
        assert "no registered script" in state["terminal_reason"]

    def test_a_missing_materializer_is_a_fault(self) -> None:
        deps = make_deps()
        deps.materializer = None
        state = run(deps)
        assert state["terminal"] == TERMINAL_FAULT
        assert "materializer" in state["terminal_reason"]


class TestBindingsAreEnforcedNotTrusted:
    def test_a_receipt_claiming_a_commit_nobody_sealed_breaks_the_chain(self) -> None:
        class Liar(ContractActor):
            def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
                outcome = super().act(stage, dispatch)
                receipt = dict(outcome.receipt or {})
                receipt["output_commit"] = "deadbeef" * 5
                return StageOutcome(event=outcome.event, receipt=receipt, produced=outcome.produced)

        actor = Liar({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run(make_deps(actor=actor))
        assert state["terminal"] == TERMINAL_FAULT
        assert "forward chain is severed" in state["terminal_reason"]

    def test_a_verdict_the_receipt_contradicts_is_refused(self) -> None:
        class Contradictory(ContractActor):
            def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
                outcome = super().act(stage, dispatch)
                if stage.id != "continuous_review":
                    return outcome
                receipt = dict(outcome.receipt or {})
                receipt["verdict"] = "REJECT"  # while the transition claims APPROVE
                return StageOutcome(event="APPROVE", receipt=receipt, produced=outcome.produced)

        state = run(make_deps(actor=Contradictory()))
        assert state["terminal"] == TERMINAL_FAULT
        assert "event binding" in state["terminal_reason"]

    def test_a_review_cannot_answer_success_and_skip_its_own_verdict(self) -> None:
        """The spine must not become a way around the verdict edges.

        `continuous_review` declares APPROVE and REJECT. If reporting the
        plain spine event fell through to the derived edge, a review could
        reach the next stage without a receipt and without either binding
        being checked -- an approval nobody gave.
        """
        actor = ContractActor()  # every stage answers with the spine event
        state = run(make_deps(actor=actor))

        assert state["terminal"] == TERMINAL_FAULT
        assert "no declared transition" in state["terminal_reason"]
        assert [stage for stage, _ in actor.calls] == [
            "configure",
            "implement",
            "continuous_review",
        ]

    def test_an_undeclared_verdict_is_a_fault(self) -> None:
        actor = ContractActor({"continuous_review": ["MAYBE"]})
        state = run(make_deps(actor=actor))
        assert state["terminal"] == TERMINAL_FAULT
        assert "no declared transition" in state["terminal_reason"]


class TestFailureExits:
    def test_a_non_retryable_failure_ends_the_pipeline(self) -> None:
        class Failing(ContractActor):
            def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
                if stage.id != "implement":
                    return super().act(stage, dispatch)
                return StageOutcome(event="failed", failure_code="DIRTY_WORKTREE")

        state = run(make_deps(actor=Failing()))
        assert state["terminal"] == TERMINAL_FAILED
        assert "DIRTY_WORKTREE" in state["terminal_reason"]

    def test_a_retryable_failure_is_retried_within_its_bound(self) -> None:
        attempts: list[str] = []

        class Flaky(ContractActor):
            def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
                if stage.id != "implement":
                    return super().act(stage, dispatch)
                attempts.append(stage.id)
                if len(attempts) < 3:
                    return StageOutcome(event="failed", failure_code="PROVIDER_UNAVAILABLE")
                return super().act(stage, dispatch)

        actor = Flaky({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run(make_deps(actor=actor, bounds=PipelineBounds(max_retries=2)))

        assert state["terminal"] == TERMINAL_COMPLETE
        assert len(attempts) == 3

    def test_retries_stop_at_the_bound(self) -> None:
        class AlwaysDown(ContractActor):
            def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
                if stage.id != "implement":
                    return super().act(stage, dispatch)
                return StageOutcome(event="failed", failure_code="PROVIDER_UNAVAILABLE")

        state = run(make_deps(actor=AlwaysDown(), bounds=PipelineBounds(max_retries=2)))
        assert state["terminal"] == TERMINAL_FAILED
        assert "2 bounded retries" in state["terminal_reason"]

    def test_a_failure_exit_never_materialises(self) -> None:
        sealer = Sealer()

        class Failing(ContractActor):
            def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
                if stage.id != "implement":
                    return super().act(stage, dispatch)
                return StageOutcome(event="failed", failure_code="DIRTY_WORKTREE")

        run(make_deps(actor=Failing(), materializer=sealer))
        assert all("implement" not in commit for commit in sealer.commits)


class TestCapabilityLockGatesEveryActor:
    def test_a_tampered_bundle_stops_the_pipeline_before_the_actor_runs(
        self, tmp_path: Path
    ) -> None:
        for src in CapabilityLock.load().contracts_dir.iterdir():
            if src.is_file():
                (tmp_path / src.name).write_bytes(src.read_bytes())
        target = tmp_path / "development-lifecycle.json"
        target.write_bytes(target.read_bytes() + b"\n")

        actor = ContractActor()
        state = run(make_deps(actor=actor, capability=CapabilityLock.load(tmp_path)))

        assert state["terminal"] == TERMINAL_FAULT
        assert "capability lock failed" in state["terminal_reason"]
        assert actor.calls == []

    def test_the_shipped_bundle_passes(self) -> None:
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run(make_deps(actor=actor, capability=CapabilityLock.load()))
        assert state["terminal"] == TERMINAL_COMPLETE


class TestTheHumanGate:
    """The graph may wait for a verdict; it may never supply one."""

    def _gate_deps(self, board: dict[str, Any]) -> PipelineDeps:
        base = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})

        class Gate:
            def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
                decision = board.get("decision")
                if decision is None:
                    board["asked"] = board.get("asked", 0) + 1
                    raise GatePending({"question_note_id": "note-1", "card_entity_id": "card-1"})
                if decision == "REJECT":
                    raise StageRefused("gate decision REJECT by a human")
                return base.act(stage, dispatch)

        scripts = {
            name: base for name, stage in Lifecycle.load().stages.items() if not stage.is_llm
        }
        scripts["human_gate"] = Gate()
        return make_deps(actor=base, scripts=scripts)

    def _compiled(self, deps: PipelineDeps) -> Any:
        return build_dd_pipeline_graph(deps).compile(checkpointer=InMemorySaver())

    def _start(self) -> Any:
        return initial_state(
            development_id="dev-1",
            stage="configure",
            head_commit=SPEC_COMMIT,
            artifacts={"spec": SPEC_COMMIT},
        )

    def test_an_unanswered_question_suspends_the_graph(self) -> None:
        board: dict[str, Any] = {}
        compiled = self._compiled(self._gate_deps(board))
        config = {"configurable": {"thread_id": "t1"}, "recursion_limit": 200}

        state = compiled.invoke(self._start(), config=config)

        assert state.get("terminal") is None, "a pending gate is not a terminal state"
        assert state["__interrupt__"], "the graph must suspend, not proceed"
        assert board["asked"] == 1

    def test_resuming_does_not_cast_the_vote(self) -> None:
        """Whoever resumes the graph must not be able to decide by resuming it."""
        board: dict[str, Any] = {}
        compiled = self._compiled(self._gate_deps(board))
        config = {"configurable": {"thread_id": "t2"}, "recursion_limit": 200}

        compiled.invoke(self._start(), config=config)
        state = compiled.invoke(Command(resume="APPROVE"), config=config)

        assert state.get("terminal") is None
        assert state["__interrupt__"], "the resume value is not a verdict"

    def test_a_decision_on_the_board_lets_it_through(self) -> None:
        board: dict[str, Any] = {}
        compiled = self._compiled(self._gate_deps(board))
        config = {"configurable": {"thread_id": "t3"}, "recursion_limit": 200}

        compiled.invoke(self._start(), config=config)
        board["decision"] = "APPROVE"
        state = compiled.invoke(None, config=config)

        assert state["terminal"] == TERMINAL_COMPLETE

    def test_a_rejection_ends_the_pipeline_without_calling_it_a_fault(self) -> None:
        board: dict[str, Any] = {"decision": "REJECT"}
        compiled = self._compiled(self._gate_deps(board))
        config = {"configurable": {"thread_id": "t4"}, "recursion_limit": 200}

        state = compiled.invoke(self._start(), config=config)

        assert state["terminal"] == TERMINAL_REFUSED
        assert state.get("fault") is False
        assert "REJECT" in state["terminal_reason"]


def executable_source(path: Path) -> str:
    """The module's code with every comment and string literal removed.

    Prose may name a stage; code may not. Searching the raw file would only
    prove the docstrings are shy.
    """
    kept: list[str] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    return " ".join(kept)


class TestNoSecondDescriptionOfTheMachine:
    """One table, not two. The walker must know no stage by name."""

    def test_no_stage_name_is_hardcoded_in_the_walker(self) -> None:
        from fleet_graph.graphs import dd_pipeline as module

        body = executable_source(Path(module.__file__))
        for stage in Lifecycle.load().stages:
            assert stage not in body, f"{stage} is hardcoded in dd_pipeline.py"

    def test_no_verdict_is_hardcoded_in_the_walker(self) -> None:
        from fleet_graph.graphs import dd_pipeline as module

        body = executable_source(Path(module.__file__))
        for verdict in ("APPROVE", "REJECT"):
            assert verdict not in body, f"{verdict} is hardcoded in dd_pipeline.py"

    def test_the_check_would_catch_a_hardcoded_name(self, tmp_path: Path) -> None:
        """The stripper must not be so eager that it passes everything."""
        sample = tmp_path / "sample.py"
        sample.write_text('x = "implement"  # implement\nif y == "acceptance": pass\n')
        assert "implement" not in executable_source(sample)

        sample.write_text("implement = 1\n")
        assert "implement" in executable_source(sample)
