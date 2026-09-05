"""dd.gate_release.v1 consumption: the gate node is the sole release path.

The spec (wf-4601c8 R3) deletes the second delivery path: ``decision_deliver``
no longer accepts a dd target and the decision-bridge no longer recovers dd
singles. Every test that was bound to that old path is deleted; this module now
holds the *equivalents bound to the new path* -- the dispatching line's own
graph gate node consuming its own ``dd.gate_release.v1`` action
(:class:`fleet_graph.graphs.dd_gate.GraphGateNode`). One authority check, one
unchanged vocabulary:

删/补对照表（旧路用例 → 新路等价用例；覆盖净数不减，另见
tests/test_r3_stop_response.py 的信封与 nodes 级用例）:

==============================  ==============================================
旧路（已删除，decision_deliver dd 目标路径）        新路等价（本文件，dd.gate_release.v1 消费）
==============================  ==============================================
test_approve_resumes_the_       test_approve_release_consumes_and_receipts
single_and_wakes_the_
dispatching_line
test_reject_is_also_a_          test_reject_release_with_board_binding_consumes
delivered_verdict_with_a_
distinct_action_key
test_the_new_path_is_zero_      test_consumption_is_never_a_third_state
swallowed
test_a_non_dispatching_         test_foreign_decider_is_refused_and_single_
principal_is_refused_and_       untouched
nothing_moves
test_an_empty_principal_is_     test_empty_decider_is_a_schema_refusal
not_the_dispatching_line
test_the_principal_must_equal_  test_decider_must_equal_record_dispatched_by
the_record_dispatched_by
test_a_single_not_awaiting_     test_single_not_awaiting_gate_is_a_failed_
gate_is_an_explicit_refusal     receipt
test_maybe_is_still_a_call_     test_invalid_verdict_is_a_failed_receipt
point_error_for_a_dd_target
test_a_non_dd_target_still_     （parked-line 外部裁决投递路保留——等价断言在
routes_to_the_parked_line_path  tests/test_decision_mcp.py 既有 line-path 用例，
                                本单不删不改其语义）
==============================  ==============================================

The plane is the duck-typed control plane the gate node holds; the decision
seal is a real git commit in a real throwaway workspace -- no mock there.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.dd.self_gate import EvidenceItem
from fleet_graph.graphs.dd_gate import (
    CODE_NOT_AWAITING_GATE,
    CODE_NOT_DISPATCHER,
    CODE_OBLIGATIONS_FAILED,
    CODE_REJECT_CONTRACT_INCOMPLETE,
    GraphGateNode,
)
from fleet_graph.graphs.stop_response import (
    KIND_GATE_RELEASE,
    REASON_PAYLOAD_SCHEMA,
    STATUS_CONSUMED,
    STATUS_FAILED,
)

DEV_ID = "dev-fg-abc"
DISPATCHER = "wf-1"
GATE_PATH_PARTS = (".dev-dispatch", "gate", "decision-g2.json")


def _passing_evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(obligation, obligation, True, "grounded by fixture")
        for obligation in (
            "acceptance_frozen",
            "diff_within_scope",
            "zero_test_deletion",
            "personally_rerun",
            "mutation_receipt",
            "regression",
        )
    ]


_WORKSPACE_SEQ = 0


def _workspace(tmp_path: Path) -> Path:
    global _WORKSPACE_SEQ
    _WORKSPACE_SEQ += 1
    workspace = tmp_path / f"subject-{_WORKSPACE_SEQ}"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
        check=True,
    )
    return workspace


class FakeGatePlane:
    """The duck-typed control plane the gate node holds: get / publish / resume."""

    def __init__(
        self,
        *,
        workspace: Path,
        state: str = "awaiting_gate",
        dispatched_by: str = DISPATCHER,
        generation: int = 2,
    ) -> None:
        self.workspace = workspace
        self.state = state
        self.dispatched_by = dispatched_by
        self.generation = generation
        self.resumed: list[tuple[str, str]] = []
        self.published: list[dict[str, Any]] = []

    def get(self, development_id: str) -> dict[str, Any]:
        return {
            "development_id": development_id,
            "state": self.state,
            "dispatched_by": self.dispatched_by,
            "generation": self.generation,
            "repo_path": str(self.workspace),
            "worktree_path": str(self.workspace),
        }

    def publish_gate_decision(
        self,
        development_id: str,
        *,
        decision: str,
        decided_by: str,
        reason: str = "",
        action_key: str = "",
    ) -> dict[str, Any]:
        self.published.append(
            {
                "development_id": development_id,
                "decision": decision,
                "decided_by": decided_by,
                "reason": reason,
                "action_key": action_key,
            }
        )
        return {"development_id": development_id, "decision": decision, "message_id": "m-1"}

    def gate(
        self, development_id: str, resume: bool = False, action_key: str | None = None
    ) -> dict[str, Any]:
        assert resume is True
        self.resumed.append((development_id, action_key or ""))
        self.state = "refused" if (action_key or "").endswith(":REJECT") else "running"
        return {
            "development_id": development_id,
            "resume": {
                "development_id": development_id,
                "generation": self.generation,
                "unit": f"fleet-graph-dd-{development_id}-r1",
                "mode": "resume",
            },
        }


def _action(
    *,
    verdict: str = "APPROVE",
    decided_by: str = DISPATCHER,
    key: str = "k1",
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "development_id": DEV_ID,
        "verdict": verdict,
        "decided_by": decided_by,
    }
    payload.update(payload_extra or {})
    return {"kind": KIND_GATE_RELEASE, "payload": payload, "idempotency_key": key}


def _node(plane: FakeGatePlane, evidence: Any = None) -> GraphGateNode:
    return GraphGateNode(plane, evidence=evidence)


def _consume(tmp_path: Path, plane: FakeGatePlane, action: dict[str, Any], **kwargs: Any):
    node = _node(plane, evidence=kwargs.pop("evidence", _passing_evidence()))
    return node.consume(action, folder_id=DISPATCHER, round_no=3)


def test_approve_release_consumes_and_receipts(tmp_path: Path) -> None:
    """The APPROVE release is consumed end to end: the verdict is sealed into
    the subject workspace by the node itself (decided_by recorded there), the
    read model gets the publish, and the suspended pipeline is resumed. S10:
    the receipt itself is the consumption evidence."""
    workspace = _workspace(tmp_path)
    plane = FakeGatePlane(workspace=workspace)

    receipt = _consume(tmp_path, plane, _action())

    assert receipt["status"] == STATUS_CONSUMED
    assert receipt["development_id"] == DEV_ID
    assert receipt["decision"] == "APPROVE"
    assert receipt["decided_by"] == DISPATCHER
    assert receipt["decided_by_source"] == "graph-gate-node"
    assert receipt["decision_message_id"] == "m-1"
    assert receipt["launches"]["unit"] == f"fleet-graph-dd-{DEV_ID}-r1"
    assert receipt["launches"]["generation"] == 2

    decision_file = workspace.joinpath(*GATE_PATH_PARTS)
    assert decision_file.is_file()
    import json

    sealed = json.loads(decision_file.read_text(encoding="utf-8"))
    assert sealed["decision"] == "APPROVE"
    assert sealed["decided_by"] == DISPATCHER
    assert sealed["development_id"] == DEV_ID

    assert plane.published == [
        {
            "development_id": DEV_ID,
            "decision": "APPROVE",
            "decided_by": DISPATCHER,
            "reason": plane.published[0]["reason"],
            "action_key": plane.resumed[0][1],
        }
    ]
    assert "acceptance_frozen=PASS" in plane.published[0]["reason"]
    assert plane.resumed == [(DEV_ID, plane.published[0]["action_key"])]


def test_reject_release_with_board_binding_consumes(tmp_path: Path) -> None:
    """A REJECT bound to the board adjudication's three non-empty fields (⑮)
    is consumed and its action key stays distinct."""
    workspace = _workspace(tmp_path)
    plane = FakeGatePlane(workspace=workspace)
    action = _action(
        verdict="REJECT",
        key="k-reject",
        payload_extra={
            "board_decision": {
                "problem": "the acceptance surface is wrong",
                "suggested_answer": "rework with the frozen argv extended",
                "cost_of_no_answer": "the single hangs at the gate unjudged",
            }
        },
    )

    receipt = _consume(tmp_path, plane, action)

    assert receipt["status"] == STATUS_CONSUMED
    assert receipt["decision"] == "REJECT"
    assert plane.published[0]["decision"] == "REJECT"
    assert plane.resumed == [(DEV_ID, plane.published[0]["action_key"])]


def test_a_reject_missing_any_board_field_is_refused_and_traced(tmp_path: Path) -> None:
    """⑮: a REJECT without all three non-empty adjudication fields is refused
    by the gate and traced; the single stays at awaiting_gate untouched."""
    workspace = _workspace(tmp_path)
    plane = FakeGatePlane(workspace=workspace)
    action = _action(
        verdict="REJECT",
        payload_extra={"board_decision": {"problem": "only the problem"}},
    )

    receipt = _consume(tmp_path, plane, action)

    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == CODE_REJECT_CONTRACT_INCOMPLETE
    assert plane.published == []
    assert plane.resumed == []
    assert plane.state == "awaiting_gate"


def test_consumption_is_never_a_third_state(tmp_path: Path) -> None:
    """A gate release is consumed or failed with its reason -- never a silent
    third state."""
    workspace = _workspace(tmp_path)
    plane = FakeGatePlane(workspace=workspace)

    consumed = _consume(tmp_path, plane, _action(key="ok"))
    failed = _consume(tmp_path, plane, _action(key="bad", decided_by="wf-other"))

    assert consumed["status"] == STATUS_CONSUMED
    assert failed["status"] == STATUS_FAILED
    for receipt in (consumed, failed):
        assert receipt["status"] in {STATUS_CONSUMED, STATUS_FAILED}


def test_foreign_decider_is_refused_and_single_untouched(tmp_path: Path) -> None:
    """The M2 identity invariant on the new path: decided_by != dispatched_by
    is refused before anything moves (REJECT + 留痕)."""
    workspace = _workspace(tmp_path)
    plane = FakeGatePlane(workspace=workspace)

    receipt = _consume(tmp_path, plane, _action(decided_by="wf-1-extra"))

    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == CODE_NOT_DISPATCHER
    assert plane.published == []
    assert plane.resumed == []
    assert plane.state == "awaiting_gate"


def test_empty_decider_is_a_schema_refusal(tmp_path: Path) -> None:
    """An empty decided_by never reaches the identity check: it is a payload
    schema refusal at the node boundary."""
    plane = FakeGatePlane(workspace=_workspace(tmp_path))

    receipt = _consume(tmp_path, plane, _action(decided_by=""))

    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == REASON_PAYLOAD_SCHEMA
    assert plane.resumed == []


def test_decider_must_equal_record_dispatched_by(tmp_path: Path) -> None:
    """Exactly the dispatcher passes; any other identity -- however close --
    fails the identity invariant."""
    ok = _consume(tmp_path, FakeGatePlane(workspace=_workspace(tmp_path)), _action(key="ok"))
    near_miss = _consume(
        tmp_path,
        FakeGatePlane(workspace=_workspace(tmp_path)),
        _action(key="near", decided_by="wf-1x"),
    )

    assert ok["status"] == STATUS_CONSUMED
    assert near_miss["reason"] == CODE_NOT_DISPATCHER


def test_single_not_awaiting_gate_is_a_failed_receipt(tmp_path: Path) -> None:
    """A single anywhere other than awaiting_gate is an explicit failed
    receipt; nothing is published or resumed."""
    plane = FakeGatePlane(workspace=_workspace(tmp_path), state="running")

    receipt = _consume(tmp_path, plane, _action())

    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == CODE_NOT_AWAITING_GATE
    assert "running" in receipt["detail"]
    assert plane.resumed == []


def test_invalid_verdict_is_a_failed_receipt(tmp_path: Path) -> None:
    """The verdict vocabulary is closed on the new path too: MAYBE is a
    payload schema refusal, never an interpretation."""
    plane = FakeGatePlane(workspace=_workspace(tmp_path))

    receipt = _consume(tmp_path, plane, _action(verdict="MAYBE"))

    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == REASON_PAYLOAD_SCHEMA
    assert plane.resumed == []


def test_a_failed_obligation_refuses_the_release(tmp_path: Path) -> None:
    """Any of the six obligations failing refuses the release with the
    per-item rationale in the receipt; the single stays untouched."""
    workspace = _workspace(tmp_path)
    plane = FakeGatePlane(workspace=workspace)
    evidence = _passing_evidence()
    evidence[3] = EvidenceItem("personally_rerun", "personally reran acceptance", False, "exit=1")

    receipt = _consume(tmp_path, plane, _action(), evidence=evidence)

    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == CODE_OBLIGATIONS_FAILED
    assert any(
        item["id"] == "personally_rerun" and not item["passed"] for item in receipt["evidence"]
    )
    assert plane.published == []
    assert plane.resumed == []


def test_unknown_development_is_a_failed_receipt(tmp_path: Path) -> None:
    """A single the control plane cannot resolve is refused, never guessed."""

    class Missing(FakeGatePlane):
        def get(self, development_id: str) -> dict[str, Any]:
            raise RuntimeError("DEVELOPMENT_NOT_FOUND")

    plane = Missing(workspace=_workspace(tmp_path))

    receipt = _consume(tmp_path, plane, _action())

    assert receipt["status"] == STATUS_FAILED
    assert plane.resumed == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
