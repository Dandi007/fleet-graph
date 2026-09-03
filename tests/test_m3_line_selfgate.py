"""M3 line-self-gate: S10/S11 delivery hardening + the six evidence obligations.

The spec (wf-8d9737 M3) makes the line self-gate the fleet default path. Two
amendments are *not weakenable* and are tested red-then-green here first:

- **S11** -- the two dd delivery forms must share one authorization path. A
  non-dispatching principal using form A (``target_kind="dd"`` + ``target_id``)
  must get ``NOT_DISPATCHING_LINE`` and leave the single untouched. Before the
  fix, form A only answered ``DD_NOT_FOUND``/``DD_NOT_AWAITING_GATE`` and never
  checked identity -- any caller could cast any line's gate verdict.
- **S10** -- a resume's success is "consumed", not "a unit was started". A
  missing workspace is refused *before* any unit starts; a resume whose single
  is still ``awaiting_gate`` afterwards is refused with the unit's exit code;
  and every refusal leaves a ``gate_refused`` + ``events.jsonl`` trace.

The six evidence obligations (the gate's mandatory answer fields) are exercised
through ``fleet_graph.selfgate``: missing any one refuses a delivery.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import git, head
from fleet_graph.dd.control_plane import (
    CHECKPOINT_FILE,
    DdControlPlane,
    derive_acceptance_commands,
)
from fleet_graph.decision_mcp import (
    CODE_DD_NOT_CONSUMED,
    CODE_DD_NOT_FOUND,
    CODE_DD_WORKSPACE_MISSING,
    CODE_GATE_EVIDENCE_DECISION_MISMATCH,
    CODE_GATE_EVIDENCE_MISSING,
    CODE_NOT_DISPATCHING_LINE,
    DECISION_APPROVE,
    DECISION_REJECT,
    OUTCOME_DELIVERED,
    OUTCOME_REFUSED,
    TARGET_KIND_DD,
    DeliveryLedger,
    deliver_decision,
    deliver_decision_dd,
)
from fleet_graph.graphs.self_gate import SelfGateResult, perform_line_self_gate
from fleet_graph.graphs.self_gate_port import DdGateSelfGatePort
from fleet_graph.selfgate import (
    GATE_EVIDENCE_FIELDS,
    SELF_GATE_APPROVE,
    SELF_GATE_REJECT,
    GateEvidenceInputs,
    GateEvidenceMissing,
    SuiteSnapshot,
    acceptance_equality,
    diff_in_scope,
    gate_decision,
    gate_evidence_payload,
    gather_gate_evidence,
    missing_gate_evidence,
    regression_obligation,
    regression_verdict,
    require_gate_evidence,
    rerun_acceptance,
    two_shot_mutation_gun,
    zero_test_deletion,
)
from fleet_graph.state.run_artifacts import write_json_durable

DD_ID = "dev-fg-36c2d76baca7"
DISPATCHER = "wf-8d9737"
ROSTER: list[Any] = [{"folder_id": "wf-8d9737", "seat": "s", "generation": 1}]

#: A complete six-field evidence payload the self-gate delivery is bound to.
#: The six obligations are the gate's mandatory answer fields; the mechanism
#: tests pass this so the delivery reaches the S10/S11 mechanics being drilled.
COMPLETE_EVIDENCE: dict[str, Any] = {field: {"ok": True} for field in GATE_EVIDENCE_FIELDS}

ACCEPTANCE_ARGV = [["uv", "run", "pytest", "-q", "tests/test_m3_line_selfgate.py"]]


def _passing_evidence() -> dict[str, Any]:
    """Evidence whose six recorded answers genuinely all pass.

    Built by the real obligation functions -- exactly the payload
    ``gather_gate_evidence`` produces on a green single -- so an APPROVE
    delivery drills the mechanics with answers that support it.
    """
    return {
        "acceptance_equality": acceptance_equality(
            ACCEPTANCE_ARGV, ACCEPTANCE_ARGV, ACCEPTANCE_ARGV
        ),
        "diff_in_scope": diff_in_scope(
            ["src/fleet_graph/selfgate.py"], ["src/fleet_graph/selfgate.py"]
        ),
        "zero_test_deletion": zero_test_deletion([]),
        "rerun_acceptance": rerun_acceptance(ACCEPTANCE_ARGV, lambda argv: (0, "ok")),
        "mutation": {"two_shots": True, "red": True, "restored": True, "shots": []},
        "regression": regression_obligation(
            target_base_commit="frozen-base",
            baseline=SuiteSnapshot(passed=106, failed=0, total=106, green_tests={"a"}),
            head=SuiteSnapshot(passed=106, failed=0, total=106),
        ),
    }


PASSING_EVIDENCE: dict[str, Any] = _passing_evidence()


def _failing_evidence() -> dict[str, Any]:
    """Evidence whose recorded answers fail obligation 1 (the forged shape the
    delivery path must refuse, even though all six keys are present)."""
    evidence = dict(PASSING_EVIDENCE)
    evidence["acceptance_equality"] = acceptance_equality(
        ACCEPTANCE_ARGV, ACCEPTANCE_ARGV, [["echo", "tampered"]]
    )
    return evidence


class FakeDdPlane:
    """A duck-typed dd control plane whose resume semantics are scriptable.

    ``consume_on_resume`` controls whether ``gate()`` moves the single out of
    ``awaiting_gate`` (the S10 "left the gate" truth) or leaves it stuck --
    modelling the observed 889ms ``75/TEMPFAIL`` unit death. ``unit_exit_code``
    is what ``get()`` reports after such a death. ``workspace`` (when set) is
    the ``repo_path`` the delivery must pre-check exists.
    """

    def __init__(
        self,
        *,
        state: str = "awaiting_gate",
        dispatched_by: str = DISPATCHER,
        generation: int = 1,
        workspace: Path | None = None,
        consume_on_resume: bool = True,
        unit_exit_code: str = "",
    ) -> None:
        self.state = state
        self.dispatched_by = dispatched_by
        self.generation = generation
        self.workspace = workspace
        self.consume_on_resume = consume_on_resume
        self.unit_exit_code = unit_exit_code
        self.resumed: list[str] = []
        self.refusals: list[dict[str, Any]] = []

    def get(self, development_id: str) -> dict[str, Any]:
        return {
            "development_id": development_id,
            "state": self.state,
            "dispatched_by": self.dispatched_by,
            "generation": self.generation,
            "awaiting": {"question_note_id": "q-1", "card_entity_id": "card-1"},
            "repo_path": str(self.workspace) if self.workspace is not None else "",
            "worktree_path": str(self.workspace) if self.workspace is not None else "",
            "unit_exit_code": self.unit_exit_code,
        }

    def gate(
        self, development_id: str, resume: bool = False, action_key: str | None = None
    ) -> dict[str, Any]:
        assert resume is True
        self.resumed.append(action_key or "")
        if self.consume_on_resume:
            self.state = "running"
        return {"resume": {"development_id": development_id, "generation": self.generation}}

    def record_gate_refusal(
        self,
        development_id: str,
        *,
        decision: str = "",
        reason: str = "",
        unit_exit_code: str = "",
    ) -> None:
        self.refusals.append(
            {
                "development_id": development_id,
                "decision": decision,
                "reason": reason,
                "unit_exit_code": unit_exit_code,
            }
        )


def _deliver(
    plane: FakeDdPlane,
    tmp_path: Path,
    *,
    target_id: str = DD_ID,
    decision: str = DECISION_APPROVE,
    principal: str = DISPATCHER,
    evidence: dict[str, Any] | None = None,
) -> Any:
    return deliver_decision(
        line="",
        decision=decision,
        reason="m3 drill",
        principal=principal,
        run_root=tmp_path,
        lines=ROSTER,
        target_kind=TARGET_KIND_DD,
        target_id=target_id,
        dd=plane,
        evidence=evidence if evidence is not None else PASSING_EVIDENCE,
    )


# ---------------------------------------------------------------------------
# S11: the two dd delivery forms share one authorization path
# ---------------------------------------------------------------------------


class TestS11UnifiedDdAuthorization:
    def test_form_a_non_dispatching_principal_is_refused_and_single_untouched(
        self, tmp_path: Path
    ) -> None:
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, principal="wf-other")
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NOT_DISPATCHING_LINE
        assert "wf-other" in result.message
        assert "wf-8d9737" in result.message
        assert plane.resumed == []

    def test_form_a_empty_principal_is_not_the_dispatching_line(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, principal="")
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NOT_DISPATCHING_LINE
        assert plane.resumed == []

    def test_form_a_dispatching_principal_is_delivered(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path)
        assert result.status == OUTCOME_DELIVERED
        assert result.as_dict()["outcome"] == "consumed"
        assert plane.resumed == [f"mcp:dd:{DD_ID}:g1:APPROVE"]

    def test_form_b_non_dispatching_principal_is_still_refused(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = deliver_decision(
            line=DD_ID,
            decision=DECISION_REJECT,
            reason="m3 drill",
            principal="wf-other",
            run_root=tmp_path,
            lines=ROSTER,
            dd=plane,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NOT_DISPATCHING_LINE
        assert plane.resumed == []

    def test_deliver_decision_dd_authorizes_too(self, tmp_path: Path) -> None:
        """The direct dd-deliver entry is not a bypass: it validates principal."""

        class Source:
            def __init__(self) -> None:
                self.plane = FakeDdPlane()

            def _control_plane(self) -> FakeDdPlane:
                return self.plane

        source = Source()
        result = deliver_decision_dd(
            target_id=DD_ID,
            decision=DECISION_APPROVE,
            reason="m3 drill",
            dd_source=source,  # type: ignore[arg-type]
            principal="wf-other",
            run_root=tmp_path,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NOT_DISPATCHING_LINE
        assert source.plane.resumed == []

    def test_unknown_dd_is_still_dd_not_found(self, tmp_path: Path) -> None:
        class UnknownPlane(FakeDdPlane):
            def get(self, development_id: str) -> dict[str, Any]:
                from fleet_graph.dd.control_plane import ControlPlaneError

                raise ControlPlaneError(
                    "DEVELOPMENT_NOT_FOUND", f"no admission for {development_id}"
                )

        result = _deliver(UnknownPlane(), tmp_path, target_id="dev-fg-nope")
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_NOT_FOUND


# ---------------------------------------------------------------------------
# S10: consumed != started; workspace pre-check; refusal trace
# ---------------------------------------------------------------------------


class TestS10ConsumptionIsNotUnitStart:
    def test_missing_workspace_is_refused_before_any_unit(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        plane = FakeDdPlane(workspace=missing)
        result = _deliver(plane, tmp_path)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_WORKSPACE_MISSING
        # No unit was started: gate() was never called.
        assert plane.resumed == []
        # The refusal left a trace with a reason.
        assert any(r["development_id"] == DD_ID for r in plane.refusals)

    def test_unit_started_but_still_awaiting_gate_is_not_consumed(self, tmp_path: Path) -> None:
        plane = FakeDdPlane(consume_on_resume=False, unit_exit_code="75/TEMPFAIL")
        result = _deliver(plane, tmp_path)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_NOT_CONSUMED
        assert "75/TEMPFAIL" in result.message
        # A refusal trace carries the unit exit code.
        assert any(r["unit_exit_code"] == "75/TEMPFAIL" for r in plane.refusals)

    def test_consumed_resume_is_delivered(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path)
        assert result.status == OUTCOME_DELIVERED
        assert result.as_dict()["outcome"] == "consumed"

    def test_reject_riding_failing_evidence_is_a_consumed_verdict(self, tmp_path: Path) -> None:
        """The self-gate's REJECT path: evidence whose recorded answers fail is
        *consistent* with the REJECT verdict it supports, so the refusal rides
        the delivery and consumes the gate (the work is refused, not the
        delivery)."""
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, decision=DECISION_REJECT, evidence=_failing_evidence())
        assert result.status == OUTCOME_DELIVERED
        assert result.decision == DECISION_REJECT


# ---------------------------------------------------------------------------
# the six obligations are wired into the delivery path (not an orphaned module)
# ---------------------------------------------------------------------------


class TestEvidenceEnforcedByTheDeliveryPath:
    def test_missing_evidence_is_refused_before_any_resume(self, tmp_path: Path) -> None:
        """The negative criterion has a real engine path: a self-gate delivery
        with no evidence is refused, not delivered (spec item 2's 缺任一项)."""
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, evidence={})
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_GATE_EVIDENCE_MISSING
        for field in GATE_EVIDENCE_FIELDS:
            assert field in result.message
        assert plane.resumed == []

    def test_an_incomplete_payload_names_every_missing_field(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, evidence={"acceptance_equality": {"ok": True}})
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_GATE_EVIDENCE_MISSING
        for field in set(GATE_EVIDENCE_FIELDS) - {"acceptance_equality"}:
            assert field in result.message
        assert plane.resumed == []

    def test_complete_evidence_is_delivered_with_a_digest_rationale(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, evidence=PASSING_EVIDENCE)
        assert result.status == OUTCOME_DELIVERED
        # The rationale (digest + field attestation) rides the result, keyed to
        # exactly the six fields -- the machine check that the gate's 必答字段
        # travelled with the verdict (spec item 4).
        assert result.evidence == gate_evidence_payload(PASSING_EVIDENCE)
        assert result.evidence["fields"] == {field: True for field in GATE_EVIDENCE_FIELDS}
        assert result.evidence["digest"].startswith("sha256:")
        assert result.as_dict()["evidence"] == result.evidence

    def test_the_ledger_entry_embeds_the_evidence_digest(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, evidence=PASSING_EVIDENCE)
        assert result.status == OUTCOME_DELIVERED
        ledger = DeliveryLedger(state_dir=tmp_path / "ledger")
        ledger.record(result)
        entry = ledger.entries()[0]
        assert entry["status"] == OUTCOME_DELIVERED
        assert entry["evidence"] == result.evidence
        assert entry["evidence"]["digest"] == gate_evidence_payload(PASSING_EVIDENCE)["digest"]


# ---------------------------------------------------------------------------
# the delivery path enforces the evidence *verdict*, not just key presence
# ---------------------------------------------------------------------------


class TestDeliveryEnforcesTheEvidenceVerdict:
    """rc-b79ebd77 blocker 2: presence of six truthy keys is not enforcement.

    The delivery path now runs ``gate_decision`` -- the six obligations' own
    verdict join -- so a caller-forged dict whose recorded answers are
    failures can no longer carry an APPROVE through the gate."""

    def test_a_forged_approve_over_failing_evidence_is_refused(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, evidence=_failing_evidence())
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_GATE_EVIDENCE_DECISION_MISMATCH
        assert "APPROVE" in result.message and "REJECT" in result.message
        # The single was never touched.
        assert plane.resumed == []

    def test_missing_and_failing_are_distinct_refusals(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        missing = _deliver(plane, tmp_path, evidence={})
        failed = _deliver(plane, tmp_path, evidence=_failing_evidence())
        assert missing.code == CODE_GATE_EVIDENCE_MISSING
        assert failed.code == CODE_GATE_EVIDENCE_DECISION_MISMATCH
        assert plane.resumed == []

    def test_passing_evidence_cannot_cast_a_reject(self, tmp_path: Path) -> None:
        """Consistency is exact, both directions: a delivery's verdict must be
        the one the six answers support."""
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, decision=DECISION_REJECT, evidence=PASSING_EVIDENCE)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_GATE_EVIDENCE_DECISION_MISMATCH
        assert plane.resumed == []

    def test_the_mismatch_refusal_is_recorded_in_the_ledger(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, evidence=_failing_evidence())
        assert result.status == OUTCOME_REFUSED
        ledger = DeliveryLedger(state_dir=tmp_path / "ledger")
        entry = ledger.record(result)
        assert entry["code"] == CODE_GATE_EVIDENCE_DECISION_MISMATCH


# ---------------------------------------------------------------------------
# six evidence obligations: the gate's mandatory answer fields
# ---------------------------------------------------------------------------


class TestSixEvidenceObligations:
    def test_missing_any_mandatory_field_refuses(self) -> None:
        with pytest.raises(GateEvidenceMissing) as excinfo:
            require_gate_evidence({})
        missing = set(excinfo.value.missing)
        assert missing == set(GATE_EVIDENCE_FIELDS)

    def test_a_complete_payload_passes(self) -> None:
        payload = {field: {"ok": True} for field in GATE_EVIDENCE_FIELDS}
        assert require_gate_evidence(payload) is payload
        assert missing_gate_evidence(payload) == ()

    def test_acceptance_equality_is_machine_exact(self) -> None:
        argv = [["uv", "run", "pytest", "-q", "tests/test_m3_line_selfgate.py"]]
        assert acceptance_equality(argv, argv, argv)["equal"] is True
        assert acceptance_equality(argv, argv, [argv[0][::-1]])["equal"] is False

    def test_diff_outside_declared_surface_is_out_of_scope(self) -> None:
        verdict = diff_in_scope(
            ["src/fleet_graph/selfgate.py"], ["src/fleet_graph/decision_mcp.py"]
        )
        assert verdict["in_scope"] is False
        assert verdict["out_of_scope"] == ["src/fleet_graph/selfgate.py"]

    def test_machine_paths_are_always_in_scope(self) -> None:
        verdict = diff_in_scope([".dev-dispatch/spec/approved.md"], [])
        assert verdict["in_scope"] is True
        assert verdict["out_of_scope"] == []

    def test_zero_test_deletion_detects_a_deleted_test(self) -> None:
        assert zero_test_deletion([])["zero"] is True
        assert zero_test_deletion(["tests/test_x.py"])["zero"] is False
        assert zero_test_deletion(["src/fleet_graph/selfgate.py"])["zero"] is True


class TestMutationGun:
    def test_two_shots_red_then_restore(self, tmp_path: Path) -> None:
        target = tmp_path / "prod.py"
        target.write_bytes(b"def f():\n    return 0\n")

        def shot_one(original: bytes) -> bytes:
            return original.replace(b"return 0", b"return 1")

        def shot_two(original: bytes) -> bytes:
            return original + b"\n# mutation\n"

        def accept() -> int:
            # The "frozen acceptance" reds as soon as the byte differs.
            return 0 if target.read_bytes() == b"def f():\n    return 0\n" else 1

        result = two_shot_mutation_gun(target, mutations=[shot_one, shot_two], accept=accept)
        assert result["red"] is True
        assert result["restored"] is True
        assert target.read_bytes() == b"def f():\n    return 0\n"


class TestRegressionVerdict:
    def test_red_set_must_not_grow(self) -> None:
        baseline = SuiteSnapshot(passed=106, failed=0, total=106, green_tests={"a", "b", "c"})
        head = SuiteSnapshot(passed=31, failed=75, total=106, failed_tests={"a", "d", "e"})
        verdict = regression_verdict(baseline, head)
        assert verdict["pass"] is False
        assert verdict["red_set_grew"] is True
        assert verdict["green_to_red_flip"] is True
        assert "a" in verdict["green_to_red"]

    def test_red_to_green_is_an_improvement_never_refused(self) -> None:
        baseline = SuiteSnapshot(passed=105, failed=1, total=106, failed_tests={"a"})
        head = SuiteSnapshot(passed=106, failed=0, total=106)
        assert regression_verdict(baseline, head)["pass"] is True

    def test_flake_attribution_clears_a_sole_red_increment(self) -> None:
        baseline = SuiteSnapshot(passed=106, failed=0, total=106)
        head = SuiteSnapshot(passed=105, failed=1, total=106, failed_tests={"flakey"})
        verdict = regression_verdict(baseline, head, flake_attribution={"cleared": ["flakey"]})
        assert verdict["pass"] is True
        assert verdict["flake_attribution"] == {"cleared": ["flakey"]}


# ---------------------------------------------------------------------------
# S9 four款 through regression_obligation (missing baseline / frozen anchor)
# ---------------------------------------------------------------------------


class TestRegressionObligation:
    def test_missing_baseline_fields_refuse(self) -> None:
        """缺基线字段拒 (判据第 6 条第一款): a missing frozen target_base_commit,
        baseline snapshot, or head snapshot is itself a refusal -- never a
        silent pass, never a comparison against a guessed baseline."""
        verdict = regression_obligation(
            target_base_commit=None,
            baseline=SuiteSnapshot(passed=106, failed=0, total=106),
            head=SuiteSnapshot(passed=106, failed=0, total=106),
        )
        assert verdict["pass"] is False
        assert verdict["refusal"] == "missing_baseline"
        assert "target_base_commit" in verdict["missing"]

        verdict = regression_obligation(
            target_base_commit="base",
            baseline=None,
            head=SuiteSnapshot(passed=106, failed=0, total=106),
        )
        assert verdict["pass"] is False
        assert "baseline" in verdict["missing"]

        verdict = regression_obligation(
            target_base_commit="base",
            baseline=SuiteSnapshot(passed=106, failed=0, total=106),
            head=None,
        )
        assert verdict["pass"] is False
        assert "head" in verdict["missing"]

    def test_green_to_red_flip_refuses(self) -> None:
        baseline = SuiteSnapshot(passed=106, failed=0, total=106, green_tests={"a", "b"})
        head = SuiteSnapshot(passed=105, failed=1, total=106, failed_tests={"a"})
        verdict = regression_obligation(target_base_commit="base", baseline=baseline, head=head)
        assert verdict["pass"] is False
        assert verdict["green_to_red_flip"] is True
        assert "a" in verdict["green_to_red"]

    def test_red_set_growth_refuses_even_when_baseline_is_red(self) -> None:
        """红项集合扩大(含基线本身红时再添新红) → refuse (判据第 6 条第三款)."""
        baseline = SuiteSnapshot(passed=105, failed=1, total=106, failed_tests={"a"})
        head = SuiteSnapshot(passed=104, failed=2, total=106, failed_tests={"a", "b"})
        verdict = regression_obligation(target_base_commit="base", baseline=baseline, head=head)
        assert verdict["pass"] is False
        assert verdict["red_set_grew"] is True
        assert verdict["red_growth"] == ["b"]

    def test_a_red_baseline_with_no_growth_passes(self) -> None:
        """基线红但红集未扩 → pass (基线本身红不是本单的错)."""
        baseline = SuiteSnapshot(passed=105, failed=1, total=106, failed_tests={"a"})
        head = SuiteSnapshot(passed=105, failed=1, total=106, failed_tests={"a"})
        verdict = regression_obligation(target_base_commit="base", baseline=baseline, head=head)
        assert verdict["pass"] is True

    def test_drifted_main_is_ignored_and_frozen_target_base_is_the_anchor(self) -> None:
        """gate 时 main 已漂移仍按冻结 target_base 比对 (判据第 6 条第四款):
        a drifted main head is recorded as ignored and never consulted -- the
        verdict is computed purely from the frozen target_base- anchored
        snapshots."""
        baseline = SuiteSnapshot(passed=106, failed=0, total=106, green_tests={"a"})
        head = SuiteSnapshot(passed=106, failed=0, total=106)
        verdict = regression_obligation(
            target_base_commit="frozen-target-base",
            baseline=baseline,
            head=head,
            main_head_commit="drifted-main-head",
        )
        assert verdict["pass"] is True
        assert verdict["target_base_commit"] == "frozen-target-base"
        # The drifted main was seen and explicitly ignored, never folded in.
        assert verdict["ignored_main_head_commit"] == "drifted-main-head"


# ---------------------------------------------------------------------------
# the six obligations are mechanically performed (not a forged six-key dict)
# ---------------------------------------------------------------------------


def _mutation_gadget(tmp_path: Path) -> tuple[Any, list[Any], Any]:
    """A real mutation target + two mutations that must red the frozen accept."""
    target = tmp_path / "prod.py"
    original = b"def stable():\n    return 42\n"
    target.write_bytes(original)

    def shot_one(data: bytes) -> bytes:
        return data.replace(b"return 42", b"return 43")

    def shot_two(data: bytes) -> bytes:
        return data + b"\n# injected\n"

    def accept() -> int:
        return 0 if target.read_bytes() == original else 1

    return target, [shot_one, shot_two], accept


def _all_pass_inputs(tmp_path: Path) -> GateEvidenceInputs:
    target, shots, accept = _mutation_gadget(tmp_path)
    argv = [["uv", "run", "pytest", "-q", "tests/test_m3_line_selfgate.py"]]
    return GateEvidenceInputs(
        spec_argv=argv,
        record_argv=argv,
        receipt_argv=argv,
        changed_paths=["src/fleet_graph/selfgate.py"],
        declared_paths=["src/fleet_graph/selfgate.py"],
        deleted_paths=[],
        acceptance_commands=argv,
        acceptance_runner=lambda a: (0, "ok"),
        mutation_target=target,
        mutations=shots,
        mutation_accept=accept,
        target_base_commit="frozen-base",
        baseline=SuiteSnapshot(passed=106, failed=0, total=106, green_tests={"a"}),
        head=SuiteSnapshot(passed=106, failed=0, total=106),
    )


class TestGatherGateEvidence:
    def test_all_six_obligations_are_mechanically_run(self, tmp_path: Path) -> None:
        """gather_gate_evidence invokes the six obligations: the evidence payload
        has all six mandatory fields, each a *real* obligation answer -- the
        acceptance rerun echo in particular (obligation 4) is produced here, not
        trusted from a caller."""
        evidence = gather_gate_evidence(_all_pass_inputs(tmp_path))
        assert set(evidence.keys()) == set(GATE_EVIDENCE_FIELDS)

        assert evidence["acceptance_equality"]["equal"] is True
        assert evidence["diff_in_scope"]["in_scope"] is True
        assert evidence["zero_test_deletion"]["zero"] is True
        # Obligation 4: rerun ran the frozen argv and kept the echo.
        assert evidence["rerun_acceptance"]["rerun"] is True
        assert evidence["rerun_acceptance"]["commands"][0]["exit_code"] == 0
        assert evidence["mutation"]["red"] is True
        assert evidence["mutation"]["restored"] is True
        assert evidence["regression"]["pass"] is True
        assert evidence["regression"]["target_base_commit"] == "frozen-base"

    def test_an_out_of_scope_diff_flips_the_verdict_to_reject(self, tmp_path: Path) -> None:
        inputs = _all_pass_inputs(tmp_path)
        inputs.changed_paths = ["src/fleet_graph/selfgate.py"]
        inputs.declared_paths = ["src/fleet_graph/decision_mcp.py"]
        evidence = gather_gate_evidence(inputs)
        assert evidence["diff_in_scope"]["in_scope"] is False
        assert gate_decision(evidence) == SELF_GATE_REJECT


class TestGateDecision:
    def test_all_pass_yields_approve(self, tmp_path: Path) -> None:
        evidence = gather_gate_evidence(_all_pass_inputs(tmp_path))
        assert gate_decision(evidence) == SELF_GATE_APPROVE

    def test_any_obligation_failure_yields_reject(self, tmp_path: Path) -> None:
        inputs = _all_pass_inputs(tmp_path)
        inputs.deleted_paths = ["tests/test_removed.py"]
        evidence = gather_gate_evidence(inputs)
        assert evidence["zero_test_deletion"]["zero"] is False
        assert gate_decision(evidence) == SELF_GATE_REJECT

    def test_a_missing_field_raises(self) -> None:
        import pytest as _pytest

        with _pytest.raises(GateEvidenceMissing):
            gate_decision({"acceptance_equality": {"equal": True}})


# ---------------------------------------------------------------------------
# the self-gate orchestration: gather -> decide -> deliver (spec item 1)
# ---------------------------------------------------------------------------


class TestPerformLineSelfGate:
    def test_gathers_decides_and_delivers(self, tmp_path: Path) -> None:
        casts: list[tuple[str, dict[str, Any]]] = []

        def deliver(decision: str, evidence: dict[str, Any]) -> dict[str, Any]:
            casts.append((decision, evidence))
            return {"status": "delivered", "decision": decision}

        result = perform_line_self_gate(
            development_id=DD_ID,
            principal=DISPATCHER,
            inputs=_all_pass_inputs(tmp_path),
            deliver=deliver,
        )
        assert isinstance(result, SelfGateResult)
        assert result.decision == SELF_GATE_APPROVE
        assert result.principal == DISPATCHER
        assert result.as_dict()["decided_by"] == DISPATCHER
        # The delivery received the mechanically-gathered evidence, all six fields.
        assert len(casts) == 1
        assert casts[0][0] == SELF_GATE_APPROVE
        assert set(casts[0][1].keys()) == set(GATE_EVIDENCE_FIELDS)
        assert result.delivery == {"status": "delivered", "decision": SELF_GATE_APPROVE}

    def test_a_failed_obligation_is_delivered_as_reject_not_crashed(self, tmp_path: Path) -> None:
        inputs = _all_pass_inputs(tmp_path)
        inputs.deleted_paths = ["tests/test_gone.py"]
        casts: list[tuple[str, dict[str, Any]]] = []

        result = perform_line_self_gate(
            development_id=DD_ID,
            principal=DISPATCHER,
            inputs=inputs,
            deliver=lambda decision, evidence: (
                casts.append((decision, evidence)) or {"status": "delivered", "decision": decision}
            ),
        )
        assert result.decision == SELF_GATE_REJECT
        assert casts[0][0] == SELF_GATE_REJECT


# ---------------------------------------------------------------------------
# the goal-line self-gate node is wired into the graph (no orphaned module)
# ---------------------------------------------------------------------------


class _MinimalArtifacts:
    def __init__(self) -> None:
        self.beats: list[tuple[int, str]] = []
        self.terminal: dict[str, Any] | None = None

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        self.beats.append((round_no, phase))
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        return True

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> str:
        return ""

    def write_terminal(self, **kwargs: Any) -> str:
        self.terminal = kwargs
        return ""


class _MinimalInbox:
    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


class _MinimalCoordinator:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def turn(self, round_no: int, coord_input: dict[str, Any], *, resume: bool = False) -> dict:
        self.calls.append(coord_input)
        return {"verdict": "done", "reason": "ok"}


class _MinimalWorker:
    def turn(self, prompt: str, round_no: int) -> dict:
        return {
            "schema_version": "fleet-graph.worker-turn-report/v1",
            "turn_id": "t",
            "outcome": "completed",
            "summary": "",
            "did": [],
            "files": [],
            "self_tests": [],
            "blocker": None,
        }


class _FakeSelfGatePort:
    def __init__(self, *, pending: bool, facts: dict[str, Any] | None = None) -> None:
        self.pending = pending
        self.facts = facts or {
            "development_id": DD_ID,
            "decision": SELF_GATE_APPROVE,
            "evidence": {field: {"ok": True} for field in GATE_EVIDENCE_FIELDS},
        }
        self.performed = 0

    def is_pending(self) -> bool:
        return self.pending

    def perform(self) -> dict[str, Any]:
        self.performed += 1
        return self.facts


def _build_graph_with_self_gate(port: Any) -> Any:
    from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
    from fleet_graph.graphs.guards import LineBounds, LineGuards

    deps = LineDeps(
        coordinator=_MinimalCoordinator(),  # type: ignore[arg-type]
        worker=_MinimalWorker(),  # type: ignore[arg-type]
        inbox=_MinimalInbox(),  # type: ignore[arg-type]
        artifacts=_MinimalArtifacts(),  # type: ignore[arg-type]
        guards=LineGuards(bounds=LineBounds()),
        folder_id=DISPATCHER,
        self_gate=port,
    )
    return build_goal_line_graph(deps), deps


class TestSelfGateNode:
    def test_a_pending_wake_runs_the_self_gate_turn(self) -> None:
        port = _FakeSelfGatePort(pending=True)
        graph, deps = _build_graph_with_self_gate(port)
        from langgraph.checkpoint.memory import InMemorySaver

        state = graph.compile(checkpointer=InMemorySaver()).invoke(
            {"round_no": 1},
            config={"configurable": {"thread_id": "t1"}, "recursion_limit": 100},
        )
        assert port.performed == 1
        assert state.get("last_self_gate", {}).get("performed") is True
        assert state["last_self_gate"]["decision"] == SELF_GATE_APPROVE
        # The facts reached the coordinator input too (engine facts, not prose).
        assert deps.coordinator.calls[0]["last_self_gate"]["decision"] == SELF_GATE_APPROVE  # type: ignore[attr-defined]

    def test_no_port_keeps_the_loop_byte_identical(self) -> None:
        graph, _deps = _build_graph_with_self_gate(None)
        from langgraph.checkpoint.memory import InMemorySaver

        state = graph.compile(checkpointer=InMemorySaver()).invoke(
            {"round_no": 1},
            config={"configurable": {"thread_id": "t2"}, "recursion_limit": 100},
        )
        assert "last_self_gate" not in state
        assert state.get("terminal") == "done"


# ---------------------------------------------------------------------------
# positive path: self-gate APPROVE -> merge 后收割触发 (spec item 5 / 判据)
# ---------------------------------------------------------------------------


class TestSelfGateApproveToHarvest:
    def test_self_gate_approve_is_not_harvestable_until_merge(self, tmp_path: Path) -> None:
        """自判 APPROVE → merge 后收割触发: a self-gate APPROVE (the positive
        verdict) must only become harvestable after the dd single reaches the
        merge stage -- a `complete` record still at the gate stage is never
        listed, while the same record at `merger` is (the read-model gating)."""
        from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateView

        casts: list[str] = []
        result = perform_line_self_gate(
            development_id=DD_ID,
            principal=DISPATCHER,
            inputs=_all_pass_inputs(tmp_path),
            deliver=lambda decision, evidence: (
                casts.append(decision) or {"status": "delivered", "decision": decision}
            ),
        )
        assert result.decision == SELF_GATE_APPROVE

        dd_root = tmp_path / "dd"
        dev_dir = dd_root / DD_ID
        dev_dir.mkdir(parents=True, exist_ok=True)
        (dev_dir / "record.json").write_text(
            json.dumps({"development_id": DD_ID, "repo_path": str(tmp_path)}), encoding="utf-8"
        )

        def write_status(*, terminal: str, stage: str) -> None:
            (dev_dir / "status.json").write_text(
                json.dumps(
                    {
                        "development_id": DD_ID,
                        "state": "complete",
                        "stage": stage,
                        "terminal": terminal,
                        "head_commit": "h1",
                    }
                ),
                encoding="utf-8",
            )

        config = FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=tmp_path / "runs",
            dd_root=dd_root,
            lines_config=tmp_path / "missing.json",
            bridge_state_dir=tmp_path / "bridge",
        )

        # A gate-stage complete (the self-gate just approved) is NOT harvestable.
        view = FleetStateView(config)
        assert view.harvestable()["developments"] == []

        write_status(terminal="complete", stage="human_gate")
        assert view.harvestable()["developments"] == []

        write_status(terminal="complete", stage="merger")
        assert [d["development_id"] for d in view.harvestable()["developments"]] == [DD_ID]


# ---------------------------------------------------------------------------
# the production port: the concrete SelfGatePort wired into build_line
# ---------------------------------------------------------------------------

PORT_SPEC = (
    "# SPEC: keep greet personalised\n\n"
    "Make greet(name) return a personalised greeting.\n\n"
    "```dd-acceptance\n"
    "python3 -c \"print('ok')\"\n"
    "```\n"
)


class _RecordingLauncher:
    """Stands in for TransientLauncher: nothing may really launch here, but a
    launch flips the unit probe active -- the same live fact the real
    rebuild_status consults after a resume."""

    dry_run = False

    def __init__(self) -> None:
        self.specs: list[Any] = []
        self.unit_active = False

    def launch(self, spec: Any) -> Any:
        from fleet_graph.scheduler.launcher import LaunchResult

        self.specs.append(spec)
        self.unit_active = True
        return LaunchResult(spec.unit_name, True, "recorded")


def _make_plane(tmp_path: Path) -> tuple[DdControlPlane, _RecordingLauncher]:
    binding = tmp_path / "plugin-binding.json"
    if not binding.exists():
        binding.write_text('{"plugin_producer": {}}', encoding="utf-8")
    launcher = _RecordingLauncher()
    plane = DdControlPlane(
        root=tmp_path / "dd",
        plugin_binding=binding,
        worktree_roots=(str(tmp_path),),
        working_directory=str(tmp_path),
        executable="/usr/local/bin/fleet-graph",
        launcher=launcher,
        unit_probe=lambda unit: launcher.unit_active,
        board_factory=lambda: None,
        clock=lambda: 1_700_000_000.0,
    )
    return plane, launcher


def _single(tmp_path: Path, *, dispatched_by: str = DISPATCHER) -> tuple[DdControlPlane, str, Path]:
    """One real dd single (admission + bootstrap) over a real git worktree."""
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "greet.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    bare = tmp_path / "origin.git"
    git(repo, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    plane, _launcher = _make_plane(tmp_path)
    dev = plane.create(str(repo), spec_text=PORT_SPEC, dispatched_by=dispatched_by)[
        "development_id"
    ]
    return plane, dev, repo


def _suspend_at_gate(plane: DdControlPlane, dev: str, repo: Path) -> None:
    """Land the runner's own result artifact for a generation suspended at the
    gate (the shape ``run_development`` writes) plus the durable checkpoint the
    gate resume requires, then derive the status from it -- status.json is a
    cache, never hand-written here."""
    dev_root = plane.root / dev
    write_json_durable(
        dev_root / "result.json",
        {
            "development_id": dev,
            "generation": 1,
            "terminal": None,
            "terminal_reason": "",
            "stage": "human_gate",
            "head_commit": head(repo),
            "awaiting": {"question_note_id": "q-1", "card_entity_id": "card-1"},
            "history": [],
            "written_at": "2026-09-03T00:00:00Z",
        },
    )
    (dev_root / CHECKPOINT_FILE).touch()
    plane.rebuild_status(dev)


def _resume_launches(plane: DdControlPlane, dev: str) -> list[dict[str, Any]]:
    """The generation's launch entries, read from the plane's own ledger."""
    entries = (plane.root / dev / "launches.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in entries if line.strip()]


def _write_acceptance_receipt(dev: str, repo: Path, commands: list[list[str]]) -> None:
    """The acceptance stage's own receipt artifact (AcceptanceStage's shape)."""
    receipt_dir = repo / ".dd-evidence"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / "acceptance.json").write_text(
        json.dumps(
            {
                "development_id": dev,
                "attempt": 1,
                "passed": True,
                "results": [
                    {
                        "command": command,
                        "exit_code": 0,
                        "stdout_tail": "ok",
                        "stderr_tail": "",
                    }
                    for command in commands
                ],
            },
        ),
        encoding="utf-8",
    )


def _green_suite(workspace: Path) -> SuiteSnapshot:
    return SuiteSnapshot(passed=106, failed=0, total=106, green_tests={"a"})


def _port(
    plane: DdControlPlane,
    dev: str,
    tmp_path: Path,
    *,
    declared_paths: tuple[str, ...] = (),
    line_id: str = DISPATCHER,
) -> DdGateSelfGatePort:
    """The production port with the *expensive* seams scripted: acceptance
    rerun, mutation acceptance and the two suite probes stand in for real
    subprocess runs; the mutation itself, the diff reads, the spec/receipt
    legs and the delivery are all real."""
    return DdGateSelfGatePort(
        line_id=line_id,
        development_id=dev,
        dd=plane,
        run_root=tmp_path / "runs",
        declared_paths=declared_paths,
        acceptance_runner=lambda argv: (0, "ok"),
        mutation_accept=lambda: 1,
        suite_probe=_green_suite,
        baseline_probe=lambda workspace, base: _green_suite(workspace),
    )


class TestDdGateSelfGatePort:
    def test_is_pending_tracks_the_live_gate_state(self, tmp_path: Path) -> None:
        plane, dev, repo = _single(tmp_path)
        port = _port(plane, dev, tmp_path)
        # Created but not yet at the gate: nothing to judge.
        assert port.is_pending() is False
        _suspend_at_gate(plane, dev, repo)
        assert port.is_pending() is True

    def test_a_probe_failure_is_never_pending(self, tmp_path: Path) -> None:
        class BrokenPlane:
            def get(self, development_id: str) -> dict[str, Any]:
                raise RuntimeError("dd root unreadable")

        assert (
            DdGateSelfGatePort(
                line_id=DISPATCHER,
                development_id=DD_ID,
                dd=BrokenPlane(),
                run_root=tmp_path / "runs",
            ).is_pending()
            is False
        )

    def test_perform_gathers_from_the_single_and_delivers_through_the_production_path(
        self, tmp_path: Path
    ) -> None:
        plane, dev, repo = _single(tmp_path)
        _suspend_at_gate(plane, dev, repo)
        record_argv = derive_acceptance_commands(PORT_SPEC.encode("utf-8"))
        _write_acceptance_receipt(dev, repo, record_argv)
        port = _port(plane, dev, tmp_path)

        facts = port.perform()

        assert facts["decided_by"] == DISPATCHER
        assert facts["decision"] == SELF_GATE_APPROVE
        # The delivery answer came from the *real* deliver_decision path: the
        # single was resumed through its gate (one resume launch entry) and
        # the dispatching line woken.
        assert facts["delivery"]["status"] == OUTCOME_DELIVERED
        assert facts["delivery"]["outcome"] == "consumed"
        entries = _resume_launches(plane, dev)
        assert [(e["mode"], e["started"]) for e in entries] == [("resume", True)]
        # The wake fact landed in the runs root's stall-state file.
        stall = json.loads((tmp_path / "runs" / ".scheduler" / f"{DISPATCHER}.json").read_text())
        assert stall["dispatched_decision_consumed_at"] is not None
        # The six obligations were gathered mechanically from the single's own
        # facts: the spec leg re-derived from the committed spec matches the
        # record leg and the receipt leg; the diff (bootstrap-only, machine
        # paths) is in scope; the regression is anchored at the frozen base.
        evidence = facts["evidence"]
        assert set(evidence.keys()) == set(GATE_EVIDENCE_FIELDS)
        assert evidence["acceptance_equality"]["equal"] is True
        assert evidence["acceptance_equality"]["spec_argv"] == record_argv
        assert evidence["diff_in_scope"]["in_scope"] is True
        assert evidence["zero_test_deletion"]["zero"] is True
        assert evidence["rerun_acceptance"]["rerun"] is True
        assert evidence["mutation"]["red"] is True
        # The mutation gun really fired on the single's product file -- and
        # restored it byte-exact.
        assert evidence["mutation"]["restored"] is True
        assert (repo / "greet.py").read_text(encoding="utf-8") == (
            'def greet():\n    return "hello"\n'
        )
        assert evidence["regression"]["pass"] is True
        record = json.loads((plane.root / dev / "record.json").read_text())
        assert evidence["regression"]["target_base_commit"] == record["target_base_commit"]
        assert evidence["regression"]["anchored"] is True

    def test_an_out_of_scope_product_change_is_delivered_as_reject(self, tmp_path: Path) -> None:
        plane, dev, repo = _single(tmp_path)
        # The single's work touched a product file nothing declared.
        (repo / "greet.py").write_text('def greet(name="x"):\n    return name\n', encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "work")
        _suspend_at_gate(plane, dev, repo)
        _write_acceptance_receipt(dev, repo, derive_acceptance_commands(PORT_SPEC.encode("utf-8")))
        port = _port(plane, dev, tmp_path, declared_paths=())

        facts = port.perform()

        assert facts["decision"] == SELF_GATE_REJECT
        assert facts["evidence"]["diff_in_scope"]["in_scope"] is False
        assert "greet.py" in facts["evidence"]["diff_in_scope"]["out_of_scope"][0]
        # A REJECT riding failing evidence is consistent: the gate consumed a
        # refusal, not an approval.
        assert facts["delivery"]["status"] == OUTCOME_DELIVERED
        assert facts["delivery"]["decision"] == SELF_GATE_REJECT

    def test_an_unreadable_receipt_fails_closed_as_reject(self, tmp_path: Path) -> None:
        plane, dev, repo = _single(tmp_path)
        _suspend_at_gate(plane, dev, repo)
        # No .dd-evidence/acceptance.json: the receipt leg cannot be produced.
        port = _port(plane, dev, tmp_path)

        facts = port.perform()

        assert facts["decision"] == SELF_GATE_REJECT
        assert facts["evidence"]["acceptance_equality"]["equal"] is False
        assert any("receipt" in note for note in facts["gather_notes"])

    def test_the_drifted_main_is_recorded_and_never_consulted(self, tmp_path: Path) -> None:
        plane, dev, repo = _single(tmp_path)
        _suspend_at_gate(plane, dev, repo)
        _write_acceptance_receipt(dev, repo, derive_acceptance_commands(PORT_SPEC.encode("utf-8")))
        # main has drifted past the frozen target base while the single ran.
        (repo / "drift.md").write_text("drift\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main drifted on")
        port = _port(plane, dev, tmp_path)

        facts = port.perform()

        assert facts["decision"] == SELF_GATE_APPROVE
        record = json.loads((plane.root / dev / "record.json").read_text())
        regression = facts["evidence"]["regression"]
        assert regression["target_base_commit"] == record["target_base_commit"]
        assert regression["ignored_main_head_commit"] == head(repo)
        assert regression["pass"] is True


class TestBuildLineWiresTheProductionSelfGate:
    def _config(self, tmp_path: Path, **overrides: Any) -> Any:
        from fleet_graph.graphs.runner import LineConfig

        defaults: dict[str, Any] = {
            "folder_id": DISPATCHER,
            "seat": "s",
            "run_root": tmp_path / "line",
        }
        defaults.update(overrides)
        return LineConfig(**defaults)

    def test_a_dd_anchor_wires_the_concrete_port(self, tmp_path: Path, monkeypatch: Any) -> None:
        from fleet_graph.graphs import self_gate_port as port_module
        from fleet_graph.graphs.runner import build_line

        seen: dict[str, Any] = {}

        class FakePort:
            def __init__(self) -> None:
                self.pending_calls = 0

            def is_pending(self) -> bool:
                self.pending_calls += 1
                return True

            def perform(self) -> dict[str, Any]:
                return {}

        def fake_build(**kwargs: Any) -> FakePort:
            seen.update(kwargs)
            return FakePort()

        monkeypatch.setattr(port_module, "build_line_self_gate", fake_build)
        config = self._config(
            tmp_path,
            prior_terminal={
                "terminal": "blocked",
                "waiting_on": "dd",
                "dd_development_id": "dev-fg-anchor",
            },
        )
        _graph, deps = build_line(config)
        assert isinstance(deps.self_gate, FakePort)
        assert seen["line_id"] == DISPATCHER
        assert seen["development_id"] == "dev-fg-anchor"
        # The wake fact must land in the *runs root's* stall file.
        assert seen["run_root"] == config.run_root.parent
        assert deps.self_gate.pending_calls == 0

    def test_no_anchor_keeps_the_loop_unwired(self, tmp_path: Path) -> None:
        from fleet_graph.graphs.runner import build_line

        _graph, deps = build_line(self._config(tmp_path))
        assert deps.self_gate is None

    def test_an_explicit_anchor_wins_over_the_prior_terminal(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from fleet_graph.graphs import self_gate_port as port_module
        from fleet_graph.graphs.runner import build_line

        seen: dict[str, Any] = {}

        def fake_build(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return object()

        monkeypatch.setattr(port_module, "build_line_self_gate", fake_build)
        config = self._config(
            tmp_path,
            self_gate_development_id="dev-fg-explicit",
            prior_terminal={
                "terminal": "blocked",
                "waiting_on": "dd",
                "dd_development_id": "dev-fg-stale",
            },
        )
        _graph, deps = build_line(config)
        assert deps.self_gate is not None
        assert seen["development_id"] == "dev-fg-explicit"

    def test_the_real_factory_builds_the_real_port(self, tmp_path: Path) -> None:
        """No fake: the wiring point hands a real dd anchor to the real
        factory and gets the concrete DdGateSelfGatePort back."""
        from fleet_graph.graphs.runner import build_line

        config = self._config(
            tmp_path,
            prior_terminal={
                "terminal": "blocked",
                "waiting_on": "dd",
                "dd_development_id": "dev-fg-nowhere",
            },
        )
        _graph, deps = build_line(config)
        assert isinstance(deps.self_gate, DdGateSelfGatePort)
        assert deps.self_gate.development_id == "dev-fg-nowhere"
        # The anchor points nowhere on this machine: fail-soft not-pending.
        assert deps.self_gate.is_pending() is False


class TestSelfGateApproveFlowsThroughMergeToHarvestable:
    """rc-b79ebd77 major 3: the positive path exercised end-to-end over the
    production machinery -- the port gathers from the single's own artifacts,
    the *real* deliver_decision consumes the gate, the runner's own result
    artifact lands the merge stage, the control plane derives the status, and
    the read model lists the development. No status.json is hand-written."""

    def test_self_gate_approve_flows_through_merge_into_harvestable(self, tmp_path: Path) -> None:
        from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateView

        plane, dev, repo = _single(tmp_path)
        record_argv = derive_acceptance_commands(PORT_SPEC.encode("utf-8"))
        _write_acceptance_receipt(dev, repo, record_argv)
        _suspend_at_gate(plane, dev, repo)
        port = _port(plane, dev, tmp_path)
        assert port.is_pending() is True

        # The gate turn: gather -> decide -> deliver through the real path.
        facts = port.perform()
        assert facts["decision"] == SELF_GATE_APPROVE
        assert facts["delivery"]["status"] == OUTCOME_DELIVERED
        assert facts["delivery"]["outcome"] == "consumed"
        assert facts["delivery"]["evidence"]["digest"].startswith("sha256:")
        assert [(e["mode"], e["started"]) for e in _resume_launches(plane, dev)] == [
            ("resume", True)
        ]

        config = FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=tmp_path / "runs",
            dd_root=plane.root,
            lines_config=tmp_path / "missing.json",
            bridge_state_dir=tmp_path / "bridge",
        )
        view = FleetStateView(config)
        # Consumed but pre-merge: the running single is never harvestable.
        assert view.harvestable()["developments"] == []

        # The pipeline's merge stage completes: the runner writes its result
        # artifact, the control plane derives the status from it.
        write_json_durable(
            plane.root / dev / "result.json",
            {
                "development_id": dev,
                "generation": 1,
                "terminal": "complete",
                "terminal_reason": "merger is the last declared stage",
                "stage": "merger",
                "head_commit": head(repo),
                "awaiting": None,
                "history": [],
                "written_at": "2026-09-03T00:01:00Z",
            },
        )
        status = plane.get(dev)
        assert status["terminal"] == "complete"
        assert status["stage"] == "merger"

        # Post-merge, the approved self-gate verdict is harvestable.
        assert [d["development_id"] for d in view.harvestable()["developments"]] == [dev]
