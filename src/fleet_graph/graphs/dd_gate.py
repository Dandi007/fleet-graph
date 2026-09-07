"""Goal 的审单节点：消费 dd.gate_release.v1，按冻结 policy 校验证据。

调用身份必须与入单 dispatcher 一致。APPROVE 要求全部义务通过；REJECT
必须携带问题、建议答案和不回答的代价。整次操作持开发单锁，先发布幂等裁决，
再封存其 ID 与证据，最后恢复 pipeline；未完整封存的裁决不可自动恢复。
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Protocol

from fleet_graph.dd.git import run_git
from fleet_graph.dd.self_gate import (
    EvidenceItem,
    collect_evidence,
    render_rationale,
)
from fleet_graph.dd.self_gate_evidence import DEFAULT_DD_ROOT, collect_gate_evidence
from fleet_graph.graphs.dd_scripts import AUTHOR_EMAIL, AUTHOR_NAME, GATE_PATH, write_json
from fleet_graph.graphs.stop_response import (
    REASON_CONSUMER_UNWIRED,
    REASON_PAYLOAD_SCHEMA,
    STATUS_CONSUMED,
    STATUS_FAILED,
    reject_board_incomplete,
    validate_gate_payload,
)

#: The dd control plane state an action may be released from. Anything else is
#: a refusal before the single is touched.
STATE_AWAITING_GATE = "awaiting_gate"

#: The closed refusal codes a failed gate receipt carries.
CODE_NOT_DISPATCHER = "decided_by_not_dispatcher"
CODE_NOT_AWAITING_GATE = "not_awaiting_gate"
CODE_UNRESOLVED = "single_unresolved"
CODE_OBLIGATIONS_FAILED = "gate_obligations_failed"
CODE_REJECT_CONTRACT_INCOMPLETE = "reject_contract_incomplete"
CODE_RELEASE_REFUSED = "release_refused"

#: The workspace seal author (the same machine identity dd_scripts seals with).
GATE_SEAL_MESSAGE = "dev-dispatch: gate decision sealed by the graph gate node"


class DdGatePort(Protocol):
    """The goal-line gate node's port: one consume = one gate-release action."""

    def consume(
        self, action: dict[str, Any], *, folder_id: str, round_no: int
    ) -> dict[str, Any]: ...


class GraphGateNode:
    """The production gate node over the dd control plane (in-process).

    ``plane`` is the same duck type the dd subgraph's gateway holds (``get``,
    ``publish_gate_decision``, ``gate``); ``evidence`` / ``rerun`` /
    ``regression_probe`` are the mechanical seams the obligation collector
    already defines -- injectable so an engine-level fixture can drive the real
    node against a micro subject without touching the mechanics themselves.
    """

    def __init__(
        self,
        plane: Any,
        *,
        dd_root: Path | str = DEFAULT_DD_ROOT,
        evidence: Any = None,
        rerun: Any = None,
        regression_probe: Any = None,
    ) -> None:
        self.plane = plane
        self.dd_root = Path(dd_root)
        self._evidence = evidence
        self._rerun = rerun
        self._regression_probe = regression_probe

    def _receipt(
        self,
        action: dict[str, Any],
        *,
        round_no: int,
        status: str,
        code: str,
        detail: str,
        **extra: Any,
    ) -> dict[str, Any]:
        receipt = {
            "kind": str(action.get("kind") or ""),
            "idempotency_key": str(action.get("idempotency_key") or ""),
            "status": status,
            "reason": code,
            "detail": detail,
        }
        receipt.update(extra)
        return receipt

    # -- policy obligations ------------------------------------------------

    def _obligations(self, development_id: str) -> list[EvidenceItem]:
        if self._evidence is not None:
            # Test/fixture seam: a fully-grounded answer set (or a callable
            # producing one), used instead of the collector. Never in
            # production wiring.
            if callable(self._evidence):
                return list(self._evidence(development_id))
            return list(self._evidence)
        kwargs: dict[str, Any] = {}
        if self._rerun is not None:
            kwargs["rerun"] = self._rerun
        if self._regression_probe is not None:
            kwargs["regression_probe"] = self._regression_probe
        from fleet_graph.dd.gate_policy import STANDARD
        from fleet_graph.dd.self_gate_evidence import collect_standard_gate_evidence

        policy = self.plane.get(development_id).get("gate_policy", "legacy-six-v1")
        if policy == STANDARD:
            return collect_standard_gate_evidence(
                development_id=development_id, dd=self.plane, dd_root=self.dd_root
            )
        return collect_gate_evidence(
            development_id=development_id,
            dd=self.plane,
            dd_root=self.dd_root,
            **kwargs,
        )

    def _seal_decision_file(
        self,
        *,
        workspace: Path,
        development_id: str,
        generation: int,
        decision: str,
        decided_by: str,
        action_key: str,
        evidence: list[EvidenceItem],
        head_commit: str,
        rationale: str = "",
        decision_message_id: str = "",
    ) -> str:
        """Write and commit the gate verdict into the subject workspace.

        This write is the in-graph gate's own seal (check-11's data source):
        ``decided_by`` is recorded here by the node itself, inside the
        single's own tree, durably -- a verdict that is not attributable
        afterwards is a verdict nobody can audit.
        """
        relative = GATE_PATH.format(generation=generation)
        write_json(
            workspace,
            relative,
            {
                "development_id": development_id,
                "decision": decision,
                "decided_by": decided_by,
                "decided_by_source": "graph-gate-node",
                "action_key": action_key,
                "decision_message_id": decision_message_id,
                "rationale": rationale or render_rationale(evidence),
                "evidence": [
                    {"id": item.id, "passed": item.passed, "detail": item.detail}
                    for item in evidence
                ],
                "output_commit": head_commit,
            },
        )
        run_git(workspace, "add", "--", relative, check=True)
        run_git(
            workspace,
            "-c",
            f"user.name={AUTHOR_NAME}",
            "-c",
            f"user.email={AUTHOR_EMAIL}",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            GATE_SEAL_MESSAGE,
            check=True,
        )
        return relative

    # -- consumption ---------------------------------------------------------

    def consume(self, action: dict[str, Any], *, folder_id: str, round_no: int) -> dict[str, Any]:
        payload = action.get("payload") or {}
        development_id = payload.get("development_id") if isinstance(payload, dict) else None
        try:
            lock = (
                self.plane.operation_lock(development_id)
                if development_id and hasattr(self.plane, "operation_lock")
                else nullcontext()
            )
            with lock:
                return self._consume(action, folder_id=folder_id, round_no=round_no)
        except Exception as exc:
            return self._receipt(
                action,
                round_no=round_no,
                status=STATUS_FAILED,
                code=CODE_RELEASE_REFUSED,
                detail=str(exc),
            )

    def _consume(self, action: dict[str, Any], *, folder_id: str, round_no: int) -> dict[str, Any]:
        """Consume one ``dd.gate_release.v1`` action; never raises into the line.

        Every refusal is a failed receipt naming its code; the release path is
        reached only past schema, state, identity, obligations and (for a
        REJECT) the board-binding contract.
        """
        payload = action.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        schema_error = validate_gate_payload(payload)
        if schema_error:
            return self._receipt(
                action,
                round_no=round_no,
                status=STATUS_FAILED,
                code=REASON_PAYLOAD_SCHEMA,
                detail=schema_error,
            )

        development_id = str(payload["development_id"]).strip()
        verdict = str(payload["verdict"]).strip().upper()
        decided_by = str(payload["decided_by"]).strip()

        if self.plane is None:
            return self._receipt(
                action,
                round_no=round_no,
                status=STATUS_FAILED,
                code=REASON_CONSUMER_UNWIRED,
                detail="no dd control plane is wired to this line's gate node",
                development_id=development_id,
            )

        try:
            status = dict(self.plane.get(development_id))
        except Exception as exc:
            return self._receipt(
                action,
                round_no=round_no,
                status=STATUS_FAILED,
                code=CODE_UNRESOLVED,
                detail=f"single {development_id!r} cannot be resolved: {type(exc).__name__}: {exc}",
                development_id=development_id,
            )

        if str(status.get("state") or "") != STATE_AWAITING_GATE:
            return self._receipt(
                action,
                round_no=round_no,
                status=STATUS_FAILED,
                code=CODE_NOT_AWAITING_GATE,
                detail=(
                    f"single {development_id!r} is {status.get('state')!r}, not "
                    f"{STATE_AWAITING_GATE!r}; a release is only consumable at the gate"
                ),
                development_id=development_id,
            )

        dispatched_by = str(status.get("dispatched_by") or "")
        if not dispatched_by or decided_by != dispatched_by or folder_id != dispatched_by:
            # The M2 identity invariant: the decider is the dispatcher, or the
            # release is refused with the single untouched (REJECT + 留痕).
            return self._receipt(
                action,
                round_no=round_no,
                status=STATUS_FAILED,
                code=CODE_NOT_DISPATCHER,
                detail=(
                    f"decided_by {decided_by!r} is not the dispatching line "
                    f"{dispatched_by!r} of {development_id!r}; the single is untouched"
                ),
                development_id=development_id,
                dispatched_by=dispatched_by,
            )

        request_id = str((status.get("awaiting") or {}).get("question_note_id") or "")
        if request_id.startswith("engine:") and payload.get("question_note_id") != request_id:
            return self._receipt(
                action,
                round_no=round_no,
                status=STATUS_FAILED,
                code="gate_request_mismatch",
                detail="裁决必须绑定当前审单请求 question_note_id",
                development_id=development_id,
            )
        evidence = self._obligations(development_id)
        from fleet_graph.dd.gate_policy import LEGACY, STANDARD, STANDARD_EVIDENCE
        from fleet_graph.dd.self_gate import REQUIRED_EVIDENCE

        policy = status.get("gate_policy", LEGACY)
        if policy not in {STANDARD, LEGACY}:
            return self._receipt(
                action,
                round_no=round_no,
                status=STATUS_FAILED,
                code="unknown_gate_policy",
                detail=f"未知审单协议 {policy!r}",
            )
        incomplete = collect_evidence(
            evidence, required_ids=STANDARD_EVIDENCE if policy == STANDARD else REQUIRED_EVIDENCE
        )
        failed_items = [item for item in evidence if not item.passed]
        if incomplete is not None or (failed_items and verdict == "APPROVE"):
            detail = render_rationale(evidence)
            if incomplete is not None:
                detail = f"{incomplete.detail}; {detail}"
            return self._receipt(
                action,
                round_no=round_no,
                status=STATUS_FAILED,
                code=CODE_OBLIGATIONS_FAILED,
                detail=detail,
                development_id=development_id,
                evidence=[
                    {"id": item.id, "passed": item.passed, "detail": item.detail}
                    for item in evidence
                ],
            )

        if verdict == "REJECT":
            missing_board = reject_board_incomplete(payload)
            if missing_board:
                # ⑮: a REJECT without the board's three non-empty fields is
                # refused by the gate and traced; the single stays at the gate.
                return self._receipt(
                    action,
                    round_no=round_no,
                    status=STATUS_FAILED,
                    code=CODE_REJECT_CONTRACT_INCOMPLETE,
                    detail=(
                        "a REJECT must bind the board adjudication's three "
                        f"non-empty fields; missing: {missing_board!r}"
                    ),
                    development_id=development_id,
                )

        # -- release ---------------------------------------------------------
        try:
            workspace = Path(str(status.get("repo_path") or status.get("worktree_path") or ""))
            if not workspace.is_dir():
                raise RuntimeError(f"subject workspace {str(workspace)!r} does not exist")
            try:
                generation = int(status.get("generation") or 1)
            except (TypeError, ValueError):
                generation = 1
            head = run_git(workspace, "rev-parse", "HEAD", check=True).stdout.strip()
            idempotency_key = str(action.get("idempotency_key") or "")
            action_key = f"dd-gate-node:{development_id}:g{generation}:{idempotency_key}:{verdict}"
            rationale = render_rationale(evidence)
            if verdict == "REJECT":
                rationale += "\n" + json.dumps(
                    {
                        key: payload["board_decision"][key]
                        for key in ("problem", "suggested_answer", "cost_of_no_answer")
                    },
                    ensure_ascii=False,
                )

            published = dict(
                self.plane.publish_gate_decision(
                    development_id,
                    decision=verdict,
                    decided_by=decided_by,
                    reason=rationale,
                    action_key=action_key,
                )
            )
            decision_file = self._seal_decision_file(
                workspace=workspace,
                development_id=development_id,
                generation=generation,
                decision=verdict,
                decided_by=decided_by,
                action_key=action_key,
                evidence=evidence,
                head_commit=head,
                rationale=str(published.get("rationale") or rationale),
                decision_message_id=str(published.get("message_id") or ""),
            )
            resume = dict(self.plane.gate(development_id, resume=True, action_key=action_key))
            resume_entry = dict(resume.get("resume") or {})
            # The honest read-back: whether the single actually left the gate
            # right after the resume. Recorded on the receipt -- the release
            # receipt is the S10 consumption evidence, so it must not claim a
            # consumption the single's own state contradicts.
            after = dict(self.plane.get(development_id))
            post_release_state = str(after.get("state") or "")
        except Exception as exc:
            return self._receipt(
                action,
                round_no=round_no,
                status=STATUS_FAILED,
                code=CODE_RELEASE_REFUSED,
                detail=f"release refused for {development_id!r}: {type(exc).__name__}: {exc}",
                development_id=development_id,
            )

        # S10: the consumption evidence is this receipt itself.
        return self._receipt(
            action,
            round_no=round_no,
            status=STATUS_CONSUMED,
            code="",
            detail=(
                f"gate release consumed: verdict {verdict} by {decided_by} sealed at "
                f"{decision_file}, published to the single's decision read model, "
                "and the suspended pipeline resumed"
            ),
            development_id=development_id,
            decision=verdict,
            decided_by=decided_by,
            decided_by_source="graph-gate-node",
            decision_file=decision_file,
            decision_message_id=str(published.get("message_id") or ""),
            post_release_state=post_release_state,
            launches={
                "unit": str(resume_entry.get("unit") or ""),
                "mode": str(resume_entry.get("mode") or "resume"),
                "generation": resume_entry.get("generation", generation),
            },
            evidence=[
                {"id": item.id, "passed": item.passed, "detail": item.detail} for item in evidence
            ],
        )


__all__ = [
    "CODE_NOT_AWAITING_GATE",
    "CODE_NOT_DISPATCHER",
    "CODE_OBLIGATIONS_FAILED",
    "CODE_REJECT_CONTRACT_INCOMPLETE",
    "CODE_RELEASE_REFUSED",
    "CODE_UNRESOLVED",
    "GATE_SEAL_MESSAGE",
    "STATE_AWAITING_GATE",
    "DdGatePort",
    "GraphGateNode",
]
