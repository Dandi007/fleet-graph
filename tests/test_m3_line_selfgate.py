"""M3 line self-gate: the six evidence duties, one test group each, plus the gate.

The spec (wf-8d9737 M3) turns D5 ("DD 闸归派单线") into six mechanical evidence
duties the dispatching line must fulfil before a self-judged ``decision_deliver``
is admitted. This file pins each duty's positive contract, the item-6 regression
negative quadruple (missing baseline fields / green→red flip / red-set growth /
drifted-main baseline), the flake-attribution escape hatch, the missing-duty
delivery refusal, the principal == dispatched_by authority check, and the
S7 merge-then-harvest ordering.

Everything runs against ``fleet_graph.dd.selfgate`` -- no Scheduler, no live dd
control plane, no worktree. The module under test never runs a subprocess; it
only judges the facts handed to it, which is exactly what makes each duty's
mutation a named, directly-red-able guard (a body collapsed to ``return None``
or a branch flipped turns a specific test red).
"""

from __future__ import annotations

from typing import Any

from fleet_graph.dd.selfgate import (
    APPROVE,
    CODE_SELFGATE_BASELINE_UNANCHORED,
    CODE_SELFGATE_INCOMPLETE,
    CODE_SELFGATE_REGRESSION,
    DUTY_ACCEPTANCE_TRIPLE_EQUAL,
    DUTY_DIFF_WITHIN_SPEC_BOUNDS,
    DUTY_MUTATION_GUN,
    DUTY_REGRESSION_BASELINE,
    DUTY_SELF_RUN_ACCEPTANCE,
    DUTY_ZERO_TEST_DELETION,
    EVIDENCE_DUTIES,
    MUTATION_SHOTS_REQUIRED,
    REJECT,
    AcceptanceTriple,
    DiffBoundary,
    MutationGun,
    MutationShot,
    RegressionBaseline,
    SelfGateEvidence,
    SelfRun,
    compare_regression,
    decide,
    harvest_trigger,
    merge_then_harvest,
)

LINE = "wf-1"
FROZEN_BASE = "d9c04295a3cddce863d4118d7b0ea58f8e2bacfe"

SPEC_ARGV = (("uv", "run", "pytest", "-q", "tests/test_m3_line_selfgate.py"),)


def _triple(equal: bool = True) -> AcceptanceTriple:
    receipt = SPEC_ARGV if equal else (("uv", "run", "pytest", "-q", "tests/other.py"),)
    return AcceptanceTriple(spec_argv=SPEC_ARGV, record_argv=SPEC_ARGV, receipt_argv=receipt)


def _boundary(clean: bool = True) -> DiffBoundary:
    changed = ("src/fleet_graph/dd/selfgate.py",)
    declared = ("src/fleet_graph/dd/",) if clean else ("src/fleet_graph/other/",)
    return DiffBoundary(changed_product_paths=changed, spec_declared_paths=declared)


def _gun(clean: bool = True) -> MutationGun:
    shots = tuple(
        MutationShot(
            index=i,
            mutator=f"mutate #{i}",
            acceptance_exit_code=1 if clean else 0,
        )
        for i in range(MUTATION_SHOTS_REQUIRED)
    )
    return MutationGun(
        shots=shots,
        restored_sha="sha256:restored",
        expected_sha="sha256:restored",
        restored_mode_ok=True,
    )


def complete_evidence(**overrides: Any) -> SelfGateEvidence:
    """A fully-complete evidence set; knock one duty out by overriding its field."""
    baseline = RegressionBaseline(
        target_base_commit=FROZEN_BASE,
        passed=2614,
        failed=0,
        skipped=0,
        failed_tests=frozenset(),
    )
    fields: dict[str, Any] = {
        "principal": LINE,
        "dispatched_by": LINE,
        "decision": APPROVE,
        "target_base_commit": FROZEN_BASE,
        "acceptance_triple": _triple(),
        "diff_boundary": _boundary(),
        "zero_test_deletion": (),
        "self_run": SelfRun(argv=SPEC_ARGV[0], exit_code=0, tail="9 passed"),
        "mutation_gun": _gun(),
        "regression_baseline": baseline,
    }
    fields.update(overrides)
    return SelfGateEvidence(**fields)


# --- duty 1: acceptance triple equal ----------------------------------------


class TestAcceptanceTripleEqual:
    def test_all_three_argv_sets_match(self) -> None:
        assert _triple(equal=True).equal()

    def test_a_receipt_argv_deviation_breaks_the_triple(self) -> None:
        assert not _triple(equal=False).equal()

    def test_a_record_argv_deviation_breaks_the_triple(self) -> None:
        triple = AcceptanceTriple(
            spec_argv=SPEC_ARGV,
            record_argv=(("uv", "run", "pytest", "-q", "tests/drifted.py"),),
            receipt_argv=SPEC_ARGV,
        )
        assert not triple.equal()


# --- duty 2: product diff within the spec surface ---------------------------


class TestDiffWithinSpecBounds:
    def test_a_changed_product_file_inside_the_spec_surface_is_bounded(self) -> None:
        assert _boundary(clean=True).out_of_bounds() == ()

    def test_a_changed_file_outside_the_declared_surface_is_out_of_bounds(self) -> None:
        assert _boundary(clean=False).out_of_bounds() == ("src/fleet_graph/dd/selfgate.py",)

    def test_machine_artifacts_never_count_as_out_of_bounds(self) -> None:
        boundary = DiffBoundary(
            changed_product_paths=(
                ".dev-dispatch/spec/approved.md",
                ".dd-evidence/evidence.json",
                "src/fleet_graph/dd/selfgate.py",
            ),
            spec_declared_paths=("src/fleet_graph/dd/",),
        )
        assert boundary.out_of_bounds() == ()


# --- duty 3: zero test deletion ---------------------------------------------


class TestZeroTestDeletion:
    def test_no_deleted_tests_is_a_clean_diff_filter(self) -> None:
        assert complete_evidence().missing_duties() == ()

    def test_a_deleted_test_file_is_missing_the_deletion_duty(self) -> None:
        evidence = complete_evidence(zero_test_deletion=("tests/test_old.py",))
        assert DUTY_ZERO_TEST_DELETION in evidence.missing_duties()


# --- duty 4: self-run acceptance --------------------------------------------


class TestSelfRunAcceptance:
    def test_a_self_run_with_argv_is_present(self) -> None:
        assert complete_evidence().missing_duties() == ()

    def test_an_absent_self_run_is_missing_the_duty(self) -> None:
        assert DUTY_SELF_RUN_ACCEPTANCE in complete_evidence(self_run=None).missing_duties()

    def test_an_empty_argv_self_run_is_vacuous_missing(self) -> None:
        evidence = complete_evidence(self_run=SelfRun(argv=(), exit_code=0))
        assert DUTY_SELF_RUN_ACCEPTANCE in evidence.missing_duties()


# --- duty 5: mutation gun (two shots, both red, byte-restore) ----------------


class TestMutationGun:
    def test_two_red_shots_restored_intact_pass(self) -> None:
        assert complete_evidence().missing_duties() == ()

    def test_a_single_shot_is_not_a_mutation_gun(self) -> None:
        evidence = complete_evidence(
            mutation_gun=MutationGun(
                shots=(MutationShot(index=0, mutator="x", acceptance_exit_code=1),),
                restored_sha="s",
                expected_sha="s",
            )
        )
        assert DUTY_MUTATION_GUN in evidence.missing_duties()

    def test_a_green_shot_is_not_an_attack(self) -> None:
        shot = MutationShot(index=0, mutator="x", acceptance_exit_code=0)
        assert not shot.turned_acceptance_red

    def test_a_gun_whose_restore_mismatched_sha_is_missing(self) -> None:
        evidence = complete_evidence(
            mutation_gun=MutationGun(
                shots=_gun().shots,
                restored_sha="sha256:drifted",
                expected_sha="sha256:restored",
            )
        )
        assert DUTY_MUTATION_GUN in evidence.missing_duties()


# --- duty 6: regression vs baseline (S9) ------------------------------------


class TestRegressionBaselineParsing:
    def test_a_missing_baseline_field_is_not_a_baseline(self) -> None:
        assert RegressionBaseline.from_dict({}) is None
        assert RegressionBaseline.from_dict({"target_base_commit": FROZEN_BASE}) is None

    def test_a_missing_failed_test_set_is_not_a_baseline(self) -> None:
        raw = {
            "target_base_commit": FROZEN_BASE,
            "passed": 10,
            "failed": 0,
            "skipped": 0,
        }
        assert RegressionBaseline.from_dict(raw) is None  # type: ignore[arg-type]

    def test_a_full_snapshot_round_trips(self) -> None:
        raw: dict[str, Any] = {
            "target_base_commit": FROZEN_BASE,
            "passed": 10,
            "failed": 2,
            "skipped": 1,
            "failed_tests": ["t1", "t2"],
        }
        baseline = RegressionBaseline.from_dict(raw)
        assert baseline is not None
        assert baseline.failed_tests == frozenset({"t1", "t2"})


class TestRegressionJudgement:
    def test_missing_baseline_is_a_refusal(self) -> None:
        verdict = compare_regression(None, frozenset())
        assert not verdict.acceptable

    def test_a_green_flip_is_a_refusal(self) -> None:
        baseline = RegressionBaseline(FROZEN_BASE, 100, 0, 0, frozenset())
        verdict = compare_regression(baseline, frozenset({"t_flip"}))
        assert not verdict.acceptable
        assert verdict.new_red == frozenset({"t_flip"})

    def test_a_red_baseline_with_an_unchanged_red_set_passes(self) -> None:
        baseline = RegressionBaseline(FROZEN_BASE, 99, 1, 0, frozenset({"t_red"}))
        verdict = compare_regression(baseline, frozenset({"t_red"}))
        assert verdict.acceptable

    def test_a_new_red_atop_an_already_red_baseline_grows_the_red_set(self) -> None:
        baseline = RegressionBaseline(FROZEN_BASE, 99, 1, 0, frozenset({"t_red"}))
        verdict = compare_regression(baseline, frozenset({"t_red", "t_new"}))
        assert not verdict.acceptable
        assert verdict.new_red == frozenset({"t_new"})

    def test_a_flaky_only_increment_is_attributed_and_released(self) -> None:
        baseline = RegressionBaseline(FROZEN_BASE, 99, 1, 0, frozenset({"t_red"}))
        verdict = compare_regression(
            baseline,
            frozenset({"t_red", "t_flaky"}),
            flaky_tests=frozenset({"t_flaky"}),
            flaky_attribution=frozenset({"t_flaky"}),
        )
        assert verdict.acceptable
        assert verdict.flaky_attributed == frozenset({"t_flaky"})

    def test_a_flaky_increment_without_attribution_is_still_a_refusal(self) -> None:
        baseline = RegressionBaseline(FROZEN_BASE, 100, 0, 0, frozenset())
        verdict = compare_regression(
            baseline, frozenset({"t_flaky"}), flaky_tests=frozenset({"t_flaky"})
        )
        assert not verdict.acceptable


# --- the gate: missing duty refusal, authority, baseline anchor --------------


class TestMissingDutyDeliveryRefused:
    def test_complete_evidence_decides_approve(self) -> None:
        result = decide(complete_evidence())
        assert result.admitted
        assert result.outcome == "approve"
        assert result.code == ""

    def test_a_reject_verdict_with_complete_evidence_is_admitted(self) -> None:
        result = decide(complete_evidence(decision=REJECT))
        assert result.outcome == "reject"

    def test_each_duty_removed_refuses_the_delivery(self) -> None:
        cases = [
            (DUTY_ACCEPTANCE_TRIPLE_EQUAL, complete_evidence(acceptance_triple=None)),
            (DUTY_DIFF_WITHIN_SPEC_BOUNDS, complete_evidence(diff_boundary=None)),
            (DUTY_ZERO_TEST_DELETION, complete_evidence(zero_test_deletion=("tests/x.py",))),
            (DUTY_SELF_RUN_ACCEPTANCE, complete_evidence(self_run=None)),
            (DUTY_MUTATION_GUN, complete_evidence(mutation_gun=None)),
            (DUTY_REGRESSION_BASELINE, complete_evidence(regression_baseline=None)),
        ]
        for name, evidence in cases:
            result = decide(evidence)
            assert result.outcome == "refused", name
            assert result.code == CODE_SELFGATE_INCOMPLETE, name
            assert name in result.reason, (name, result.reason)

    def test_a_drifted_main_baseline_refuses_even_with_every_other_duty_done(self) -> None:
        drifted = RegressionBaseline(
            target_base_commit="0" * 40, passed=2614, failed=0, skipped=0, failed_tests=frozenset()
        )
        result = decide(complete_evidence(regression_baseline=drifted))
        assert result.outcome == "refused"
        assert result.code == CODE_SELFGATE_BASELINE_UNANCHORED

    def test_a_regression_refusal_names_the_green_flip(self) -> None:
        baseline = RegressionBaseline(FROZEN_BASE, 2614, 0, 0, frozenset())
        result = decide(
            complete_evidence(
                regression_baseline=baseline,
                current_failed_tests=frozenset({"tests/test_x.py::test_y"}),
            )
        )
        assert result.outcome == "refused"
        assert result.code == CODE_SELFGATE_REGRESSION

    def test_flake_attribution_is_lodged_in_the_payload(self) -> None:
        baseline = RegressionBaseline(FROZEN_BASE, 2614, 0, 0, frozenset())
        result = decide(
            complete_evidence(
                regression_baseline=baseline,
                current_failed_tests=frozenset({"tests/test_x.py::test_flaky"}),
                flaky_tests=frozenset({"tests/test_x.py::test_flaky"}),
                flaky_attribution=frozenset({"tests/test_x.py::test_flaky"}),
            )
        )
        assert result.admitted
        regression = result.rationale[DUTY_REGRESSION_BASELINE]["verdict"]
        assert regression["flaky_attributed"] == ["tests/test_x.py::test_flaky"]


class TestPrincipalIsDispatchingLine:
    def test_a_non_dispatching_principal_is_refused(self) -> None:
        result = decide(complete_evidence(principal="wf-other"))
        assert result.outcome == "refused"
        assert result.code == CODE_SELFGATE_INCOMPLETE
        assert "wf-other" in result.reason

    def test_a_maybe_decision_is_not_a_verdict(self) -> None:
        result = decide(complete_evidence(decision="MAYBE"))
        assert result.outcome == "refused"

    def test_the_rationale_records_the_decided_by_authority(self) -> None:
        result = decide(complete_evidence())
        assert result.rationale["decided_by"] == LINE
        assert result.rationale["dispatched_by"] == LINE


# --- S7: merge-then-harvest ordering ----------------------------------------


class TestMergeThenHarvest:
    def test_an_approve_triggers_harvest_only_after_merge(self) -> None:
        assert harvest_trigger(APPROVE, merged=False) is False
        assert harvest_trigger(APPROVE, merged=True) is True

    def test_a_reject_never_harvests(self) -> None:
        assert harvest_trigger(REJECT, merged=True) is False

    def test_the_spec_named_alias_is_the_same_contract(self) -> None:
        assert merge_then_harvest(APPROVE, merged=False) is False
        assert merge_then_harvest(APPROVE, merged=True) is True


# --- the duty vocabulary is the closed six ----------------------------------


class TestClosedDutyVocabulary:
    def test_the_six_duties_are_exactly_the_vocabulary(self) -> None:
        assert EVIDENCE_DUTIES == (
            DUTY_ACCEPTANCE_TRIPLE_EQUAL,
            DUTY_DIFF_WITHIN_SPEC_BOUNDS,
            DUTY_ZERO_TEST_DELETION,
            DUTY_SELF_RUN_ACCEPTANCE,
            DUTY_MUTATION_GUN,
            DUTY_REGRESSION_BASELINE,
        )
        assert len(EVIDENCE_DUTIES) == 6
