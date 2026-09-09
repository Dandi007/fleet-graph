"""监督专用：重放原 Review 封存意图并通过正式 checkpoint API 续同代。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from fleet_graph.dd.upstream_constants import compute_json_digest
from fleet_graph.graphs.dd_pipeline import StageOutcome, dispatch_for_state
from fleet_graph.state.run_artifacts import write_json_durable


def recover_review_publication(
    compiled: Any, deps: Any, config: Any, intent_path: Path, expected_output: str
) -> None:
    """只恢复已终局的发布传输故障，不运行 actor 或生成审查结果。"""
    run_config = {"configurable": {"thread_id": config.thread_id}}
    snapshot = compiled.get_state(run_config)
    state = snapshot.values
    if snapshot.next or state.get("terminal") != "fault":
        raise ValueError("publication recovery requires an inactive fault checkpoint")
    reason = state.get("terminal_reason", "")
    if "materialize failed on" not in reason or not any(
        code in reason for code in ("PUBLISH_FAILED:", "PROVIDER_UNAVAILABLE:")
    ):
        raise ValueError("checkpoint is not a publication transport fault")
    if (
        state.get("development_id") != config.development_id
        or state.get("generation") != config.generation
    ):
        raise ValueError("publication recovery generation/identity mismatch")
    if intent_path.resolve().parent != (config.state_root / "intents").resolve():
        raise ValueError("publication intent is outside the current generation")
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    if intent.get("kind") != "review_materialization_intent":
        raise ValueError("only original review materialization intents are recoverable")
    if state.get("steps", 0) + 1 > deps.bounds.max_steps:
        raise ValueError("publication recovery exceeds existing step budget")
    stage = deps.lifecycle.stages[state["stage"]]
    if stage.id not in {"continuous_review", "final_review"}:
        raise ValueError("publication recovery requires a review stage")
    if intent.get("review_phase") != ("final" if stage.id == "final_review" else "continuous"):
        raise ValueError("review stage differs from frozen intent")
    if (
        intent.get("development_id") != config.development_id
        or intent.get("input_commit") != state.get("head_commit")
        or intent.get("remote_url") != config.remote_url
        or intent.get("remote_ref") != (config.audit_ref or config.remote_ref)
    ):
        raise ValueError("publication intent differs from frozen checkpoint/ref")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_output):
        raise ValueError("publication output must be an exact commit")
    raw = base64.b64decode(intent["artifact_bytes_base64"], validate=True)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != intent["artifact_digest"]:
        raise ValueError("original review artifact digest mismatch")
    review = json.loads(raw)
    if (
        review.get("attempt_id") != intent.get("attempt_id")
        or review.get("subject_commit") != state.get("head_commit")
        or review.get("verdict") != intent.get("verdict")
    ):
        raise ValueError("original review binding mismatch")

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(config.workspace_path), *args])

    if git("status", "--porcelain").strip():
        raise ValueError("publication worktree must be clean")
    if git("rev-parse", "HEAD").decode().strip() != expected_output:
        raise ValueError("local output differs from explicitly preserved candidate")
    git("merge-base", "--is-ancestor", state["head_commit"], expected_output)
    if git("show", expected_output + ":" + intent["artifact_path"]) != raw:
        raise ValueError("candidate does not contain the original review bytes")
    dispatch = dispatch_for_state(state, stage, deps.lifecycle)
    outcome = StageOutcome(
        event=review["verdict"],
        receipt={"review_result": review},
        produced=tuple(stage.produced_artifacts),
    )
    # The pinned materializer validates intent, metadata, tree, parent receipts
    # and exact-old CAS, including already-published output and conflicts.
    sealed = deps.materializer.materialize(stage, dispatch, outcome)
    if sealed.commit != expected_output or not sealed.receipt:
        raise ValueError("materializer did not attest the preserved output")
    if compiled.get_state(run_config).config != snapshot.config:
        raise ValueError("checkpoint advanced during publication recovery")
    event = {
        "stage": stage.id,
        "event": review["verdict"],
        "attempt": state["attempt"],
        "output_commit": sealed.commit,
        "publication_recovered": True,
        "intent_id": intent["materialization_intent_id"],
    }
    update = {
        "terminal": "",
        "terminal_reason": "",
        "terminal_code": "",
        "fault": False,
        "steps": state.get("steps", 0) + 1,
        "head_commit": sealed.commit,
        "last_event": review["verdict"],
        "last_receipt": sealed.receipt,
        "last_failure_code": "",
        "last_failure_detail": "",
        "artifacts": {
            **state.get("artifacts", {}),
            **{k: sealed.commit for k in stage.produced_artifacts},
        },
        "receipt_digests": {
            **state.get("receipt_digests", {}),
            stage.id: compute_json_digest(sealed.receipt),
        },
        "history": [*state.get("history", []), event],
    }
    compiled.update_state(snapshot.config, update, as_node="run_stage")
    if deps.observe is not None:
        deps.observe(event)
    write_json_durable(
        config.run_root / "publication-recovery.json",
        {
            "development_id": config.development_id,
            "generation": config.generation,
            "expected_input": intent["input_commit"],
            "output_commit": sealed.commit,
            "intent_id": intent["materialization_intent_id"],
            "receipt_digest": compute_json_digest(sealed.receipt),
            "event": event,
        },
    )
