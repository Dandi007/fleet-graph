"""M2 dd-gate delivery: ``decision_deliver`` accepts a dd single target.

The spec (wf-8d9737 M2) extends the decision MCP surface so the *same* tool
that wakes a parked line can also deliver ``APPROVE``/``REJECT`` to a dd single
sitting at ``awaiting_gate`` -- resuming the single through its existing gate
path and waking the dispatching line synchronously. One authority check, one
unchanged vocabulary:

- **positive** -- an ``awaiting_gate`` single delivered by its dispatching
  principal resumes, and the dispatching line's ``parked_*`` snapshot is cleared
  while the ``dispatched_decision_consumed_at`` wake fact lands (the scheduler
  wakes it next tick); the new path never produces a "swallowed" entry.
- **principal == dispatched_by** -- any other principal is refused with
  ``NOT_DISPATCHING_LINE`` and BOTH the single and the dispatching line stay
  untouched.
- **MAYBE stays negative** -- a decision outside {APPROVE, REJECT} is still a
  call-point :class:`DecisionPayloadError`, exactly as before.

The resume is driven against a fake dd control plane (the same duck-type the
dd MCP surface and its tests use), but the *line wake* is the real stall-state
file write the scheduler reads -- no mock there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.decision_bridge.owners import OWNER_KIND_DD
from fleet_graph.decision_mcp import (
    CODE_DD_NOT_AWAITING_GATE,
    CODE_NOT_DISPATCHING_LINE,
    DECISION_APPROVE,
    DECISION_REJECT,
    OUTCOME_DELIVERED,
    OUTCOME_REFUSED,
    DecisionPayloadError,
    DeliveryResult,
    deliver_decision,
)
from fleet_graph.selfgate import GATE_EVIDENCE_FIELDS

COMPLETE_EVIDENCE: dict[str, Any] = {field: {"ok": True} for field in GATE_EVIDENCE_FIELDS}

#: Evidence whose six recorded answers genuinely pass (the payload
#: ``gather_gate_evidence`` produces on a green single). The delivery path
#: runs ``gate_decision`` over the six answers, so an APPROVE delivery must
#: ride answers that support it.
M2_ACCEPTANCE_ARGV = [["uv", "run", "pytest", "-q", "tests/test_m2_dd_gate_delivery.py"]]
PASSING_EVIDENCE: dict[str, Any] = {
    "acceptance_equality": {
        "equal": True,
        "spec_argv": M2_ACCEPTANCE_ARGV,
        "record_argv": M2_ACCEPTANCE_ARGV,
        "receipt_argv": M2_ACCEPTANCE_ARGV,
    },
    "diff_in_scope": {"in_scope": True, "changed": [], "declared": [], "out_of_scope": []},
    "zero_test_deletion": {"zero": True, "deleted_tests": [], "all_deleted": []},
    "rerun_acceptance": {
        "rerun": True,
        "commands": [{"argv": M2_ACCEPTANCE_ARGV, "exit_code": 0, "output": "ok"}],
    },
    "mutation": {"two_shots": True, "red": True, "restored": True, "shots": []},
    "regression": {"pass": True, "red_set_grew": False, "green_to_red_flip": False},
}

DD_ID = "dev-fg-abc"
DISPATCHER = "wf-1"

ROSTER: list[Any] = [{"folder_id": "wf-1", "seat": "s", "generation": 2}]


class FakeDdPlane:
    """A duck-typed dd control plane: ``get`` + ``gate`` only."""

    def __init__(
        self,
        *,
        state: str = "awaiting_gate",
        dispatched_by: str = DISPATCHER,
        generation: int = 2,
        awaiting: dict[str, str] | None = None,
    ) -> None:
        self.state = state
        self.dispatched_by = dispatched_by
        self.generation = generation
        self.awaiting = awaiting or {
            "question_note_id": "q-dd-1",
            "card_entity_id": "card-dd-1",
        }
        self.resumed: list[tuple[str, str]] = []

    def get(self, development_id: str) -> dict[str, Any]:
        return {
            "development_id": development_id,
            "state": self.state,
            "dispatched_by": self.dispatched_by,
            "generation": self.generation,
            "awaiting": self.awaiting,
        }

    def gate(
        self, development_id: str, resume: bool = False, action_key: str | None = None
    ) -> dict[str, Any]:
        assert resume is True
        self.resumed.append((development_id, action_key or ""))
        # M3 S10: a resume means "the unit started and the single left the
        # gate" -- reflect consumption in the re-read a delivery performs.
        if self.state == "awaiting_gate":
            self.state = "running"
        return {
            "state": self.state,
            "development_id": development_id,
            "resume": {"development_id": development_id, "generation": self.generation},
        }


def _stall(run_root: Path, folder_id: str = DISPATCHER) -> Path:
    path = run_root / ".scheduler" / f"{folder_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generation": 2,
                "park_considered_run_id": "run-1",
                "parked_run_id": "run-1",
                "parked_at": 1_700_000_000.0,
                "parked_goal_revision": "sha256:consumed",
                "parked_inbox_available": True,
                "parked_dd_development_id": DD_ID,
            }
        ),
        encoding="utf-8",
    )
    return path


def _read_stall(run_root: Path, folder_id: str = DISPATCHER) -> dict[str, Any]:
    return json.loads((run_root / ".scheduler" / f"{folder_id}.json").read_text(encoding="utf-8"))


def _call(
    run_root: Path,
    plane: FakeDdPlane,
    *,
    line: str = DD_ID,
    decision: str = DECISION_APPROVE,
    principal: str = DISPATCHER,
) -> DeliveryResult:
    # The delivery path enforces the evidence verdict (M3): passing answers
    # for an APPROVE; key-present answers that decide REJECT for a REJECT.
    evidence = PASSING_EVIDENCE if decision == DECISION_APPROVE else COMPLETE_EVIDENCE
    return deliver_decision(
        line=line,
        decision=decision,
        reason="live drill",
        principal=principal,
        run_root=run_root,
        lines=ROSTER,
        dd=plane,
        clock=lambda: 1_700_000_123.0,
        evidence=evidence,
    )


class TestPositiveDdDelivery:
    def test_approve_resumes_the_single_and_wakes_the_dispatching_line(
        self, tmp_path: Path
    ) -> None:
        plane = FakeDdPlane()
        _stall(tmp_path)

        result = _call(tmp_path, plane, decision=DECISION_APPROVE)

        assert result.status == OUTCOME_DELIVERED
        assert result.as_dict()["outcome"] == "consumed"
        assert result.target is not None
        assert result.target["kind"] == OWNER_KIND_DD
        assert result.target["id"] == DD_ID
        assert result.target["generation"] == 2
        assert result.target["question_note_id"] == "q-dd-1"
        assert result.target["card_entity_id"] == "card-dd-1"
        assert result.action_key == f"mcp:dd:{DD_ID}:g2:APPROVE"

        # The single was resumed through its gate, once, with the durable key.
        assert plane.resumed == [(DD_ID, f"mcp:dd:{DD_ID}:g2:APPROVE")]

        # The dispatching line is woken: parked_* cleared, anti-swallow marker
        # preserved, and the wake fact written.
        after = _read_stall(tmp_path)
        assert after["parked_run_id"] is None
        assert after["parked_at"] is None
        assert after["parked_goal_revision"] is None
        assert after["parked_dd_development_id"] is None
        assert after["park_considered_run_id"] == "run-1"
        assert after["dispatched_decision_consumed_at"] == 1_700_000_123.0

    def test_reject_is_also_a_delivered_verdict_with_a_distinct_action_key(
        self, tmp_path: Path
    ) -> None:
        plane = FakeDdPlane()
        _stall(tmp_path)

        result = _call(tmp_path, plane, decision=DECISION_REJECT)

        assert result.status == OUTCOME_DELIVERED
        assert result.decision == DECISION_REJECT
        assert result.action_key == f"mcp:dd:{DD_ID}:g2:REJECT"
        assert plane.resumed == [(DD_ID, f"mcp:dd:{DD_ID}:g2:REJECT")]

    def test_the_new_path_is_zero_swallowed(self, tmp_path: Path) -> None:
        """A dd delivery is never a third 'swallowed' state: it is delivered or
        refused, both of which name their consumption/refusal."""
        plane = FakeDdPlane()
        _stall(tmp_path)

        delivered = _call(tmp_path, plane)
        refused = _call(tmp_path, plane, principal="wf-other")

        assert delivered.status == OUTCOME_DELIVERED
        assert refused.status == OUTCOME_REFUSED
        assert refused.code == CODE_NOT_DISPATCHING_LINE
        for result in (delivered, refused):
            assert result.status in {OUTCOME_DELIVERED, OUTCOME_REFUSED}


class TestPrincipalIsDispatchingLine:
    def test_a_non_dispatching_principal_is_refused_and_nothing_moves(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        stall = _stall(tmp_path)
        before = json.loads(stall.read_text(encoding="utf-8"))

        result = _call(tmp_path, plane, principal="wf-other")

        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NOT_DISPATCHING_LINE
        assert "wf-other" in result.message
        assert "wf-1" in result.message

        # The single was NOT resumed and the dispatching line is untouched.
        assert plane.resumed == []
        assert _read_stall(tmp_path) == before

    def test_an_empty_principal_is_not_the_dispatching_line(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        _stall(tmp_path)

        result = _call(tmp_path, plane, principal="")

        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NOT_DISPATCHING_LINE
        assert plane.resumed == []

    def test_the_principal_must_equal_the_record_dispatched_by(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        _stall(tmp_path)
        assert _call(tmp_path, plane, principal=DISPATCHER).status == OUTCOME_DELIVERED
        assert _call(tmp_path, plane, principal="wf-1-extra").code == CODE_NOT_DISPATCHING_LINE


class TestDdNotAtGate:
    def test_a_single_not_awaiting_gate_is_an_explicit_refusal(self, tmp_path: Path) -> None:
        plane = FakeDdPlane(state="running")
        stall = _stall(tmp_path)
        before = json.loads(stall.read_text(encoding="utf-8"))

        result = _call(tmp_path, plane)

        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_NOT_AWAITING_GATE
        assert result.retryable is True
        assert "running" in result.message
        assert plane.resumed == []
        assert _read_stall(tmp_path) == before


class TestMayBeStaysNegative:
    def test_maybe_is_still_a_call_point_error_for_a_dd_target(self, tmp_path: Path) -> None:
        plane = FakeDdPlane()
        _stall(tmp_path)

        with pytest.raises(DecisionPayloadError, match="APPROVE or REJECT"):
            _call(tmp_path, plane, decision="MAYBE")

        assert plane.resumed == []


class TestLinePathUnaffected:
    def test_a_non_dd_target_still_routes_to_the_parked_line_path(self, tmp_path: Path) -> None:
        """The human/supervisor path to lines is unchanged: a ``wf-*`` target
        ignores ``principal`` and the dd plane entirely."""
        plane = FakeDdPlane()
        stall = _stall(tmp_path)
        stall.write_text(
            json.dumps(
                {
                    "generation": 2,
                    "board_question_note_id": "q-1",
                    "board_card_entity_id": "card-1",
                    "parked_run_id": "run-1",
                    "parked_at": 1_700_000_000.0,
                    "parked_goal_revision": "sha256:consumed",
                    "parked_inbox_available": True,
                }
            ),
            encoding="utf-8",
        )

        result = _call(tmp_path, plane, line="wf-1", principal="wf-other")

        assert result.status == OUTCOME_DELIVERED
        assert result.target["kind"] == "line"
        assert plane.resumed == []
        after = _read_stall(tmp_path)
        assert after["parked_run_id"] is None
        assert (
            "dispatched_decision_consumed_at" not in after
            or after["dispatched_decision_consumed_at"] is None
        )
