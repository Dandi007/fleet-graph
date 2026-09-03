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

from pathlib import Path
from typing import Any

import pytest

from fleet_graph.decision_mcp import (
    CODE_DD_NOT_CONSUMED,
    CODE_DD_NOT_FOUND,
    CODE_DD_WORKSPACE_MISSING,
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
from fleet_graph.selfgate import (
    GATE_EVIDENCE_FIELDS,
    GateEvidenceMissing,
    SuiteSnapshot,
    acceptance_equality,
    diff_in_scope,
    gate_evidence_payload,
    missing_gate_evidence,
    regression_verdict,
    require_gate_evidence,
    two_shot_mutation_gun,
    zero_test_deletion,
)

DD_ID = "dev-fg-36c2d76baca7"
DISPATCHER = "wf-8d9737"
ROSTER: list[Any] = [{"folder_id": "wf-8d9737", "seat": "s", "generation": 1}]

#: A complete six-field evidence payload the self-gate delivery is bound to.
#: The six obligations are the gate's mandatory answer fields; the mechanism
#: tests pass this so the delivery reaches the S10/S11 mechanics being drilled.
COMPLETE_EVIDENCE: dict[str, Any] = {field: {"ok": True} for field in GATE_EVIDENCE_FIELDS}


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
        evidence=evidence if evidence is not None else COMPLETE_EVIDENCE,
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

    def test_reject_is_still_a_consumed_verdict(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, decision=DECISION_REJECT)
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
        result = _deliver(plane, tmp_path, evidence=COMPLETE_EVIDENCE)
        assert result.status == OUTCOME_DELIVERED
        # The rationale (digest + field attestation) rides the result, keyed to
        # exactly the six fields -- the machine check that the gate's 必答字段
        # travelled with the verdict (spec item 4).
        assert result.evidence == gate_evidence_payload(COMPLETE_EVIDENCE)
        assert result.evidence["fields"] == {field: True for field in GATE_EVIDENCE_FIELDS}
        assert result.evidence["digest"].startswith("sha256:")
        assert result.as_dict()["evidence"] == result.evidence

    def test_the_ledger_entry_embeds_the_evidence_digest(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        result = _deliver(plane, tmp_path, evidence=COMPLETE_EVIDENCE)
        assert result.status == OUTCOME_DELIVERED
        ledger = DeliveryLedger(state_dir=tmp_path / "ledger")
        ledger.record(result)
        entry = ledger.entries()[0]
        assert entry["status"] == OUTCOME_DELIVERED
        assert entry["evidence"] == result.evidence
        assert entry["evidence"]["digest"] == gate_evidence_payload(COMPLETE_EVIDENCE)["digest"]


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
