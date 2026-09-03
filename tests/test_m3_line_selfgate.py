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

from langgraph.checkpoint.memory import InMemorySaver

from fleet_graph.dd.control_plane import DdControlPlane
from fleet_graph.dd.selfgate import (
    DECISION_APPROVE,
    DECISION_REJECT,
    REQUIRED_EVIDENCE,
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
from fleet_graph.dd.selfgate_facts import EngineSelfGateFacts
from fleet_graph.dd.selfgate_flow import (
    RATIONALE_SCHEMA,
    harvest_eligibility,
    is_release_writable_repo,
    release_branch_ref,
    run_line_selfgate,
    template_evidence_rationale,
)
from fleet_graph.dd.selfgate_run import SelfGateExecutor, parse_pytest_summary
from fleet_graph.decision_mcp import (
    CODE_DD_GATE_NOT_CONSUMED,
    CODE_DD_WORKSPACE_MISSING,
    CODE_NOT_DISPATCHING_LINE,
    OUTCOME_DELIVERED,
    OUTCOME_REFUSED,
    DeliveryResult,
    deliver_decision,
    deliver_line_selfgate,
)
from fleet_graph.decision_mcp import (
    DECISION_APPROVE as MCP_APPROVE,
)
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.supervise.harvest_allowlist import HarvestAllowlist, parse_harvest_allowlist

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


class FakeFacts:
    """A scripted six-obligation gatherer: the engine-side ``SelfGateFacts`` port."""

    def __init__(self, evidence: dict[str, Any]) -> None:
        self.evidence = evidence
        self.seen: list[str] = []

    def gather(self, development_id: str) -> dict[str, Any]:
        self.seen.append(development_id)
        return self.evidence


class TestSelfGateOrchestration:
    """The production caller: gather six facts -> decide -> template rationale."""

    def test_clean_gate_and_matching_principal_calls_the_gatherer_and_approves(self) -> None:
        facts = FakeFacts(_evidence())
        result = run_line_selfgate(
            development_id=DD_ID,
            principal=DISPATCHER,
            dispatched_by=DISPATCHER,
            facts=facts,
        )
        assert facts.seen == [DD_ID]
        assert result.verdict == DECISION_APPROVE
        assert result.assessment.ok is True
        assert result.dispatched_by == DISPATCHER

    def test_wrong_principal_is_rejected_by_the_orchestrator(self) -> None:
        result = run_line_selfgate(
            development_id=DD_ID,
            principal="wf-other",
            dispatched_by=DISPATCHER,
            facts=FakeFacts(_evidence()),
        )
        assert result.verdict == DECISION_REJECT
        assert result.assessment.ok is False

    def test_broken_obligation_is_rejected_even_with_matching_principal(self) -> None:
        result = run_line_selfgate(
            development_id=DD_ID,
            principal=DISPATCHER,
            dispatched_by=DISPATCHER,
            facts=FakeFacts(_evidence(regression_baseline=None)),
        )
        assert result.verdict == DECISION_REJECT
        assert "regression_baseline" in result.assessment.violations[0]


class TestRationaleTemplating:
    """§4: the six results + verdict template into the decision_deliver rationale."""

    def test_rationale_is_a_machine_readable_payload_with_all_six_obligations(self) -> None:
        evidence = _evidence()
        assessment = assess_evidence(evidence)
        payload = json.loads(
            template_evidence_rationale(
                evidence=evidence,
                assessment=assessment,
                verdict=DECISION_APPROVE,
                development_id=DD_ID,
            )
        )
        assert payload["schema"] == RATIONALE_SCHEMA
        assert payload["verdict"] == DECISION_APPROVE
        assert payload["development_id"] == DD_ID
        assert payload["violations"] == []
        assert set(payload["evidence"]) == set(REQUIRED_EVIDENCE)
        assert all(entry["ok"] for entry in payload["evidence"].values())

    def test_rationale_names_each_failed_obligation(self) -> None:
        evidence = _evidence(zero_test_deletion={"ok": False, "reason": "tests/t_removed.py"})
        assessment = assess_evidence(evidence)
        payload = json.loads(
            template_evidence_rationale(
                evidence=evidence,
                assessment=assessment,
                verdict=DECISION_REJECT,
                development_id=DD_ID,
            )
        )
        assert payload["verdict"] == DECISION_REJECT
        assert any("zero_test_deletion" in v for v in payload["violations"])

    def test_rationale_normalises_a_plain_boolean_obligation(self) -> None:
        evidence = _evidence(product_diff_in_scope=False)
        assessment = assess_evidence(evidence)
        payload = json.loads(
            template_evidence_rationale(
                evidence=evidence,
                assessment=assessment,
                verdict=DECISION_REJECT,
                development_id=DD_ID,
            )
        )
        assert payload["evidence"]["product_diff_in_scope"]["ok"] is False


class TestSelfGateDelivery:
    """The integrated self-APPROVE flow, through M2 decision_deliver (spec §1/S10)."""

    def test_deliver_line_selfgate_approves_and_consumes_with_rationale(
        self, tmp_path: Path
    ) -> None:
        plane = FakeGatePlane(workspace=str(tmp_path))
        result = deliver_line_selfgate(
            development_id=DD_ID,
            principal=DISPATCHER,
            dispatched_by=DISPATCHER,
            facts=FakeFacts(_evidence()),
            run_root=tmp_path,
            lines=ROSTER,
            dd=plane,
            clock=lambda: 1_700_000_123.0,
        )
        assert result.status == OUTCOME_DELIVERED
        assert result.decision == MCP_APPROVE
        # The six-evidence rationale rides the delivery (§4), never a bare verdict.
        assert RATIONALE_SCHEMA in result.message
        assert RATIONALE_SCHEMA in result.target["reason"]
        assert plane.resumed == [(DD_ID, f"mcp:dd:{DD_ID}:g2:APPROVE")]

    def test_deliver_line_selfgate_rejects_broken_evidence(self, tmp_path: Path) -> None:
        plane = FakeGatePlane(workspace=str(tmp_path))
        result = deliver_line_selfgate(
            development_id=DD_ID,
            principal=DISPATCHER,
            dispatched_by=DISPATCHER,
            facts=FakeFacts(_evidence(regression_baseline=None)),
            run_root=tmp_path,
            lines=ROSTER,
            dd=plane,
            clock=lambda: 0.0,
        )
        assert result.decision == DECISION_REJECT

    def test_deliver_line_selfgate_derives_dispatched_by_from_the_plane(
        self, tmp_path: Path
    ) -> None:
        plane = FakeGatePlane(workspace=str(tmp_path))
        result = deliver_line_selfgate(
            development_id=DD_ID,
            principal=DISPATCHER,
            facts=FakeFacts(_evidence()),
            run_root=tmp_path,
            lines=ROSTER,
            dd=plane,
            clock=lambda: 1_700_000_123.0,
        )
        # dispatched_by defaults from dd.get(...).dispatched_by == DISPATCHER.
        assert result.status == OUTCOME_DELIVERED
        assert result.decision == MCP_APPROVE


class TestS7HarvestWiring:
    """§5/S7: harvest waits for merge, and the release/<line-id> allowlist semantics."""

    def test_harvest_eligibility_requires_the_merge_segment(self) -> None:
        assert harvest_eligibility(gate_approved=True, merge_complete=False) == (
            False,
            "merge not complete; harvest waits for the merge segment",
        )
        assert harvest_eligibility(gate_approved=True, merge_complete=True)[0] is True
        assert harvest_eligibility(gate_approved=False, merge_complete=True)[0] is False

    def test_release_branch_ref_is_the_s7_writable_release_target(self) -> None:
        assert release_branch_ref("wf-1") == "refs/heads/release/wf-1"

    def test_release_writable_repo_hits_the_allowlist_prefix(self) -> None:
        allowlist = parse_harvest_allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "allowed_branches": ["refs/heads/release/wf-1"],
                        "allowed_deploy": [],
                    }
                ]
            }
        )
        ok, reasons = is_release_writable_repo(
            allowlist, repo_path="/data/code/self/fleet-graph", line_id="wf-1"
        )
        assert ok is True
        assert reasons == ()

    def test_release_writable_repo_refuses_another_line_and_unknown_repo(self) -> None:
        allowlist = parse_harvest_allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "allowed_branches": ["refs/heads/release/wf-1"],
                        "allowed_deploy": [],
                    }
                ]
            }
        )
        ok, reasons = is_release_writable_repo(
            allowlist, repo_path="/data/code/self/fleet-graph", line_id="wf-2"
        )
        assert ok is False
        assert reasons
        ok2, reasons2 = is_release_writable_repo(allowlist, repo_path="/unknown", line_id="wf-1")
        assert ok2 is False
        assert reasons2

    def test_release_writable_repo_deny_all_by_default(self) -> None:
        ok, reasons = is_release_writable_repo(
            HarvestAllowlist.default(), repo_path="/x", line_id="wf-1"
        )
        assert ok is False
        assert reasons


class FakeControl:
    """A duck-typed dd read surface carrying exactly the fields the gatherer reads."""

    def __init__(self, **info: Any) -> None:
        self.info = info

    def get(self, development_id: str) -> dict[str, Any]:
        self.info["development_id"] = development_id
        return self.info


class TestEngineSelfGateFacts:
    """The production gatherer measures the six obligations from the read model."""

    def test_full_positive_measurements_gather_six_ok(self) -> None:
        control = FakeControl(
            spec_acceptance_commands=ACCEPT,
            acceptance_commands=ACCEPT,
            verification_commands=ACCEPT,
            changed_paths=["src/fleet_graph/x.py"],
            scope_paths=["src/fleet_graph/x.py"],
            deleted_paths=[],
            acceptance_runs=[{"argv": ACCEPT, "exit_code": 0}],
            mutations=[{"red": True, "restored": True}, {"red": True, "restored": True}],
            target_base_commit="base",
            compared_base_commit="base",
            baseline_run={"passed": 100, "failed": 0, "failed_set": []},
            head_run={"passed": 100, "failed": 0, "failed_set": []},
        )
        facts = EngineSelfGateFacts(control).gather("dev-fg-m3")
        assert set(facts) == set(REQUIRED_EVIDENCE)
        assert all(entry["ok"] for entry in facts.values())

    def test_a_missing_acceptance_transcript_is_measured_negative(self) -> None:
        control = FakeControl(
            spec_acceptance_commands=ACCEPT,
            acceptance_commands=ACCEPT,
            verification_commands=[],  # receipt missing -> refuse, never a guess
        )
        facts = EngineSelfGateFacts(control).gather("dev-fg-m3")
        assert facts["acceptance_verbatim"]["ok"] is False

    def test_an_out_of_scope_product_change_is_measured_negative(self) -> None:
        control = FakeControl(
            changed_paths=["src/out-of-scope.py"],
            scope_paths=["src/fleet_graph/x.py"],
        )
        facts = EngineSelfGateFacts(control).gather("dev-fg-m3")
        assert facts["product_diff_in_scope"]["ok"] is False
        assert "src/out-of-scope.py" in facts["product_diff_in_scope"]["reason"]

    def test_a_green_to_red_regression_is_measured_negative(self) -> None:
        control = FakeControl(
            target_base_commit="base",
            compared_base_commit="base",
            baseline_run={"passed": 10, "failed": 0, "failed_set": []},
            head_run={"passed": 9, "failed": 1, "failed_set": ["new:test"]},
        )
        facts = EngineSelfGateFacts(control).gather("dev-fg-m3")
        assert facts["regression_baseline"]["ok"] is False
        assert "new:test" in facts["regression_baseline"]["reason"]


class _FakeSelfGatePort:
    """A scripted ``SelfGatePort`` for the goal-line graph wiring test."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.called: list[str] = []

    def run(self, development_id: str) -> dict[str, Any]:
        self.called.append(development_id)
        return self.reply


class _FakeCoordinator:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def turn(
        self, round_no: int, coord_input: dict[str, Any], resume: bool = False
    ) -> dict[str, Any]:
        self.inputs.append(coord_input)
        return {"verdict": "done", "reason": "self-gated"}


class _FakeReviewWorker:
    def turn(self, prompt: str, round_no: int) -> Any:
        raise AssertionError("the self-gate path must not reach the worker")


class _FakeReviewInbox:
    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


class _FakeReviewArtifacts:
    def __init__(self) -> None:
        self.rounds: list[dict[str, Any]] = []
        self.terminal: dict[str, Any] | None = None

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        self.rounds.append(line)
        return True

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> str:
        return "worker-report.json"

    def write_terminal(self, **kwargs: Any) -> str:
        self.terminal = kwargs
        return "terminal.json"


class TestGoalLineSelfGateWiring:
    """The engine's own caller: a line woken by ``dd_awaiting_gate`` self-gates."""

    def test_the_line_discharges_the_six_obligations_and_templates_the_evidence(
        self,
    ) -> None:
        rationale = '{"schema": "fleet-graph.selfgate-rationale/v1", "verdict": "APPROVE"}'
        port = _FakeSelfGatePort({"verdict": "APPROVE", "rationale": rationale})
        artifacts = _FakeReviewArtifacts()
        coordinator = _FakeCoordinator()
        deps = LineDeps(
            coordinator=coordinator,
            worker=_FakeReviewWorker(),  # type: ignore[arg-type]
            inbox=_FakeReviewInbox(),  # type: ignore[arg-type]
            artifacts=artifacts,  # type: ignore[arg-type]
            guards=LineGuards(bounds=LineBounds()),
            folder_id=DISPATCHER,
            selfgate=port,
            prior_terminal={
                "terminal": "blocked",
                "waiting_on": "dd",
                "dd_development_id": DD_ID,
            },
        )
        compiled = build_goal_line_graph(deps).compile(
            checkpointer=InMemorySaver()  # type: ignore[call-arg]
        )
        compiled.invoke(
            {"round_no": 1},
            config={"configurable": {"thread_id": "th-selfgate"}, "recursion_limit": 100},
        )
        # The mechanical gate ran against the dispatched development.
        assert port.called == [DD_ID]
        # §4: the evidence was templated into the line's progress, never a bare word.
        assert any(round_["verdict"] == "selfgate" for round_ in artifacts.rounds)
        progress = next(round_ for round_ in artifacts.rounds if round_["verdict"] == "selfgate")
        assert progress["selfgate_rationale"] == rationale
        # The self-gate result rode the coordinator input as a mechanical fact.
        assert coordinator.inputs, "the coordinator was not (re-)consulted"
        assert coordinator.inputs[0]["selfgate"]["verdict"] == "APPROVE"

    def test_a_line_with_no_dd_anchor_keeps_the_pre_m3_path(self) -> None:
        port = _FakeSelfGatePort({"verdict": "APPROVE", "rationale": "x"})
        artifacts = _FakeReviewArtifacts()
        coordinator = _FakeCoordinator()
        deps = LineDeps(
            coordinator=coordinator,
            worker=_FakeReviewWorker(),  # type: ignore[arg-type]
            inbox=_FakeReviewInbox(),  # type: ignore[arg-type]
            artifacts=artifacts,  # type: ignore[arg-type]
            guards=LineGuards(bounds=LineBounds()),
            folder_id=DISPATCHER,
            selfgate=port,
            prior_terminal={"terminal": "blocked", "waiting_on": "decision"},
        )
        compiled = build_goal_line_graph(deps).compile(
            checkpointer=InMemorySaver()  # type: ignore[call-arg]
        )
        compiled.invoke(
            {"round_no": 1},
            config={"configurable": {"thread_id": "th-nodd"}, "recursion_limit": 100},
        )
        # No dd anchor -> the self-gate never fired, the line ran its normal path.
        assert port.called == []


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _fake_run(returncode: int):
    def run(argv: list[str], **kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode)

    return run


class _FakeExecutor:
    """A scripted ``SelfGateExecutor`` recording the calls the gatherer makes."""

    def __init__(self) -> None:
        self.rerun: list[str] = []
        self.mutations: list[str] = []
        self.regression: list[str] = []

    def rerun_acceptance(self, argvs: list[list[str]], *, cwd: str) -> list[dict[str, Any]]:
        self.rerun.append(cwd)
        return [{"argv": list(argv), "exit_code": 0} for argv in argvs]

    def fire_mutation_gun(
        self, argvs: list[list[str]], *, cwd: str, product_paths: list[str], shots: int = 2
    ) -> list[dict[str, Any]]:
        self.mutations.append(cwd)
        return [{"path": p, "red": True, "restored": True} for p in product_paths[:shots]]

    def full_regression(
        self, *, repo: str, base_commit: str, head_commit: str, test_argv: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        self.regression.append(repo)
        ok = {"passed": 10, "failed": 0, "skipped": 0, "failed_set": []}
        return {"baseline_run": ok, "head_run": ok}


class TestParsePytestSummary:
    def test_parses_pass_fail_skip_counts_and_the_failed_set(self) -> None:
        summary = parse_pytest_summary(
            "FAILED tests/test_x.py::TestY::test_z - assert False\n"
            "1 failed, 2 passed, 1 skipped in 1.02s\n"
        )
        assert summary == {
            "passed": 2,
            "failed": 1,
            "skipped": 1,
            "failed_set": ["tests/test_x.py::TestY::test_z"],
        }

    def test_a_clean_run_has_no_failed_set(self) -> None:
        assert parse_pytest_summary("50 passed in 3.20s\n")["failed"] == 0
        assert parse_pytest_summary("50 passed in 3.20s\n")["failed_set"] == []


class TestGathererPerformsTheObligations:
    """The engine performs §2.4/§2.5/§2.6 rather than reading absent facts."""

    def test_when_transcripts_are_absent_the_gatherer_runs_the_executor(
        self, tmp_path: Path
    ) -> None:
        control = FakeControl(
            spec_acceptance_commands=ACCEPT,
            acceptance_commands=ACCEPT,
            verification_commands=ACCEPT,
            worktree_path=str(tmp_path),
            target_base_commit="base",
            head_commit="head",
            compared_base_commit="base",
            changed_paths=["src/a.py", "src/b.py"],
        )
        executor = _FakeExecutor()
        facts = EngineSelfGateFacts(control, executor=executor).gather("dev-fg-m3")
        assert executor.rerun == [str(tmp_path)]
        assert executor.mutations == [str(tmp_path)]
        assert executor.regression == [str(tmp_path)]
        assert all(entry["ok"] for entry in facts.values())


class TestMutationGunExecution:
    def test_a_shot_turns_acceptance_red_and_restores_the_bytes(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
        executor = SelfGateExecutor(run=_fake_run(1), git=lambda *a, **k: _FakeProc(0))
        shots = executor.fire_mutation_gun(
            [ACCEPT], cwd=str(tmp_path), product_paths=["a.py", "b.py"]
        )
        assert len(shots) == 2
        assert all(shot["red"] is True and shot["restored"] is True for shot in shots)
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"
        assert (tmp_path / "b.py").read_text(encoding="utf-8") == "y = 2\n"

    def test_out_of_scope_mutation_still_runs_the_frozen_acceptance(self, tmp_path: Path) -> None:
        executor = SelfGateExecutor(run=_fake_run(0), git=lambda *a, **k: _FakeProc(0))
        runs = executor.rerun_acceptance([ACCEPT], cwd=str(tmp_path))
        assert runs == [{"argv": ACCEPT, "exit_code": 0}]


class _FakeBoard:
    def __init__(self) -> None:
        self.evidence_calls: list[dict[str, str]] = []

    def evidence(self, *, card_entity_id: str, text: str, idempotency_key: str) -> None:
        self.evidence_calls.append({"card": card_entity_id, "text": text, "key": idempotency_key})


class TestSelfGateEvidenceNote:
    def test_the_rationale_lands_as_a_board_evidence_note(self, tmp_path: Path) -> None:
        (tmp_path / DD_ID).mkdir()
        (tmp_path / DD_ID / "record.json").write_text(
            json.dumps({"card_entity_id": "card-1", "generation": 2}), encoding="utf-8"
        )
        board = _FakeBoard()
        control = DdControlPlane(root=tmp_path, board_factory=lambda: board)
        assert (
            control.publish_selfgate_evidence(
                DD_ID, verdict="APPROVE", rationale='{"schema": "x", "verdict": "APPROVE"}'
            )
            is True
        )
        assert board.evidence_calls == [
            {
                "card": "card-1",
                "text": '{"schema": "x", "verdict": "APPROVE"}',
                "key": f"dd-selfgate:{DD_ID}:g2:APPROVE",
            }
        ]

    def test_a_single_without_a_card_writes_no_note(self, tmp_path: Path) -> None:
        (tmp_path / DD_ID).mkdir()
        (tmp_path / DD_ID / "record.json").write_text(
            json.dumps({"card_entity_id": "", "generation": 1}), encoding="utf-8"
        )
        board = _FakeBoard()
        control = DdControlPlane(root=tmp_path, board_factory=lambda: board)
        assert control.publish_selfgate_evidence(DD_ID, verdict="REJECT", rationale="x") is False
        assert board.evidence_calls == []
