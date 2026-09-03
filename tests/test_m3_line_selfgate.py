"""M3 line self-gate: six evidence obligations + the S10 decision-delivery contract.

wf-8d9737 M3 makes the line self-gate the fleet default. A line woken by
``dd_awaiting_gate`` must discharge six mechanical evidence obligations before it
may deliver ``APPROVE``/``REJECT`` through M2's ``decision_deliver``, and the
delivery itself must satisfy the S10 three-part contract (success = consumed, not
"a unit was started"; a refused resume leaves a trace; the workspace is validated
before any unit is launched).

This suite drives each obligation red in isolation, exercises the S9 regression
baseline's five sub-cases, and -- against a duck-typed gate plane -- the three S10
negative cases plus the positive self-APPROVE path. No git, bus, or board here:
the module consumes measured facts and returns a judgement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet_graph.dd.selfgate import (
    DECISION_APPROVE,
    DECISION_REJECT,
    FlakeAttribution,
    RegressionRun,
    acceptance_argv_verbatim,
    assess_evidence,
    decide,
    harvest_eligible,
    mutation_gun_satisfied,
    personally_ran_acceptance,
    product_diff_in_scope,
    regression_ok,
    zero_test_deletion,
)
from fleet_graph.decision_mcp import (
    CODE_DD_GATE_NOT_CONSUMED,
    CODE_DD_WORKSPACE_MISSING,
    CODE_NOT_DISPATCHING_LINE,
    OUTCOME_DELIVERED,
    OUTCOME_REFUSED,
    DeliveryResult,
    deliver_decision,
)
from fleet_graph.decision_mcp import (
    DECISION_APPROVE as MCP_APPROVE,
)

DISPATCHER = "wf-1"
DD_ID = "dev-fg-m3"
ROSTER: list[Any] = [{"folder_id": "wf-1", "seat": "s", "generation": 2}]

ACCEPT = ["bash", "-lc", "uv sync --frozen && uv run pytest -q tests/test_m3_line_selfgate.py"]


def _evidence(**overrides: Any) -> dict[str, Any]:
    """A fully positive six-obligation evidence payload, ready to be broken."""
    evidence: dict[str, Any] = {
        "acceptance_verbatim": {"ok": True},
        "product_diff_in_scope": {"ok": True},
        "zero_test_deletion": {"ok": True},
        "personally_ran_acceptance": {"ok": True},
        "mutation_gun": {"ok": True},
        "regression_baseline": {"ok": True},
    }
    evidence.update(overrides)
    return evidence


class TestSixObligations:
    def test_acceptance_verbatim_must_match_three_ways(self) -> None:
        ok = acceptance_argv_verbatim(spec=ACCEPT, record=ACCEPT, receipt=ACCEPT)
        assert ok["ok"] is True

    def test_acceptance_verbatim_any_missing_is_negative(self) -> None:
        assert acceptance_argv_verbatim(spec=[], record=ACCEPT, receipt=ACCEPT)["ok"] is False
        assert acceptance_argv_verbatim(spec=ACCEPT, record=[], receipt=ACCEPT)["ok"] is False
        assert acceptance_argv_verbatim(spec=ACCEPT, record=ACCEPT, receipt=[])["ok"] is False

    def test_acceptance_verbatim_drift_is_negative(self) -> None:
        drifted = ["bash", "-lc", "pytest tests/other.py"]
        result = acceptance_argv_verbatim(spec=ACCEPT, record=ACCEPT, receipt=drifted)
        assert result["ok"] is False
        assert "differ" in result["reason"]

    def test_product_diff_outside_scope_is_negative(self) -> None:
        result = product_diff_in_scope(
            changed_paths=["src/fleet_graph/x.py", "src/out_of_scope.py"],
            scope_paths=["src/fleet_graph/x.py"],
        )
        assert result["ok"] is False
        assert "src/out_of_scope.py" in result["reason"]

    def test_product_diff_protocol_subtrees_are_exempt(self) -> None:
        result = product_diff_in_scope(
            changed_paths=[".dev-dispatch/spec/approved.md", ".dd-evidence/acceptance.json"],
            scope_paths=[],
        )
        assert result["ok"] is True

    def test_zero_test_deletion_is_negative_on_a_deleted_test(self) -> None:
        assert zero_test_deletion(deleted_paths=[])["ok"] is True
        result = zero_test_deletion(deleted_paths=["tests/test_removed.py"])
        assert result["ok"] is False
        assert "tests/test_removed.py" in result["reason"]

    def test_personally_ran_acceptance_requires_a_real_transcript(self) -> None:
        assert personally_ran_acceptance(runs=[])["ok"] is False
        assert personally_ran_acceptance(runs=[{"argv": ACCEPT}])["ok"] is False
        assert personally_ran_acceptance(runs=[{"argv": ACCEPT, "exit_code": 0}])["ok"] is True

    def test_mutation_gun_needs_two_red_and_restored_shots(self) -> None:
        shot = {"red": True, "restored": True}
        assert mutation_gun_satisfied(mutations=[shot])["ok"] is False
        assert mutation_gun_satisfied(mutations=[shot, shot])["ok"] is True
        assert (
            mutation_gun_satisfied(mutations=[shot, {"red": True, "restored": False}])["ok"]
            is False
        )


class TestRegressionBaseline:
    RED = RegressionRun(passed=99, failed=1, failed_set=frozenset({"A:test_feature"}))
    CLEAN = RegressionRun(passed=100, failed=0, failed_set=frozenset())

    def test_missing_baseline_is_refused(self) -> None:
        result = regression_ok(
            base=None, head=self.CLEAN, base_commit="b", compared_base_commit="b"
        )
        assert result["ok"] is False
        assert "baseline missing" in result["reason"]

    def test_green_to_red_flip_is_refused(self) -> None:
        head = RegressionRun(passed=99, failed=1, failed_set=frozenset({"new:test"}))
        result = regression_ok(
            base=self.CLEAN, head=head, base_commit="b", compared_base_commit="b"
        )
        assert result["ok"] is False
        assert "new:test" in result["reason"]

    def test_baseline_red_that_does_not_expand_passes(self) -> None:
        result = regression_ok(
            base=self.RED, head=self.RED, base_commit="b", compared_base_commit="b"
        )
        assert result["ok"] is True

    def test_red_to_green_is_an_improvement_not_a_regression(self) -> None:
        result = regression_ok(
            base=self.RED, head=self.CLEAN, base_commit="b", compared_base_commit="b"
        )
        assert result["ok"] is True

    def test_flake_increment_is_released_with_attribution(self) -> None:
        head = RegressionRun(passed=99, failed=1, failed_set=frozenset({"flake:test"}))
        result = regression_ok(
            base=self.CLEAN,
            head=head,
            base_commit="b",
            compared_base_commit="b",
            flake_attribution=[
                FlakeAttribution(test_id="flake:test", red_count=1, clean_base_reruns=4)
            ],
        )
        assert result["ok"] is True
        assert "flake" in result["reason"]

    def test_drifted_main_as_baseline_is_refused(self) -> None:
        result = regression_ok(
            base=self.RED,
            head=self.RED,
            base_commit="frozen-base",
            compared_base_commit="drifted-main",
        )
        assert result["ok"] is False
        assert "frozen" in result["reason"]


class TestCompositeGate:
    def test_any_missing_obligation_is_refused(self) -> None:
        assessment = assess_evidence(_evidence(acceptance_verbatim=None))
        assert assessment.ok is False
        assert "acceptance_verbatim" in assessment.violations[0]

    def test_any_negative_obligation_is_refused(self) -> None:
        assessment = assess_evidence(_evidence(zero_test_deletion={"ok": False, "reason": "x"}))
        assert assessment.ok is False
        assert "zero_test_deletion" in assessment.violations[0]

    def test_clean_evidence_is_approved(self) -> None:
        assessment = assess_evidence(_evidence())
        assert assessment.ok is True
        assert assessment.violations == ()


class TestDecide:
    def test_wrong_principal_is_rejected(self) -> None:
        verdict, assessment = decide(_evidence(), principal="wf-other", dispatched_by=DISPATCHER)
        assert verdict == DECISION_REJECT
        assert assessment.ok is False

    def test_clean_gate_and_matching_principal_is_approved(self) -> None:
        verdict, assessment = decide(_evidence(), principal=DISPATCHER, dispatched_by=DISPATCHER)
        assert verdict == DECISION_APPROVE
        assert assessment.ok is True

    def test_broken_obligation_is_rejected_even_with_matching_principal(self) -> None:
        verdict, _ = decide(
            _evidence(regression_baseline=None),
            principal=DISPATCHER,
            dispatched_by=DISPATCHER,
        )
        assert verdict == DECISION_REJECT


class TestHarvestOrdering:
    def test_harvest_waits_for_merge_never_fires_on_approve_alone(self) -> None:
        ok, reason = harvest_eligible(gate_approved=True, merge_complete=False)
        assert ok is False
        assert "merge" in reason

    def test_harvest_fires_once_merge_completes(self) -> None:
        ok, _ = harvest_eligible(gate_approved=True, merge_complete=True)
        assert ok is True

    def test_harvest_never_fires_without_a_gate_approval(self) -> None:
        ok, _ = harvest_eligible(gate_approved=False, merge_complete=True)
        assert ok is False


class FakeGatePlane:
    """A duck-typed dd control plane exercising the S10 delivery path.

    ``workspace`` (None = absent) is surfaced on ``get``; ``post_resume_state`` is
    what ``get`` reports after ``gate`` -- "running" models a consumed decision,
    "awaiting_gate" models the measured 889ms ``75/TEMPFAIL`` death.
    """

    def __init__(
        self,
        *,
        state: str = "awaiting_gate",
        dispatched_by: str = DISPATCHER,
        generation: int = 2,
        workspace: str | None = None,
        post_resume_state: str = "running",
        gate_refused: dict[str, Any] | None = None,
    ) -> None:
        self.state = state
        self.dispatched_by = dispatched_by
        self.generation = generation
        self.workspace = workspace
        self.post_resume_state = post_resume_state
        self.gate_refused = gate_refused
        self.awaiting = {"question_note_id": "q-dd-m3", "card_entity_id": "card-dd-m3"}
        self.resumed: list[tuple[str, str]] = []
        self.refusals: list[dict[str, Any]] = []

    def get(self, development_id: str) -> dict[str, Any]:
        status: dict[str, Any] = {
            "development_id": development_id,
            "state": self.state,
            "dispatched_by": self.dispatched_by,
            "generation": self.generation,
            "awaiting": self.awaiting,
        }
        if self.workspace is not None:
            status["workspace"] = self.workspace
        if self.gate_refused is not None:
            status["gate_refused"] = self.gate_refused
        return status

    def gate(
        self, development_id: str, resume: bool = False, action_key: str | None = None
    ) -> dict[str, Any]:
        assert resume is True
        self.resumed.append((development_id, action_key or ""))
        self.state = self.post_resume_state
        return {
            "state": self.state,
            "development_id": development_id,
            "resume": {"development_id": development_id, "generation": self.generation},
        }

    def record_gate_refusal(
        self, development_id: str, *, code: str, reason: str, unit_exit_code: str | None = None
    ) -> None:
        self.refusals.append(
            {
                "development_id": development_id,
                "code": code,
                "reason": reason,
                "unit_exit_code": unit_exit_code,
            }
        )


def _call(tmp_path: Path, plane: FakeGatePlane, *, decision: str = MCP_APPROVE) -> DeliveryResult:
    return deliver_decision(
        line=DD_ID,
        decision=decision,
        reason="live drill",
        principal=DISPATCHER,
        run_root=tmp_path,
        lines=ROSTER,
        dd=plane,
        clock=lambda: 1_700_000_123.0,
    )


class TestS10Delivery:
    def test_consumed_resume_is_delivered_and_untouched_refusals(self, tmp_path: Path) -> None:
        plane = FakeGatePlane(workspace=str(tmp_path))
        result = _call(tmp_path, plane)
        assert result.status == OUTCOME_DELIVERED
        assert result.target is not None
        assert result.target["kind"] == "dd"
        assert plane.resumed == [(DD_ID, f"mcp:dd:{DD_ID}:g2:APPROVE")]
        assert plane.refusals == []

    def test_missing_workspace_is_refused_before_the_unit_starts(self, tmp_path: Path) -> None:
        plane = FakeGatePlane(workspace=str(tmp_path / "does-not-exist"))
        result = _call(tmp_path, plane)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_WORKSPACE_MISSING
        assert plane.resumed == []
        # The refusal left a trace (S10 item 2).
        assert plane.refusals
        assert plane.refusals[0]["code"] == CODE_DD_WORKSPACE_MISSING

    def test_a_unit_that_died_still_awaiting_gate_is_refused_with_exit_code(
        self, tmp_path: Path
    ) -> None:
        plane = FakeGatePlane(
            workspace=str(tmp_path),
            post_resume_state="awaiting_gate",
            gate_refused={"unit_exit_code": "75/TEMPFAIL"},
        )
        result = _call(tmp_path, plane)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_GATE_NOT_CONSUMED
        assert "75/TEMPFAIL" in result.message
        # The refusal carried the unit exit code and left a trace.
        assert plane.refusals
        assert plane.refusals[-1]["code"] == CODE_DD_GATE_NOT_CONSUMED
        assert plane.refusals[-1]["unit_exit_code"] == "75/TEMPFAIL"

    def test_an_unreadable_gate_refusal_is_still_refused(self, tmp_path: Path) -> None:
        plane = FakeGatePlane(workspace=str(tmp_path), post_resume_state="awaiting_gate")
        result = _call(tmp_path, plane)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_GATE_NOT_CONSUMED
        assert plane.refusals

    def test_absent_workspace_field_keeps_the_old_delivery_path(self, tmp_path: Path) -> None:
        plane = FakeGatePlane(workspace=None)
        result = _call(tmp_path, plane)
        assert result.status == OUTCOME_DELIVERED

    def test_principal_mismatch_still_refuses_before_any_resume(self, tmp_path: Path) -> None:
        plane = FakeGatePlane(workspace=str(tmp_path))
        result = deliver_decision(
            line=DD_ID,
            decision=MCP_APPROVE,
            reason="drill",
            principal="wf-other",
            run_root=tmp_path,
            lines=ROSTER,
            dd=plane,
            clock=lambda: 0.0,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NOT_DISPATCHING_LINE
        assert plane.resumed == []


def test_selfgate_assessment_round_trips_to_json() -> None:
    assessment = assess_evidence(_evidence())
    payload = assessment.as_dict()
    assert json.loads(json.dumps(payload)) == payload
