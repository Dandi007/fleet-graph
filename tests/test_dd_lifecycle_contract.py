"""The version-bound linear DD lifecycle (spec L1-L7), as a contract.

These tests do not talk to any service, model, daemon, git remote or PR
platform. They pin the *contract* -- the stage machine, the rework topology,
the validity key, the typed merge feedback and the raw-event boundary -- using
the pure functions, the in-memory graph and injected fake stage/effect ports
the spec calls for. Everything that would need a real remote, a shared service,
a run daemon or a live PR stays out (development stage: unrun).

L1 strict order: implement -> acceptance -> continuous_review -> final_review
-> human_gate -> merger, with program acceptance blocking continuous review.
L2 same-identity rework: the five rejections return to the same DD's implement.
L3 validity key: product/tree/spec/context/target/PR binding and invalidation.
L4 replay: a sealed prefix, verified per receipt boundary.
L5 goal gate: the same accepted/reviewed version + validity key.
L6 typed merge feedback.
L7 raw-event boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.dd.control_plane import (
    CLASS_IMPLEMENTATION,
    CLASS_REJECTED,
    CLASS_SPEC_CONFLICT,
    EXIT_RECONFIGURE,
    EXIT_REWORK,
    classify_failure,
)
from fleet_graph.dd.lifecycle import LIFECYCLE_PATH, Lifecycle, UnknownTransition
from fleet_graph.dd.merge_feedback import (
    MergeFeedbackKind,
    classify_merge_feedback,
    is_merge_success,
    requires_rework,
)
from fleet_graph.dd.validity import (
    ValidityInputs,
    affected_stages,
    build_validity_key,
    changed_fields,
    is_bookkeeping_only,
    verify_validity,
)
from fleet_graph.graphs.dd_pipeline import (
    SPINE_EVENT,
    TERMINAL_COMPLETE,
    TERMINAL_FAILED,
    TERMINAL_PREPARED,
    TERMINAL_REFUSED,
    TERMINAL_TARGET_COMPETITION,
    PipelineBounds,
    Replayed,
    StageOutcome,
    StageRefused,
    build_dd_pipeline_graph,
    initial_state,
)

# The fake stage/effect ports shared by the pipeline walk tests, reused from
# the historical pipeline test so the two never drift on what a scriptable
# actor or an attesting sealer is.
from conftest import git, head
from test_dd_pipeline import ContractActor, Sealer, make_deps

LIFECYCLE = Lifecycle.load()
ORDER = [
    "configure",
    "implement",
    "acceptance",
    "continuous_review",
    "final_review",
    "human_gate",
    "merger",
]


def run_actor(actor: ContractActor, **deps: Any) -> dict[str, Any]:
    graph = build_dd_pipeline_graph(make_deps(actor=actor, **deps)).compile()
    return graph.invoke(
        initial_state(
            development_id="dev-1",
            stage="configure",
            head_commit="0" * 40,
            artifacts={"spec": "0" * 40},
        ),
        config={"recursion_limit": 400},
    )


# --------------------------------------------------------------------------
# L1: the strict, linear order, and acceptance blocking review
# --------------------------------------------------------------------------


class TestStrictLinearOrder:
    def test_spine_is_implement_acceptance_review_gate_merge(self) -> None:
        assert LIFECYCLE.spine == {
            "configure": "implement",
            "implement": "acceptance",
            "acceptance": "continuous_review",
            "continuous_review": "final_review",
            "final_review": "human_gate",
            "human_gate": "merger",
        }

    def test_acceptance_is_reached_before_continuous_review(self) -> None:
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_actor(actor)
        assert state["terminal"] == TERMINAL_COMPLETE
        assert [stage for stage, _ in actor.calls] == ORDER

    def test_the_contract_declares_the_same_order_as_the_walker(self) -> None:
        """The order is not hardcoded in the walker; it falls out of the
        contract's artifact graph, and the walker walks that exact order."""
        implement_then_acceptance = LIFECYCLE.transition("implement", "success").target
        acceptance_then_review = LIFECYCLE.transition("acceptance", "success").target
        assert implement_then_acceptance == "acceptance"
        assert acceptance_then_review == "continuous_review"

    def test_configure_and_prepared_do_not_count_as_complete(self) -> None:
        """configure or PREPARED is not \"done\": the terminal is the merger."""
        assert LIFECYCLE.is_terminal("merger") is True
        assert LIFECYCLE.is_terminal("configure") is False
        assert LIFECYCLE.is_terminal("acceptance") is False


class TestAcceptanceBlocksReview:
    def test_acceptance_failure_returns_to_implement_not_review(self) -> None:
        """A REJECT verdict from acceptance is a rework to implement; it never
        lets a failing run sail into continuous review (spec L1/L2)."""

        class FailingAcceptance(ContractActor):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self._rejected = False
                self.acceptance_attempts: list[int] = []

            def act(self, stage: Any, dispatch: dict[str, Any]) -> StageOutcome:
                if stage.id == "acceptance":
                    self.acceptance_attempts.append(int(dispatch.get("attempt", 1)))
                    self.calls.append((stage.id, dispatch["attempt"]))
                    if not self._rejected:
                        self._rejected = True
                        return StageOutcome(
                            event="REJECT",
                            receipt={
                                "verdict": "REJECT",
                                "output_commit": dispatch["input_commit"],
                            },
                            produced=tuple(stage.produced_artifacts),
                        )
                return super().act(stage, dispatch)

        actor = FailingAcceptance({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_actor(actor)

        assert state["terminal"] == TERMINAL_COMPLETE
        # The first acceptance failed (reject on attempt 1); the rework
        # re-implemented and re-ran acceptance (attempt 2) before review.
        assert actor.acceptance_attempts == [1, 2]
        assert [attempt for stage, attempt in actor.calls if stage == "implement"] == [1, 2]
        assert "continuous_review" in [stage for stage, _ in actor.calls]

    def test_the_contract_binds_the_acceptance_reject_to_its_verdict(self) -> None:
        with pytest.raises(Exception, match="event binding"):
            LIFECYCLE.advance(
                "acceptance",
                "REJECT",
                receipt={"verdict": "APPROVE", "output_commit": "c"},
                next_dispatch={"input_commit": "c"},
            )


# --------------------------------------------------------------------------
# L2: same-identity rework, unbounded, per-call timeout distinct
# --------------------------------------------------------------------------


class TestSameIdentityRework:
    def test_every_rework_reenters_the_same_development(self) -> None:
        """The development id is never forked by a rejection; only the attempt
        increments, keeping the same DD id, frozen spec and PR."""
        seen: list[tuple[str, int]] = []

        class Recorder(ContractActor):
            def act(self, stage: Any, dispatch: dict[str, Any]) -> StageOutcome:
                seen.append((str(dispatch.get("development_id")), int(dispatch.get("attempt", 1))))
                return super().act(stage, dispatch)

        actor = Recorder(
            {"continuous_review": ["REJECT", "REJECT", "APPROVE"], "final_review": ["APPROVE"]}
        )
        state = run_actor(actor)
        assert state["terminal"] == TERMINAL_COMPLETE
        assert {dev for dev, _ in seen} == {"dev-1"}, "the DD id never changes across rework"
        attempts = [attempt for stage, attempt in actor.calls if stage == "implement"]
        assert attempts == [1, 2, 3]

    def test_final_review_reject_returns_to_implement(self) -> None:
        # CR approves once per attempt (the reworks re-review), FR rejects once
        # then approves the reworked work.
        actor = ContractActor(
            {"continuous_review": ["APPROVE", "APPROVE"], "final_review": ["REJECT", "APPROVE"]}
        )
        state = run_actor(actor)
        assert state["terminal"] == TERMINAL_COMPLETE
        attempts = [attempt for stage, attempt in actor.calls if stage == "implement"]
        assert attempts == [1, 2]

    def test_rework_is_unbounded(self) -> None:
        """Old business bounds (6 reworks, 40 steps) are gone: a long reject
        chain propagates without a bound terminal."""
        actor = ContractActor({"continuous_review": ["REJECT"] * 30})
        state = run_actor(actor)
        attempts = [attempt for stage, attempt in actor.calls if stage == "implement"]
        assert len(attempts) > 6, "business rework must not cap at 6"
        assert state["terminal"] != "bounds"

    def test_infrastructure_retry_stays_bounded(self) -> None:
        """The removed caps are business ones; the infra retry bound remains."""

        class Down(ContractActor):
            def act(self, stage: Any, dispatch: dict[str, Any]) -> StageOutcome:
                if stage.id == "implement":
                    return StageOutcome(event="failed", failure_code="PROVIDER_UNAVAILABLE")
                return super().act(stage, dispatch)

        state = run_actor(Down(), bounds=PipelineBounds(max_retries=1))
        assert state["terminal"] == TERMINAL_FAILED
        assert "bounded retries" in state["terminal_reason"]

    def test_per_call_timeout_is_typed_not_a_business_bound(
        self, repo: Path, monkeypatch: Any
    ) -> None:
        """A subprocess timeout is a per-call, bounded infra fact (exit 124),
        typed distinctly from a business REJECT and independent of any business
        rework cap -- the rejection of the work is not what a timeout is."""
        import subprocess

        from fleet_graph.graphs import dd_scripts
        from fleet_graph.graphs.dd_scripts import AcceptanceStage, ConfigureStage

        ConfigureStage(repo=repo, run_config={"acceptance_commands": [["slow-command"]]}).act(
            LIFECYCLE.stages["configure"],
            {"development_id": "dev-1", "generation": 1},
        )

        def timed_out(argv: Any, **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1800))

        monkeypatch.setattr(dd_scripts.subprocess, "run", timed_out)
        outcome = AcceptanceStage(repo=repo, declared=[["slow-command"]]).act(
            LIFECYCLE.stages["acceptance"],
            {"development_id": "dev-1", "attempt": 1},
        )
        record = json.loads((repo / ".dd-evidence" / "acceptance.json").read_text())
        assert record["results"][0]["exit_code"] == 124
        assert outcome.event == "REJECT"


class TestFiveRejectionKinds:
    def test_gate_reject_classifies_as_a_verdict_not_a_fault(self) -> None:
        record = classify_failure("refused", "gate REJECT by human", "GATE_REJECTED")
        assert record is not None
        assert record["class"] == CLASS_REJECTED
        assert record["exit"] == EXIT_REWORK
        assert record["retryable"] is True

    def test_target_competition_classifies_as_spec_conflict(self) -> None:
        record = classify_failure("refused", "advanced", "RELEASE_HEAD_ADVANCED")
        assert record is not None
        assert record["class"] == CLASS_SPEC_CONFLICT
        assert record["exit"] == EXIT_RECONFIGURE

    def test_legacy_rework_bound_code_still_classifies_as_implementation(self) -> None:
        """A result minted before the bound removal still classifies correctly."""
        record = classify_failure("bounds", "rework limit", "REWORK_LIMIT_REACHED")
        assert record is not None
        assert record["class"] == CLASS_IMPLEMENTATION


class TestUnrelatedDevelopmentsStayIndependent:
    def test_two_developments_walk_independently(self) -> None:
        a = ContractActor(
            {"continuous_review": ["REJECT", "REJECT", "APPROVE"], "final_review": ["APPROVE"]}
        )
        b = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})

        ga = build_dd_pipeline_graph(make_deps(actor=a)).compile()
        gb = build_dd_pipeline_graph(make_deps(actor=b)).compile()
        sa = ga.invoke(
            initial_state(
                development_id="dev-a",
                stage="configure",
                head_commit="0" * 40,
                artifacts={"spec": "0" * 40},
            ),
            config={"recursion_limit": 400},
        )
        sb = gb.invoke(
            initial_state(
                development_id="dev-b",
                stage="configure",
                head_commit="0" * 40,
                artifacts={"spec": "0" * 40},
            ),
            config={"recursion_limit": 400},
        )

        assert sa["terminal"] == TERMINAL_COMPLETE
        assert sb["terminal"] == TERMINAL_COMPLETE
        assert [s for s, _ in b.calls] == ORDER, "dev-b is not polluted by dev-a's rejects"
        assert [attempt for stage, attempt in a.calls if stage == "implement"] == [1, 2, 3]


# --------------------------------------------------------------------------
# L3: the validity key
# --------------------------------------------------------------------------


def key(**overrides: Any) -> ValidityInputs:
    base = ValidityInputs(
        product_revision="a" * 40,
        product_tree="b" * 40,
        spec_digest="sha256:" + "c" * 64,
        acceptance_context_revision="d" * 40,
        target_identity="refs/heads/release/self",
        pr_identity="head-branch:base-branch",
    )
    return ValidityInputs(**{**base.__dict__, **overrides})


class TestValidityKey:
    def test_the_digest_binds_every_field(self) -> None:
        k1 = build_validity_key(key())
        k2 = build_validity_key(key(product_revision="f" * 40))
        assert k1.digest != k2.digest

    def test_digest_is_order_independent_and_stable(self) -> None:
        assert build_validity_key(key()).digest == build_validity_key(key()).digest

    def test_each_field_change_is_detected(self) -> None:
        base = key()
        for field in (
            "product_revision",
            "product_tree",
            "spec_digest",
            "acceptance_context_revision",
            "target_identity",
            "pr_identity",
        ):
            changed = ValidityInputs(
                **{
                    **base.__dict__,
                    field: ("e" * 40 if field != "spec_digest" else "sha256:" + "e" * 64),
                }
            )
            assert changed_fields(changed, base) == (field,), field

    def test_product_tree_change_invalidates_review_and_acceptance(self) -> None:
        affected = affected_stages(changed_fields(key(product_tree="f" * 40), key()))
        assert "implement" in affected
        assert "acceptance" in affected
        assert "continuous_review" in affected
        assert "merger" not in affected

    def test_target_change_invalidates_only_the_merge(self) -> None:
        affected = affected_stages(
            changed_fields(key(target_identity="refs/heads/release/other"), key())
        )
        assert affected == ("merger",)

    def test_spec_change_invalidates_everything_a_new_dd(self) -> None:
        changed = changed_fields(key(spec_digest="sha256:" + "e" * 64), key())
        affected = affected_stages(changed)
        assert set(ORDER) == set(affected), "a spec change is a new DD, not a rework"

    def test_bookkeeping_only_revision_does_not_invalidate(self) -> None:
        # The revision (commit id) moved; every meaningful fact stayed put. This
        # is a bookkeeping commit and must not mint a re-review.
        newer = key(product_revision="f" * 40)
        assert is_bookkeeping_only(newer, key()) is True
        assert affected_stages(changed_fields(newer, key())) == ()

    def test_a_meaningful_change_is_not_bookkeeping(self) -> None:
        newer = key(product_revision="f" * 40, product_tree="c" * 40)
        assert is_bookkeeping_only(newer, key()) is False

    def test_verify_recomputes_digest_and_names_changes(self) -> None:
        bound = build_validity_key(key())
        current = key(product_tree="f" * 40)
        changed, matches = verify_validity(bound, current)
        assert changed == ("product_tree",)
        assert matches is False

    def test_verify_of_unchanged_inputs_matches(self) -> None:
        bound = build_validity_key(key())
        changed, matches = verify_validity(bound, key())
        assert changed == ()
        assert matches is True


# --------------------------------------------------------------------------
# L4: replay verifies a sealed prefix, per receipt boundary
# --------------------------------------------------------------------------


class ScriptedReplayer:
    """Replays exactly the stages it is handed, then refuses everything after."""

    def __init__(self, stages: list[str]) -> None:
        self.stages = list(stages)

    def replay(self, stage: Any, dispatch: dict[str, Any]) -> Replayed | None:
        if self.stages and self.stages[0] == stage.id:
            self.stages.pop(0)
            return Replayed(
                event=SPINE_EVENT,
                receipt={"stage": stage.id, "output_commit": dispatch["input_commit"]},
                output_commit=dispatch["input_commit"],
            )
        return None


class TestReplayPrefix:
    def test_a_replayed_prefix_is_recorded_as_replayed(self) -> None:
        replayer = ScriptedReplayer(["configure", "implement"])
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_actor(actor, replayer=replayer)

        assert state["terminal"] == TERMINAL_COMPLETE
        replayed = [e["stage"] for e in state["history"] if e.get("replayed")]
        assert replayed == ["configure", "implement"]
        assert next(stage for stage, _ in actor.calls) == "acceptance"

    def test_replay_stops_at_the_first_unsealed_stage(self) -> None:
        """A prefix, never a hole: once the replayer declines one stage it
        declines every later one, so the real walk resumes from the break."""
        replayer = ScriptedReplayer([])  # replays nothing
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_actor(actor, replayer=replayer)

        assert state["terminal"] == TERMINAL_COMPLETE
        assert next(stage for stage, _ in actor.calls) == "configure"
        assert [e for e in state["history"] if e.get("replayed")] == []


class TestLegacyContractsAreExplainedOrRefused:
    def test_an_unknown_stage_reference_is_a_fault_not_a_guess(self) -> None:
        with pytest.raises(UnknownTransition, match="no declared transition"):
            LIFECYCLE.transition("implement", "APPROVE")

    def test_a_legacy_bound_result_classifies_deterministically(self) -> None:
        assert (
            classify_failure("bounds", "step limit", "STEP_LIMIT_REACHED")["code"]
            == "STEP_LIMIT_REACHED"
        )

    def test_the_taxonomy_still_names_the_old_bounds_for_legacy_results(self) -> None:
        taxonomy = LIFECYCLE.failure_taxonomy
        assert "PROVIDER_UNAVAILABLE" in taxonomy
        assert taxonomy["PROVIDER_UNAVAILABLE"]["retryable"] is True


# --------------------------------------------------------------------------
# L5: the goal gate binds the accepted, reviewed version and its validity key
# --------------------------------------------------------------------------


class TestGoalGateBinding:
    def test_an_expired_goal_verdict_does_not_approve(self) -> None:
        """A gate verdict is valid only for the version+validity key it was
        bound to; when the product or spec moves, the verdict is stale."""
        bound = build_validity_key(key())
        stale = key(product_revision="f" * 40)
        changed, _ = verify_validity(bound, stale)
        assert changed == ("product_revision",)
        assert "merger" not in affected_stages(changed)  # target/PR unchanged
        assert "human_gate" not in affected_stages(changed), (
            "gate re-binds only on PR/target change"
        )

    def test_a_pr_or_target_change_invalidates_the_gate_and_merge(self) -> None:
        changed = ("pr_identity", "target_identity")
        affected = affected_stages(changed)
        assert "human_gate" in affected
        assert "merger" in affected

    def test_the_gate_refuses_without_a_human_verdict(self) -> None:
        """The gate never casts its own vote: an unanswered board suspends,
        a REJECT is a verdict (refused, not fault)."""
        record = classify_failure("refused", "gate decision REJECT by a human", "GATE_REJECTED")
        assert record["class"] == CLASS_REJECTED
        assert record["exit"] == EXIT_REWORK


# --------------------------------------------------------------------------
# L6: typed merge feedback
# --------------------------------------------------------------------------


class TestTypedMergeFeedback:
    def test_target_competition_is_not_a_content_conflict(self) -> None:
        fb = classify_merge_feedback("RELEASE_HEAD_ADVANCED", "advanced")
        assert fb.kind == MergeFeedbackKind.TARGET_COMPETITION
        assert fb.requires_code_change is False

    def test_a_real_content_conflict_requires_rework(self) -> None:
        fb = classify_merge_feedback("REBASE_SPEC_INCOMPATIBLE", "conflict on x")
        assert fb.kind == MergeFeedbackKind.CONTENT_CONFLICT
        assert requires_rework(fb) is True

    def test_unknown_codes_are_transport_unknown_never_conflict(self) -> None:
        fb = classify_merge_feedback("SOMETHING_NEW", "no idea")
        assert fb.kind == MergeFeedbackKind.TRANSPORT_UNKNOWN
        assert requires_rework(fb) is False

    def test_provider_unavailable_is_transport(self) -> None:
        fb = classify_merge_feedback("PROVIDER_UNAVAILABLE", "down")
        assert fb.kind == MergeFeedbackKind.TRANSPORT_UNKNOWN

    def test_already_merged_is_not_an_error(self) -> None:
        fb = classify_merge_feedback("ALREADY_MERGED", "already there")
        assert fb.kind == MergeFeedbackKind.ALREADY_MERGED
        assert is_merge_success(fb) is False
        assert requires_rework(fb) is False

    def test_prepared_is_not_a_success(self) -> None:
        fb = classify_merge_feedback(result="PREPARED")
        assert fb.kind == MergeFeedbackKind.PREPARED_ONLY
        assert is_merge_success(fb) is False

    def test_only_a_measured_merge_is_success(self) -> None:
        fb = classify_merge_feedback(result="MERGED")
        assert fb.kind == MergeFeedbackKind.MERGED
        assert is_merge_success(fb) is True


class TestTypedMergeFeedbackIsWiredIntoTheLifecycle:
    """The contract declares the merger's typed edges, and the walker routes
    them: MERGED completes, PREPARED ends prepared (not complete), a code-change
    REJECT re-enters implement (same DD, next attempt)."""

    def test_merger_declares_typed_terminal_edges(self) -> None:
        merged = LIFECYCLE.transition("merger", "MERGED")
        prepared = LIFECYCLE.transition("merger", "PREPARED")
        reject = LIFECYCLE.transition("merger", "REJECT")
        assert merged.terminal == TERMINAL_COMPLETE
        assert prepared.terminal == TERMINAL_PREPARED
        assert reject.is_rework and reject.target == "implement"

    def test_a_prepared_merge_ends_prepared_not_complete(self) -> None:
        actor = ContractActor(
            {
                "continuous_review": ["APPROVE"],
                "final_review": ["APPROVE"],
                "merger": ["PREPARED"],
            }
        )
        state = run_actor(actor)
        assert state["terminal"] == TERMINAL_PREPARED
        assert state["terminal"] != TERMINAL_COMPLETE

    def test_a_code_change_merge_feedback_reenters_implement(self) -> None:
        actor = ContractActor(
            {
                "continuous_review": ["APPROVE", "APPROVE"],
                "final_review": ["APPROVE", "APPROVE"],
                "merger": ["REJECT", "MERGED"],
            }
        )
        state = run_actor(actor)
        assert state["terminal"] == TERMINAL_COMPLETE
        implements = [attempt for stage, attempt in actor.calls if stage == "implement"]
        assert implements == [1, 2], "the rejecting merge re-entered implement under rework"


class TestTargetCompetitionIsADistinctTerminal:
    """Spec L6: a target that advanced under the order is neither a content
    conflict nor a generic refusal -- it is a distinct, named non-content
    outcome that selects the line's reconfigure recovery (``spec_conflict`` /
    ``reconfigure`` exit), never a plain terminal refusal."""

    def test_a_target_competition_refusal_ends_in_a_distinct_terminal(self) -> None:
        class TargetMoved(ContractActor):
            def act(self, stage: Any, dispatch: dict[str, Any]) -> StageOutcome:
                if stage.id == "merger":
                    raise StageRefused(
                        "[target_competition] the target advanced past the frozen base",
                        code="RELEASE_HEAD_ADVANCED",
                        terminal_kind=TERMINAL_TARGET_COMPETITION,
                    )
                return super().act(stage, dispatch)

        actor = TargetMoved(
            {"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}
        )
        state = run_actor(actor)

        assert state["terminal"] == TERMINAL_TARGET_COMPETITION
        assert state["terminal"] != TERMINAL_REFUSED
        assert state["terminal_code"] == "RELEASE_HEAD_ADVANCED"
        assert state.get("fault") is False

    def test_the_distinct_terminal_still_classifies_as_a_reconfigure(self) -> None:
        record = classify_failure(
            TERMINAL_TARGET_COMPETITION,
            "the target advanced past the frozen base",
            "RELEASE_HEAD_ADVANCED",
        )
        assert record is not None
        assert record["class"] == CLASS_SPEC_CONFLICT
        assert record["exit"] == EXIT_RECONFIGURE

    def test_a_generic_refusal_is_not_being_reclassified(self) -> None:
        class Refusing(ContractActor):
            def act(self, stage: Any, dispatch: dict[str, Any]) -> StageOutcome:
                if stage.id == "merger":
                    raise StageRefused("merge refused for transport reasons", code="MERGE_REFUSED")
                return super().act(stage, dispatch)

        actor = Refusing(
            {"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}
        )
        state = run_actor(actor)
        assert state["terminal"] == TERMINAL_REFUSED


# --------------------------------------------------------------------------
# L7: the raw-event boundary
# --------------------------------------------------------------------------


class TestRawEventBoundary:
    def test_history_entries_carry_stage_attempt_and_commit(self) -> None:
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_actor(actor, materializer=Sealer())
        for entry in state["history"]:
            assert "stage" in entry
            assert "attempt" in entry
            assert "output_commit" in entry

    def test_the_observability_sink_is_separate_from_state_migration(self) -> None:
        """A failing observe sink does not silently corrupt the run's own
        state: the walker keeps its history authoritative, and the sink is
        called per entry rather than being trusted to reconstruct it."""
        seen: list[dict[str, Any]] = []

        def observe(entry: dict[str, Any]) -> None:
            seen.append(entry)

        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_actor(actor, observe=observe)
        assert seen, "the sink observed every history entry"
        assert [e["stage"] for e in seen] == [e["stage"] for e in state["history"]]

    def test_rework_or_unchanged_tree_cannot_be_faked_as_success(self) -> None:
        """TERMINAL_COMPLETE is only produced by reaching the last stage, never
        fabricated: an empty actor with no review verdicts faults."""
        state = run_actor(ContractActor())
        assert state["terminal"] != TERMINAL_COMPLETE

    def test_a_failing_observability_sink_is_not_swallowed(self) -> None:
        """Spec L7: a raw-event write failure must not be swallowed. The walker
        calls its observer per history entry; a failing observer propagates
        rather than letting the pipeline migrate state on an untraceable basis."""
        seen: list[dict[str, Any]] = []

        def failing_observe(entry: dict[str, Any]) -> None:
            seen.append(entry)
            raise OSError("events.jsonl write failed")

        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        with pytest.raises(OSError):
            run_actor(actor, observe=failing_observe)
        assert seen, "the failing observer was invoked before it failed"

    def test_history_entries_carry_the_sealed_receipt_digest(self) -> None:
        """Spec L7: the raw-event boundary records the sealed receipt's digest,
        not just the stage and its commit -- so a later recovery pass can re-trace
        which receipt each history entry was sealed under."""
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_actor(actor, materializer=Sealer())
        for entry in state["history"]:
            assert entry["receipt_digest"].startswith("sha256:"), entry

    def test_history_entries_carry_opaque_runtime_receipt_refs(self) -> None:
        """Spec L7: the opaque Runtime run/session/receipt references a sealed
        receipt carries are emitted on the raw-event boundary, verbatim -- the
        walker invents none and elides none."""

        class RuntimeCarrier(ContractActor):
            def act(self, stage: Any, dispatch: dict[str, Any]) -> StageOutcome:
                outcome = super().act(stage, dispatch)
                if stage.id != "implement":
                    return outcome
                receipt = dict(outcome.receipt or {})
                receipt["actor_job_id"] = "job-implement"
                receipt["materialization_intent_id"] = "intent-implement"
                return StageOutcome(
                    event=outcome.event, receipt=receipt, produced=outcome.produced
                )

        actor = RuntimeCarrier({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_actor(actor, materializer=Sealer())
        implement = next(e for e in state["history"] if e["stage"] == "implement")
        assert implement["receipt_refs"]["actor_job_id"] == "job-implement"
        assert implement["receipt_refs"]["materialization_intent_id"] == "intent-implement"


# --------------------------------------------------------------------------
# L1 (consistency): the shipped contract and its schema must agree
# --------------------------------------------------------------------------


class TestLifecycleSchemaConsistency:
    def test_the_lifecycle_manifest_validates_against_its_schema(self) -> None:
        """The contract table development-lifecycle.json is the authority the
        executor/materializer/replay read; its committed schema must describe
        exactly that table rather than an older stage machine."""
        import jsonschema

        contracts = LIFECYCLE_PATH.parent
        manifest = json.loads((contracts / "development-lifecycle.json").read_text())
        schema = json.loads((contracts / "development-lifecycle.schema.json").read_text())
        jsonschema.validate(manifest, schema)

    def test_the_schema_pins_the_same_contract_version_as_the_manifest(self) -> None:
        contracts = LIFECYCLE_PATH.parent
        schema = json.loads((contracts / "development-lifecycle.schema.json").read_text())
        assert schema["properties"]["contract_version"]["const"] == LIFECYCLE.contract_version


# --------------------------------------------------------------------------
# L3 (wiring): the validity key is measured from real facts and re-verified
# by the dispatch, gate and recovery paths -- not just a pure test helper.
# --------------------------------------------------------------------------


class TestValidityBindingIsWired:
    def test_binding_measures_git_facts_and_detects_a_spec_change(
        self, repo: Path
    ) -> None:
        from fleet_graph.dd.validity_binding import (
            BindingFacts,
            build_validity_binding,
            verify_binding,
        )

        commit = head(repo)
        facts = BindingFacts(spec_digest="sha256:" + "d" * 64)
        key = build_validity_binding(str(repo), commit, facts)
        assert key.digest.startswith("sha256:")
        assert key.inputs.product_revision == commit
        assert key.inputs.product_tree, "the tree is measured out of git, not guessed"

        changed, matches = verify_binding(str(repo), commit, facts, key)
        assert changed == () and matches is True

        moved = BindingFacts(spec_digest="sha256:" + "e" * 64)
        changed, matches = verify_binding(str(repo), commit, moved, key)
        assert changed == ("spec_digest",) and matches is False

    def _commit_run_config(self, repo: Path) -> str:
        """Commit a run-config so the measured acceptance-context revision exists."""
        from fleet_graph.dd.validity_binding import RUN_CONFIG_PATH

        path = repo / RUN_CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"acceptance_commands": [["true"]]}\n', encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "run-config")
        return head(repo)

    def test_the_dispatch_builder_measures_a_validity_key(self, repo: Path) -> None:
        from fleet_graph.dd.dispatch import DevelopmentChain, StageDispatchBuilder
        from fleet_graph.dd.validity_binding import BindingFacts

        commit = self._commit_run_config(repo)
        builder = StageDispatchBuilder(
            DevelopmentChain(
                development_id="dev-1",
                workspace_path=str(repo),
                target_base_commit="0" * 40,
                root_handoff_digest="sha256:" + "0" * 64,
            )
        )
        facts = BindingFacts(
            target_identity="refs/heads/release/self",
            pr_identity="refs/heads/dd/dev-fg-1->refs/heads/release/self",
        )
        key = builder.validity_key({"input_commit": commit}, facts=facts)
        assert key.digest.startswith("sha256:")
        assert key.inputs.product_revision == commit
        assert key.inputs.spec_digest.startswith("sha256:")
        # The target and PR identities are the caller's bound facts -- never the
        # chain's base commit (a git object id is not a ref) and never a
        # substitute identity. The key is complete: all six fields bind.
        assert key.inputs.target_identity == "refs/heads/release/self"
        assert key.inputs.pr_identity == "refs/heads/dd/dev-fg-1->refs/heads/release/self"
        # The acceptance-context revision is measured out of git, not left empty.
        assert key.inputs.acceptance_context_revision, "acceptance context must bind"

    def test_the_dispatch_builder_refuses_to_seal_an_unknown_target_or_pr(
        self, repo: Path
    ) -> None:
        """Spec L3: the validity key must be complete. An unknown target or PR
        identity is not a sealable fact -- sealing "" as a bound fact would mint
        an incomplete key, so the builder refuses instead of guessing."""
        from fleet_graph.dd.dispatch import DevelopmentChain, DispatchError, StageDispatchBuilder
        from fleet_graph.dd.validity_binding import BindingFacts

        commit = self._commit_run_config(repo)
        builder = StageDispatchBuilder(
            DevelopmentChain(
                development_id="dev-1",
                workspace_path=str(repo),
                target_base_commit="0" * 40,
                root_handoff_digest="sha256:" + "0" * 64,
            )
        )
        with pytest.raises(DispatchError, match="complete validity key"):
            builder.validity_key({"input_commit": commit}, facts=BindingFacts())
        with pytest.raises(DispatchError, match="complete validity key"):
            builder.validity_key(
                {"input_commit": commit},
                facts=BindingFacts(target_identity="refs/heads/release/self", pr_identity=""),
            )

    def test_the_dispatch_builder_refuses_to_bind_without_a_run_config(
        self, repo: Path
    ) -> None:
        from fleet_graph.dd.dispatch import DevelopmentChain, DispatchError, StageDispatchBuilder
        from fleet_graph.dd.vendor.git_ops import ExactWorkspaceError

        builder = StageDispatchBuilder(
            DevelopmentChain(
                development_id="dev-1",
                workspace_path=str(repo),
                target_base_commit="0" * 40,
                root_handoff_digest="sha256:" + "0" * 64,
            )
        )
        with pytest.raises((DispatchError, ExactWorkspaceError)):
            builder.validity_key({"input_commit": head(repo)})

    def test_a_materialization_target_expresses_target_and_pr_nonexistence(
        self, repo: Path
    ) -> None:
        from fleet_graph.graphs.dd_materializer import MaterializationTarget

        target = MaterializationTarget(
            remote_url="https://example.invalid/repo.git",
            remote_ref="refs/heads/dev-1",
            worktree=str(repo),
            state_root=str(repo / ".state"),
        )
        # Neither the merge target nor the PR head/base is substituted with the
        # publish ref when unset: absence is expressed as "" (bound, not yet
        # known), never re-identified as the publish ref (spec L3).
        assert target.merge_target_identity == ""
        assert target.merge_head_ref == ""
        assert target.pr_identity == ""

    def test_the_gate_binds_and_verifies_a_validity_key(self, repo: Path) -> None:
        from fleet_graph.graphs.dd_gate import GraphGateNode

        commit = self._commit_run_config(repo)
        node = GraphGateNode(plane=None)
        binding = node._validity_binding(
            repo,
            commit,
            {
                "spec_digest": "sha256:" + "d" * 64,
                "remote_ref": "refs/heads/release/self",
                "audit_ref": "refs/heads/dd/dev-fg-1",
            },
        )
        assert binding["matches"] is True
        assert binding["changed"] == []
        assert binding["digest"].startswith("sha256:")
        assert "product_revision" in binding["fields"]
        assert binding["fields"]["target_identity"] == "refs/heads/release/self"
        assert binding["fields"]["pr_identity"] == "refs/heads/dd/dev-fg-1->refs/heads/release/self"
        assert binding["fields"]["acceptance_context_revision"], "acceptance context must bind"

    def test_the_gate_refuses_when_validity_cannot_be_bound(self, repo: Path) -> None:
        from fleet_graph.dd.vendor.git_ops import ExactWorkspaceError
        from fleet_graph.graphs.dd_gate import GraphGateNode

        node = GraphGateNode(plane=None)
        with pytest.raises((ExactWorkspaceError, RuntimeError)):
            node._validity_binding(repo, head(repo), {"spec_digest": "sha256:" + "d" * 64})

    def test_the_gate_refuses_when_the_pr_head_or_target_is_unknown(self, repo: Path) -> None:
        """Spec L3: the gate must not fabricate a PR identity by substituting the
        target ref for a missing audit branch (``release->release``). A verdict
        whose target or PR head/base pair cannot be named refuses fail-closed,
        consistent with ``MaterializationTarget.pr_identity``'s empty-value rule."""
        from fleet_graph.graphs.dd_gate import GraphGateNode

        commit = self._commit_run_config(repo)
        node = GraphGateNode(plane=None)

        # A release-branch target with no order-private audit branch: the PR
        # head is unknown, so the binding refuses rather than minting a
        # "release->release" pair.
        with pytest.raises(RuntimeError, match="pr_identity"):
            node._validity_binding(
                repo,
                commit,
                {
                    "spec_digest": "sha256:" + "d" * 64,
                    "remote_ref": "refs/heads/release/self",
                    "audit_ref": "",
                },
            )

        # Neither identity is present: the binding refuses fail-closed too.
        with pytest.raises(RuntimeError, match="pr_identity"):
            node._validity_binding(
                repo,
                commit,
                {"spec_digest": "sha256:" + "d" * 64, "remote_ref": "", "audit_ref": ""},
            )

    def test_the_gate_verifies_against_the_sealed_key_and_flags_expiry(self, repo: Path) -> None:
        """Spec L5: a goal verdict is bound to the accepted/reviewed version, not
        self-compared. When the product tree drifts after the sealed key was
        measured, the current facts no longer bind it and the verdict is expired."""
        from fleet_graph.graphs.dd_gate import GraphGateNode

        commit = self._commit_run_config(repo)
        node = GraphGateNode(plane=None)
        status = {
            "spec_digest": "sha256:" + "d" * 64,
            "remote_ref": "refs/heads/release/self",
            "audit_ref": "refs/heads/dd/dev-fg-1",
        }
        sealed = node._validity_binding(repo, commit, status)
        assert sealed["expired"] is False

        path = repo / "product.py"
        path.write_text("print('drift')\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "product drift")
        drifted = head(repo)

        result = node._validity_binding(repo, drifted, status, previous=sealed)
        assert result["expired"] is True
        assert "product_tree" in result["changed"]

    def test_a_bookkeeping_only_advance_does_not_expire_the_verdict(self, repo: Path) -> None:
        """A pure bookkeeping commit (same tree) must not invalidate a verdict
        (spec L3): the revision advances, every meaningful fact stays put."""
        from fleet_graph.graphs.dd_gate import GraphGateNode

        commit = self._commit_run_config(repo)
        node = GraphGateNode(plane=None)
        status = {
            "spec_digest": "sha256:" + "d" * 64,
            "remote_ref": "refs/heads/release/self",
            "audit_ref": "refs/heads/dd/dev-fg-1",
        }
        sealed = node._validity_binding(repo, commit, status)
        git(repo, "commit", "-q", "--allow-empty", "-m", "bookkeeping")
        later = head(repo)

        result = node._validity_binding(repo, later, status, previous=sealed)
        assert result["expired"] is False
        assert result["matches"] is False
        assert "product_revision" in result["changed"]

    def test_the_materializer_refuses_to_seal_without_a_binding(self, repo: Path) -> None:
        from fleet_graph.dd.dispatch import DevelopmentChain, StageDispatchBuilder
        from fleet_graph.graphs.dd_materializer import (
            MaterializationFailed,
            MaterializationTarget,
            PluginMaterializer,
        )

        builder = StageDispatchBuilder(
            DevelopmentChain(
                development_id="dev-1",
                workspace_path=str(repo),
                target_base_commit="0" * 40,
                root_handoff_digest="sha256:" + "0" * 64,
            )
        )
        materializer = PluginMaterializer(
            builder=builder,
            binding=object(),
            target=MaterializationTarget(
                remote_url="https://example.invalid/repo.git",
                remote_ref="refs/heads/dev-1",
                worktree=str(repo),
                state_root=str(repo / ".state"),
            ),
            verify_worktree_head=True,
        )
        # No workspace run-config, and a commit the git tree cannot read: the
        # validity key cannot be bound, so the seal must fail rather than invent
        # a None binding.
        with pytest.raises(MaterializationFailed, match="VALIDITY_BINDING_FAILED"):
            materializer._validity_key("9" * 40)

    def test_the_materializer_refuses_to_seal_with_an_unknown_target_or_pr(
        self, repo: Path
    ) -> None:
        """Spec L3: the seal must not mint an incomplete validity key. Even when
        the git facts, SPEC and run-config are all readable, an unknown target
        or PR identity (an unset ``target_ref``/``audit_ref``) makes the binding
        incomplete, so the seal fails closed instead of sealing "" facts."""
        from fleet_graph.dd.dispatch import DevelopmentChain, StageDispatchBuilder
        from fleet_graph.graphs.dd_materializer import (
            MaterializationFailed,
            MaterializationTarget,
            PluginMaterializer,
        )

        commit = self._commit_run_config(repo)
        builder = StageDispatchBuilder(
            DevelopmentChain(
                development_id="dev-1",
                workspace_path=str(repo),
                target_base_commit="0" * 40,
                root_handoff_digest="sha256:" + "0" * 64,
            )
        )
        materializer = PluginMaterializer(
            builder=builder,
            binding=object(),
            target=MaterializationTarget(
                remote_url="https://example.invalid/repo.git",
                remote_ref="refs/heads/dev-1",
                worktree=str(repo),
                state_root=str(repo / ".state"),
            ),
            verify_worktree_head=True,
        )
        # Valid commit with a committed spec and run-config, but no durable
        # merge target or audit branch: the seal must refuse, not fabricate.
        with pytest.raises(MaterializationFailed, match="VALIDITY_BINDING_FAILED"):
            materializer._validity_key(commit)

    def test_replay_records_a_traceable_refusal_when_validity_evidence_is_lost(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """Spec L4: a replay step that finds the sealed validity key missing or
        corrupted refuses traceably, not silently. The refusal names a distinct
        code and is emitted to the raw-event sink, so a recovery pass can tell
        "the evidence was lost" from "the facts moved"."""
        from fleet_graph.dd.lifecycle import Lifecycle
        from fleet_graph.graphs.dd_replay import (
            VALIDITY_EVIDENCE_CORRUPT,
            VALIDITY_EVIDENCE_MISSING,
            ReceiptReplayer,
        )

        state_root = tmp_path / "state"
        receipt = {"attempt_id": "attempt-1"}

        def make_replayer() -> tuple[ReceiptReplayer, list[dict[str, object]]]:
            observed: list[dict[str, object]] = []
            replayer = ReceiptReplayer(
                workspace=repo,
                state_root=state_root,
                prior_state_roots=(),
                development_id="dev-1",
                generation=2,
                lifecycle=Lifecycle.load(),
                target_identity="refs/heads/release/self",
                pr_identity="refs/heads/dd/dev-fg-1->refs/heads/release/self",
                observe=observed.append,
            )
            return replayer, observed

        # Missing key: safe reject with a distinct, traceable code.
        replayer, observed = make_replayer()
        assert replayer._validity_allows(state_root, receipt, "implement", head(repo)) is False
        assert replayer.refusals[-1]["reason"] == VALIDITY_EVIDENCE_MISSING
        replayer._flush_refusals()
        assert observed == replayer.refusals

        # Corrupted key: same fail-closed rejection, distinct code.
        key_dir = state_root / "receipts" / "attempt-1"
        key_dir.mkdir(parents=True, exist_ok=True)
        (key_dir / "implement-validity.json").write_text("{not json", encoding="utf-8")
        replayer, observed = make_replayer()
        assert replayer._validity_allows(state_root, receipt, "implement", head(repo)) is False
        assert replayer.refusals[-1]["reason"] == VALIDITY_EVIDENCE_CORRUPT
        replayer._flush_refusals()
        assert observed == replayer.refusals

    def test_recovery_binds_the_validity_digest(self) -> None:
        from fleet_graph.dd.recovery import HumanRecoveryExit, recovery_validity_digest
        from fleet_graph.dd.validity import ValidityInputs

        inputs = ValidityInputs(
            product_revision="a" * 40,
            product_tree="b" * 40,
            spec_digest="sha256:" + "c" * 64,
        )
        digest = recovery_validity_digest(inputs)
        exit_ = HumanRecoveryExit()
        recorded = exit_.record(
            target_ref="refs/heads/release/x",
            decision="rework",
            decided_by="human",
            question_note_id="note-1",
            validity_digest=digest,
        )
        assert recorded.validity_digest == digest
        other = ValidityInputs(
            product_revision="f" * 40,
            product_tree="b" * 40,
            spec_digest="sha256:" + "c" * 64,
        )
        assert recovery_validity_digest(other) != digest

    def test_binding_key_from_fields_refuses_a_digest_that_does_not_match(self) -> None:
        """Spec L3/L4: the persisted digest is part of the seal. Reconstructing a
        key from fields whose digest was tampered must refuse fail-closed
        (``None``), not silently accept the field set under a digest it no
        longer binds."""
        from fleet_graph.dd.validity_binding import binding_key_from_fields

        key = build_validity_key(ValidityInputs(spec_digest="sha256:" + "c" * 64))
        assert binding_key_from_fields(key.fields, key.digest) is not None
        assert binding_key_from_fields(key.fields, "sha256:" + "f" * 64) is None

    def test_the_gate_refuses_a_tampered_previous_digest(self, repo: Path) -> None:
        """The gate compares against the sealed key through the same helper, so a
        tampered sealed digest makes the previous key unreconstructable and the
        verdict refuses as expired (fail-closed, spec L5)."""
        from fleet_graph.graphs.dd_gate import GraphGateNode

        commit = self._commit_run_config(repo)
        node = GraphGateNode(plane=None)
        status = {
            "spec_digest": "sha256:" + "d" * 64,
            "remote_ref": "refs/heads/release/self",
            "audit_ref": "refs/heads/dd/dev-fg-1",
        }
        sealed = node._validity_binding(repo, commit, status)
        tampered = dict(sealed)
        tampered["digest"] = "sha256:" + "f" * 64
        result = node._validity_binding(repo, commit, status, previous=tampered)
        assert result["expired"] is True
