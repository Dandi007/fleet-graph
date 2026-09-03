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

import asyncio
import json
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

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
    parse_self_gate_evidence,
)
from fleet_graph.decision_mcp import (
    OUTCOME_DELIVERED,
    OUTCOME_REFUSED,
    build_decision_mcp_server,
    deliver_decision,
    deliver_self_gate_decision,
)
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineBounds, LineGuards

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


def evidence_dict(**overrides: Any) -> dict[str, Any]:
    """The six-duty evidence as the delivery surface receives it: a JSON object."""
    fields: dict[str, Any] = {
        "principal": LINE,
        "dispatched_by": LINE,
        "decision": APPROVE,
        "target_base_commit": FROZEN_BASE,
        "acceptance_triple": {
            "spec_argv": [list(a) for a in SPEC_ARGV],
            "record_argv": [list(a) for a in SPEC_ARGV],
            "receipt_argv": [list(a) for a in SPEC_ARGV],
        },
        "diff_boundary": {
            "changed_product_paths": ["src/fleet_graph/dd/selfgate.py"],
            "spec_declared_paths": ["src/fleet_graph/dd/"],
        },
        "zero_test_deletion": [],
        "self_run": {"argv": list(SPEC_ARGV[0]), "exit_code": 0, "tail": "9 passed"},
        "mutation_gun": {
            "shots": [
                {"index": 0, "mutator": "mutate #0", "acceptance_exit_code": 1},
                {"index": 1, "mutator": "mutate #1", "acceptance_exit_code": 1},
            ],
            "restored_sha": "sha256:restored",
            "expected_sha": "sha256:restored",
            "restored_mode_ok": True,
        },
        "regression_baseline": {
            "target_base_commit": FROZEN_BASE,
            "passed": 2614,
            "failed": 0,
            "skipped": 0,
            "failed_tests": [],
        },
        "current_failed_tests": [],
        "flaky_tests": [],
        "flaky_attribution": [],
    }
    fields.update(overrides)
    return fields


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


# --- the engine default path: the self-gate is wired into the dd delivery -----
#
# The M3 blocker the reviewer named was that ``decide`` / ``SelfGateEvidence``
# lived only in the judging library and were never consulted by the delivery
# path. ``deliver_self_gate_decision`` is that wiring: the line's self-judged dd
# delivery runs the six-duty gate first (``decide``), and only an admitted
# verdict reaches the M2 dd gate delivery (``_deliver_dd``). A missing duty, a
# non-dispatching principal, or a regression refusal stops before the single is
# touched.


DD_ID = "dev-fg-abc"


class FakeDdPlane:
    """A duck-typed dd control plane (``get`` + ``gate``), like M2's tests use."""

    def __init__(self, *, state: str = "awaiting_gate", dispatched_by: str = LINE) -> None:
        self.state = state
        self.dispatched_by = dispatched_by
        self.queried = False
        self.resumed: list[tuple[str, str]] = []

    def get(self, development_id: str) -> dict[str, Any]:
        self.queried = True
        return {
            "development_id": development_id,
            "state": self.state,
            "dispatched_by": self.dispatched_by,
            "generation": 2,
            "awaiting": {"question_note_id": "q-dd-1", "card_entity_id": "card-dd-1"},
        }

    def gate(self, development_id: str, resume: bool = False, action_key: str | None = None) -> Any:
        self.resumed.append((development_id, action_key or ""))
        return {
            "development_id": development_id,
            "resume": {"development_id": development_id, "generation": 2},
        }


def _stall(run_root: Path) -> Path:
    path = run_root / ".scheduler" / f"{LINE}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generation": 2,
                "park_considered_run_id": "run-1",
                "parked_run_id": "run-1",
                "parked_at": 1_700_000_000.0,
            }
        ),
        encoding="utf-8",
    )
    return path


class TestSelfGateDeliveryRefused:
    def test_a_missing_duty_refuses_the_delivery_before_the_single_is_touched(
        self, tmp_path: Path
    ) -> None:
        plane = FakeDdPlane()
        result = deliver_self_gate_decision(
            complete_evidence(self_run=None),
            development_id=DD_ID,
            run_root=tmp_path,
            dd=plane,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_SELFGATE_INCOMPLETE
        assert DUTY_SELF_RUN_ACCEPTANCE in result.message
        # Refused at the gate: the dd control plane was never even queried.
        assert plane.queried is False
        assert plane.resumed == []

    def test_a_regression_refusal_names_the_green_flip(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = deliver_self_gate_decision(
            complete_evidence(current_failed_tests=frozenset({"tests/test_x.py::test_flip"})),
            development_id=DD_ID,
            run_root=tmp_path,
            dd=plane,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_SELFGATE_REGRESSION
        assert plane.queried is False
        assert plane.resumed == []

    def test_a_non_dispatching_principal_is_refused_and_never_touches_the_single(
        self, tmp_path: Path
    ) -> None:
        plane = FakeDdPlane()
        result = deliver_self_gate_decision(
            complete_evidence(principal="wf-other"),
            development_id=DD_ID,
            run_root=tmp_path,
            dd=plane,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_SELFGATE_INCOMPLETE
        assert plane.queried is False
        assert plane.resumed == []


class TestSelfGatePositiveDelivery:
    def test_a_complete_evidence_approve_resumes_and_wakes_the_line(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        _stall(tmp_path)
        result = deliver_self_gate_decision(
            complete_evidence(),
            development_id=DD_ID,
            run_root=tmp_path,
            dd=plane,
            clock=lambda: 1_700_000_123.0,
        )
        assert result.status == OUTCOME_DELIVERED
        assert result.as_dict()["outcome"] == "consumed"
        assert plane.resumed == [(DD_ID, f"mcp:dd:{DD_ID}:g2:APPROVE")]
        # Item 4: the six-duty rationale is the delivery's evidence payload.
        assert result.target is not None
        assert result.target["self_gate"]["decided_by"] == LINE
        assert result.target["self_gate"]["dispatched_by"] == LINE
        assert DUTY_REGRESSION_BASELINE in result.target["self_gate"]
        # The dispatching line is woken: the M2 wake fact landed.
        after = json.loads((tmp_path / ".scheduler" / f"{LINE}.json").read_text(encoding="utf-8"))
        assert after["dispatched_decision_consumed_at"] == 1_700_000_123.0

    def test_a_complete_reject_verdict_is_also_a_delivered_decision(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = deliver_self_gate_decision(
            complete_evidence(decision=REJECT),
            development_id=DD_ID,
            run_root=tmp_path,
            dd=plane,
        )
        assert result.status == OUTCOME_DELIVERED
        assert result.decision == REJECT
        assert plane.resumed == [(DD_ID, f"mcp:dd:{DD_ID}:g2:REJECT")]


class TestSelfGateApprovedThenMergeHarvest:
    def test_the_positive_path_harvests_only_after_merge(self) -> None:
        """自判 APPROVE → merge 段完成 → 收割触发（S7 阳性链）。"""

        result = decide(complete_evidence())
        assert result.admitted
        # At the gate (before merge) the harvest must not fire.
        assert harvest_trigger(APPROVE, merged=False) is False
        # After the merge segment the harvest fires.
        assert harvest_trigger(APPROVE, merged=True) is True


# --- the engine surface wiring (M3 blocker): decision_deliver carries the
# six-duty evidence and runs the self-gate; the goal line has a delivery port ---


class TestParseSelfGateEvidence:
    def test_a_complete_json_object_parses_and_admits(self) -> None:
        evidence = parse_self_gate_evidence(evidence_dict())
        assert evidence is not None
        assert decide(evidence).outcome == "approve"

    def test_a_missing_duty_is_parsed_as_missing_not_silently_green(self) -> None:
        evidence = parse_self_gate_evidence(evidence_dict(self_run=None))
        assert evidence is not None
        assert DUTY_SELF_RUN_ACCEPTANCE in evidence.missing_duties()

    def test_a_malformed_payload_is_none(self) -> None:
        assert parse_self_gate_evidence({}) is None
        assert parse_self_gate_evidence("not an object") is None  # type: ignore[arg-type]


class TestEvidenceWiredIntoDeliverySurface:
    def test_a_missing_duty_refuses_delivery_before_the_single_is_touched(
        self, tmp_path: Path
    ) -> None:
        plane = FakeDdPlane()
        result = deliver_decision(
            line=DD_ID,
            decision=APPROVE,
            reason="line self-judged",
            run_root=tmp_path,
            lines=[],
            dd=plane,
            evidence=evidence_dict(self_run=None),
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_SELFGATE_INCOMPLETE
        assert DUTY_SELF_RUN_ACCEPTANCE in result.message
        assert plane.queried is False
        assert plane.resumed == []

    def test_complete_evidence_delivers_and_wakes_the_line(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        _stall(tmp_path)
        result = deliver_decision(
            line=DD_ID,
            decision=APPROVE,
            reason="line self-judged",
            run_root=tmp_path,
            lines=[],
            dd=plane,
            evidence=evidence_dict(),
            clock=lambda: 1_700_000_123.0,
        )
        assert result.status == OUTCOME_DELIVERED
        assert result.as_dict()["outcome"] == "consumed"
        assert result.target is not None
        assert result.target["self_gate"]["decided_by"] == LINE
        assert result.target["self_gate"]["dispatched_by"] == LINE
        assert plane.resumed == [(DD_ID, f"mcp:dd:{DD_ID}:g2:APPROVE")]

    def test_unparseable_evidence_refuses_without_touching_the_single(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = deliver_decision(
            line=DD_ID,
            decision=APPROVE,
            reason="line self-judged",
            run_root=tmp_path,
            lines=[],
            dd=plane,
            evidence={"principal": LINE},
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_SELFGATE_INCOMPLETE
        assert plane.queried is False


class TestDecisionDeliverToolEvidenceSurface:
    def test_the_tool_registers_an_evidence_argument(self) -> None:
        server = build_decision_mcp_server(Path("/tmp"), [])
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        schema = tools["decision_deliver"].parameters
        assert "evidence" in schema["properties"]


class _SelfGateFake:
    def __init__(self, outcome: dict[str, Any] | None = None) -> None:
        self.outcome = outcome or {"status": "delivered", "code": "", "line": DD_ID}
        self.calls: list[tuple[Any, str]] = []

    def deliver(self, evidence: Any, development_id: str) -> dict[str, Any]:
        self.calls.append((evidence, development_id))
        return self.outcome


class _OrchCoordinator:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def turn(self, round_no: int, coord_input: dict[str, Any], *, resume: bool = False) -> Any:
        self.inputs.append(coord_input)
        return {"verdict": "done", "reason": "self-judged delivered"}


class _OrchWorker:
    def turn(self, prompt: str, round_no: int) -> Any:
        raise AssertionError("self-gate delivery must not reach the worker")


class _OrchInbox:
    def drain_then_ack(self, persist: Any) -> tuple[Any, list[str]]:
        persist([])
        return [], []


class _OrchArtifacts:
    def __init__(self) -> None:
        self.terminal: dict[str, Any] | None = None

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        return True

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> Any:
        return "worker-report.json"

    def write_terminal(self, **kwargs: Any) -> Any:
        self.terminal = kwargs
        return "terminal.json"


class TestGoalLineSelfGateOrchestration:
    def test_pending_evidence_is_delivered_on_wake_and_recorded(self) -> None:
        """The goal line delivers its six-duty evidence through the self-gate port
        before the coordinator turn (the M3 default path, wired at the graph)."""
        self_gate = _SelfGateFake()
        coordinator = _OrchCoordinator()
        artifacts = _OrchArtifacts()
        deps = LineDeps(
            coordinator=coordinator,
            worker=_OrchWorker(),
            inbox=_OrchInbox(),
            artifacts=artifacts,
            guards=LineGuards(bounds=LineBounds()),
            folder_id=LINE,
            self_gate=self_gate,
        )
        graph = build_goal_line_graph(deps).compile(checkpointer=InMemorySaver())
        state = graph.invoke(
            {"round_no": 1, "self_gate_evidence": evidence_dict(), "dd_development_id": DD_ID},
            config={"configurable": {"thread_id": "wf-1:g1"}, "recursion_limit": 100},
        )
        assert self_gate.calls == [(evidence_dict(), DD_ID)]
        assert state["self_gate_delivery"]["status"] == "delivered"
        # Evidence cleared: the delivery fires exactly once, not every round.
        assert state["self_gate_evidence"] is None
        # The coordinator weighed the delivery outcome as a mechanical fact.
        assert coordinator.inputs and "self_gate_delivery" in coordinator.inputs[0]
        assert artifacts.terminal is not None
        assert artifacts.terminal["terminal"] == "done"

    def test_an_unwired_graph_runs_the_line_unchanged(self) -> None:
        """A line without a self-gate port keeps the exact prior routing."""
        artifacts = _OrchArtifacts()
        deps = LineDeps(
            coordinator=_OrchCoordinator(),
            worker=_OrchWorker(),
            inbox=_OrchInbox(),
            artifacts=artifacts,
            guards=LineGuards(bounds=LineBounds()),
            folder_id=LINE,
            self_gate=None,
        )
        graph = build_goal_line_graph(deps).compile(checkpointer=InMemorySaver())
        state = graph.invoke(
            {"round_no": 1, "self_gate_evidence": evidence_dict(), "dd_development_id": DD_ID},
            config={"configurable": {"thread_id": "wf-1:g2"}, "recursion_limit": 100},
        )
        assert artifacts.terminal is not None
        assert artifacts.terminal["terminal"] == "done"
        assert "self_gate_delivery" not in state
