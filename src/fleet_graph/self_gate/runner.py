"""The self-gate runner: the line's six evidence duties, mechanically.

Every duty is a pure-ish function returning one evidence entry
(``{"duty", "passed", "detail", ...}``) so the gate's decision is a template
filled by machinery, never prose. :func:`deliver_self_gate_decision` runs the
six, templates the results into the rationale payload, and delivers the
verdict through M2's dd single path with the line's own principal --
``decided_by`` is therefore always the dispatching line, enforced twice (here
by construction, and again by the delivery path's identity check).

:func:`handle_dd_awaiting_gate_wake` is the M1 wake entry the goal line calls
when ``dd_awaiting_gate(dev_id)`` fires; this is the production wiring the
mutation gun must be able to shoot (S12.4: deleting the
``result = deliver_self_gate_decision(...)`` line inside it has to turn the
frozen acceptance red).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_graph.acceptance import EXIT_TIMEOUT
from fleet_graph.dd.git import run_git
from fleet_graph.dd.mutation import (
    MUTATION_ACCEPTANCE_TIMEOUT_SECONDS,
    enumerate_mutation_targets,
    verify_mutation_receipt,
)
from fleet_graph.state.run_artifacts import iso

#: Evidence template version stamped on every rationale payload.
EVIDENCE_VERSION = 1

#: The six duty names -- the gate's mandatory answer fields.
DUTY_ACCEPTANCE_THREE_WAY = "acceptance_three_way"
DUTY_PRODUCT_DIFF_BOUNDARY = "product_diff_boundary"
DUTY_ZERO_TEST_DELETIONS = "zero_test_deletions"
DUTY_ACCEPTANCE_RUN = "acceptance_run"
DUTY_MUTATION_RECEIPT = "mutation_receipt"
DUTY_REGRESSION_BASELINE = "regression_baseline"

DUTY_ORDER = (
    DUTY_ACCEPTANCE_THREE_WAY,
    DUTY_PRODUCT_DIFF_BOUNDARY,
    DUTY_ZERO_TEST_DELETIONS,
    DUTY_ACCEPTANCE_RUN,
    DUTY_MUTATION_RECEIPT,
    DUTY_REGRESSION_BASELINE,
)

#: Machine-evidence / controller-reserved namespaces excluded from the product
#: diff boundary (spec §2.2) -- same rule as the mutation enumerator.
BOUNDARY_EXCLUDED_PREFIXES = (".dev-dispatch/", ".dd-evidence/")

#: What counts as a test path for the zero-deletion duty (spec §2.3: existing
#: test updates are not deletions; only ``--diff-filter=D`` deletions count).
TEST_PATH = re.compile(r"(^|/)tests?(/|$)|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")

AcceptanceRunner = Callable[[Path, list[str]], int]

#: The gate-side acceptance re-run gets the same per-command bound the engine
#: gives its own acceptance stage (1800s). The gate re-runs the same frozen
#: argv a hanging target could have made unbounded; a wedged duty would hang
#: the wake -- and a wake that never answers is not a gate verdict.
GATE_ACCEPTANCE_TIMEOUT_SECONDS = MUTATION_ACCEPTANCE_TIMEOUT_SECONDS


class AcceptanceRunTimeout(RuntimeError):
    """Duty 4's frozen acceptance outlived its bound and never returned."""


#: The production acceptance runner: run the argv, capture everything, keep
#: the exit code. The echo captured for the evidence payload is the argv plus
#: the exit code -- a gate-side re-run must leave a visible trace. Bounded:
#: a command that never returns is a failed duty with a recorded reason
#: (exit code 124, the shell's timeout convention), never a hung gate.
def _production_acceptance_runner(
    cwd: Path, argv: list[str], timeout_seconds: float | None = None
) -> int:
    bound = GATE_ACCEPTANCE_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=bound,
        )
    except subprocess.TimeoutExpired as exc:
        raise AcceptanceRunTimeout(
            f"frozen acceptance re-run timed out after {bound}s: {list(argv)}"
        ) from exc
    return completed.returncode


@dataclass
class SelfGateDecision:
    """The gate's structured answer: verdict, evidence, and the delivery."""

    development_id: str
    verdict: str
    decided_by: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    rationale: dict[str, Any] = field(default_factory=dict)
    delivery: dict[str, Any] | None = None

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "development_id": self.development_id,
            "verdict": self.verdict,
            "decided_by": self.decided_by,
            "evidence_version": EVIDENCE_VERSION,
            "evidence": self.evidence,
            "rationale": self.rationale,
            "delivery": self.delivery,
        }


def _entry(duty: str, passed: bool, detail: str, **facts: Any) -> dict[str, Any]:
    return {"duty": duty, "passed": passed, "detail": detail, **facts}


def duty_acceptance_three_way(
    spec_acceptance: list[list[str]],
    record_acceptance: list[list[str]],
    receipt_command: list[str] | None,
) -> dict[str, Any]:
    """Duty 1: spec frozen acceptance == record acceptance == stage receipt.

    Exact machine comparison of the argv lists (one receipt command compared
    against the single frozen command; ``None`` means the stage receipt never
    named a command, which is itself a mismatch).
    """
    frozen = [list(argv) for argv in spec_acceptance]
    record = [list(argv) for argv in record_acceptance]
    receipt = list(receipt_command or [])
    expected = frozen[0] if len(frozen) == 1 else None
    passed = frozen and record and frozen == record and expected is not None and receipt == expected
    return _entry(
        DUTY_ACCEPTANCE_THREE_WAY,
        bool(passed),
        "acceptance argv equal across spec, record and stage receipt"
        if passed
        else "acceptance argv differ across spec, record and stage receipt",
        spec_acceptance=frozen,
        record_acceptance=record,
        receipt_command=receipt,
    )


def changed_product_files(repo: Path, base: str, head: str) -> list[str]:
    """Product files changed in ``base..head`` (tests and machine trees off)."""
    diff = run_git(repo, "diff", "--name-only", f"{base}..{head}")
    if diff.returncode != 0:
        raise RuntimeError(f"cannot diff {base}..{head}: {diff.stderr.strip()}")
    files = []
    for raw in diff.stdout.splitlines():
        path = raw.strip().replace("\\", "/")
        if not path:
            continue
        if path.startswith(BOUNDARY_EXCLUDED_PREFIXES):
            continue
        files.append(path)
    return sorted(files)


def duty_product_diff_boundary(changed_files: list[str], spec_surface: list[str]) -> dict[str, Any]:
    """Duty 2: every changed product file sits inside the spec's declared surface.

    ``.dev-dispatch/`` and ``.dd-evidence/`` are machine namespaces excluded by
    construction; everything else must have been declared by the spec.
    """
    declared = {path.replace("\\", "/") for path in spec_surface}
    outside = [
        path
        for path in changed_files
        if not path.replace("\\", "/").startswith(BOUNDARY_EXCLUDED_PREFIXES)
        and path.replace("\\", "/") not in declared
    ]
    return _entry(
        DUTY_PRODUCT_DIFF_BOUNDARY,
        not outside,
        "product diff inside the spec-declared surface"
        if not outside
        else f"product diff leaves the spec surface: {outside}",
        changed_files=list(changed_files),
        outside_surface=outside,
    )


def deleted_paths(repo: Path, base: str, head: str) -> list[str]:
    """Paths deleted in ``base..head`` (``git diff --diff-filter=D``)."""
    diff = run_git(repo, "diff", "--diff-filter=D", "--name-only", f"{base}..{head}")
    if diff.returncode != 0:
        raise RuntimeError(f"cannot diff deletions {base}..{head}: {diff.stderr.strip()}")
    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def duty_zero_test_deletions(repo: Path, base: str, head: str) -> dict[str, Any]:
    """Duty 3: ``base..head`` deletes no test (updates are not deletions)."""
    deleted_tests = [path for path in deleted_paths(repo, base, head) if TEST_PATH.search(path)]
    return _entry(
        DUTY_ZERO_TEST_DELETIONS,
        not deleted_tests,
        "no test deleted between base and head"
        if not deleted_tests
        else f"tests deleted between base and head: {deleted_tests}",
        deleted_tests=deleted_tests,
    )


def duty_acceptance_run(
    acceptance_commands: list[list[str]],
    cwd: Path,
    runner: AcceptanceRunner | None = None,
) -> dict[str, Any]:
    """Duty 4: the gate re-runs the frozen acceptance itself and keeps the echo.

    The line never trusts the implement stage's claimed green: it runs the
    frozen argv again at the gate and records each command's exit code as the
    receipt of having personally run it. A command that outlives its bound is
    itself a failed run -- recorded with the timeout exit code (124) and the
    reason, so the gate refuses delivery instead of hanging forever.
    """
    run = runner or _production_acceptance_runner
    results = []
    for argv in acceptance_commands:
        try:
            code = run(cwd, list(argv))
        except AcceptanceRunTimeout as exc:
            results.append(
                {
                    "argv": list(argv),
                    "exit_code": EXIT_TIMEOUT,
                    "timed_out": True,
                    "detail": str(exc),
                }
            )
            continue
        results.append({"argv": list(argv), "exit_code": code})
    passed = (
        bool(results)
        and all(item["exit_code"] == 0 for item in results)
        and not any(item.get("timed_out") for item in results)
    )
    return _entry(
        DUTY_ACCEPTANCE_RUN,
        passed,
        "frozen acceptance re-run at the gate, all green"
        if passed
        else "frozen acceptance re-run at the gate failed",
        runs=results,
        echo=iso(time.time()),
    )


def duty_mutation_receipt(
    mutation_receipt: dict[str, Any] | None,
    repo: Path,
    base: str,
    head: str,
    acceptance_commands: list[list[str]],
) -> dict[str, Any]:
    """Duty 5: verify the final_review mutation receipt -- never re-run.

    The receipt must name exactly the targets a mechanical ``base..head``
    enumeration produces and every one of them must have fallen red. The gate
    verifies the receipt only (S12.3): re-running the mutations here would put
    the gun back in the implementer's hands.
    """
    expected = enumerate_mutation_targets(repo, base, head)
    verified, violations = verify_mutation_receipt(
        mutation_receipt, expected, acceptance_commands=acceptance_commands
    )
    return _entry(
        DUTY_MUTATION_RECEIPT,
        verified,
        "mutation receipt verified: target set matches the mechanical "
        "enumeration and every target fell red"
        if verified
        else "mutation receipt refused: " + "; ".join(violations),
        violations=violations,
        expected_targets=[target.location for target in expected],
    )


def _counts_of(snapshot: dict[str, Any]) -> dict[str, int] | None:
    counts = snapshot.get("counts")
    if not isinstance(counts, dict):
        return None
    try:
        return {
            "passed": int(counts.get("passed")),
            "failed": int(counts.get("failed")),
            "skipped": int(counts.get("skipped", 0)),
        }
    except (TypeError, ValueError):
        return None


def duty_regression_baseline(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    frozen_target_base: str,
    flake_evidence: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Duty 6 (S9): full regression may not regress against the frozen base.

    The baseline snapshot is anchored to the single's frozen
    ``target_base_commit`` -- never the gate-time main head, which drifts while
    the single runs and would miscount harvest conflicts as regressions. The
    judgment is the red set, not "is everything green now":

    - a missing baseline/current field refuses,
    - any green->red flip refuses,
    - any red-set growth refuses,
    - a new red may only pass with isolated-rerun flake attribution *on the
      net base*, and that attribution lands in the payload,
    - a baseline whose ``base_commit`` is not the frozen target base refuses
      (comparing against drifted main is itself a defect).
    """
    missing: list[str] = []
    if str(baseline.get("base_commit") or "") != frozen_target_base:
        return _entry(
            DUTY_REGRESSION_BASELINE,
            False,
            "baseline is not anchored to the frozen target_base_commit "
            f"{frozen_target_base!r} (got {baseline.get('base_commit')!r}); "
            "comparing against drifted main is a regression-accounting defect",
            baseline_commit=baseline.get("base_commit"),
            frozen_target_base=frozen_target_base,
        )
    baseline_counts = _counts_of(baseline)
    current_counts = _counts_of(current)
    if baseline_counts is None:
        missing.append("baseline.counts")
    if current_counts is None:
        missing.append("current.counts")
    baseline_failed = baseline.get("failed_tests")
    current_failed = current.get("failed_tests")
    if not isinstance(baseline_failed, list):
        missing.append("baseline.failed_tests")
    if not isinstance(current_failed, list):
        missing.append("current.failed_tests")
    if missing:
        return _entry(
            DUTY_REGRESSION_BASELINE,
            False,
            f"regression snapshot missing fields: {sorted(missing)}",
            missing=sorted(missing),
        )

    baseline_red = set(map(str, baseline_failed))
    current_red = set(map(str, current_failed))
    flipped = sorted(test for test in current_red if test not in baseline_red)
    grew = flipped
    attributions = {str(test_id): dict(proof) for test_id, proof in (flake_evidence or {}).items()}
    unattributed = [test for test in grew if test not in attributions]
    attributed_payload = {test: proof for test, proof in attributions.items() if test in grew}
    passed = not unattributed
    if passed and grew:
        detail = (
            "red-set growth fully attributed to net-base flake by isolated "
            f"re-run on {frozen_target_base}: {sorted(attributed_payload)}"
        )
    elif passed:
        detail = "red set did not grow against the frozen-base baseline"
    elif flipped:
        detail = f"regression against frozen base: green->red flips {flipped}" + (
            f"; unattributed new reds {unattributed}" if set(unattributed) != set(flipped) else ""
        )
    else:
        detail = f"regression snapshot missing fields: {sorted(missing)}"
    return _entry(
        DUTY_REGRESSION_BASELINE,
        passed,
        detail,
        baseline_commit=str(baseline.get("base_commit")),
        baseline_counts=baseline_counts,
        current_counts=current_counts,
        baseline_red=sorted(baseline_red),
        current_red=sorted(current_red),
        green_to_red=flipped,
        flake_attributions=attributed_payload,
    )


def _verdict_from(evidence: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    failed = [entry for entry in evidence if not entry["passed"]]
    return ("APPROVE" if not failed else "REJECT"), failed


def deliver_self_gate_decision(
    *,
    development_id: str,
    principal: str,
    record: dict[str, Any],
    repo: Path,
    base: str,
    head: str,
    spec_acceptance: list[list[str]],
    spec_surface: list[str],
    stage_receipt_command: list[str] | None,
    mutation_receipt: dict[str, Any] | None,
    regression_baseline: dict[str, Any],
    regression_current: dict[str, Any],
    workspace: Path,
    changed_files: list[str] | None = None,
    flake_evidence: dict[str, dict[str, Any]] | None = None,
    acceptance_runner: AcceptanceRunner | None = None,
    deliver: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
) -> SelfGateDecision:
    """Run the six duties, then deliver the verdict through the dd gate.

    ``decided_by`` is the line's own ``principal``; the delivery path (M2,
    S11-unified) re-validates it against ``record.json.dispatched_by``. The
    rationale payload carries the six evidence entries verbatim -- the gate's
    answer is a filled template, auditable after the fact.
    """
    files = changed_files if changed_files is not None else changed_product_files(repo, base, head)
    evidence = [
        duty_acceptance_three_way(
            spec_acceptance,
            [list(argv) for argv in record.get("acceptance_commands") or []],
            stage_receipt_command,
        ),
        duty_product_diff_boundary(files, spec_surface),
        duty_zero_test_deletions(repo, base, head),
        duty_acceptance_run(
            [list(argv) for argv in record.get("acceptance_commands") or []],
            workspace,
            acceptance_runner,
        ),
        duty_mutation_receipt(
            mutation_receipt,
            repo,
            base,
            head,
            [list(argv) for argv in record.get("acceptance_commands") or []],
        ),
        duty_regression_baseline(
            regression_baseline,
            regression_current,
            frozen_target_base=str(record.get("target_base_commit") or ""),
            flake_evidence=flake_evidence,
        ),
    ]
    verdict, failed = _verdict_from(evidence)
    rationale = {
        "development_id": development_id,
        "decided_by": principal,
        "verdict": verdict,
        "evidence_version": EVIDENCE_VERSION,
        "evidence": evidence,
        "failed_duties": [entry["duty"] for entry in failed],
        "reason": "; ".join(entry["detail"] for entry in failed)
        if failed
        else "all six gate duties passed",
    }

    from fleet_graph.decision_mcp import deliver_decision

    deliverer = deliver or deliver_decision
    result = deliverer(
        line=development_id,
        decision=verdict,
        reason=json.dumps(rationale, ensure_ascii=False, sort_keys=True),
        principal=principal,
        run_root=Path(record.get("run_root") or "/data/fleet-graph/runs"),
        clock=clock or (lambda: 0.0),
    )
    delivery = result.as_dict() if hasattr(result, "as_dict") else dict(result)
    return SelfGateDecision(
        development_id=development_id,
        verdict=verdict,
        decided_by=principal,
        evidence=evidence,
        rationale=rationale,
        delivery=delivery,
    )


def handle_dd_awaiting_gate_wake(
    development_id: str,
    *,
    principal: str,
    dd: Any,
    spec_acceptance: list[list[str]],
    spec_surface: list[str],
    stage_receipt_command: list[str] | None = None,
    regression_baseline: dict[str, Any] | None = None,
    regression_current: dict[str, Any] | None = None,
    mutation_receipt: dict[str, Any] | None = None,
    flake_evidence: dict[str, dict[str, Any]] | None = None,
    acceptance_runner: AcceptanceRunner | None = None,
    deliver: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """M1 wake entry: the line answers its ``dd_awaiting_gate`` wake itself.

    This is the production wiring the frozen acceptance must cover: the wake
    resolves the single's own facts (record, workspace, commits) and hands
    them to the six-duty gate. Deleting the ``result = deliver_self_gate_decision(...)``
    line below removes the line's gate from the wake path -- which is exactly
    the instance mutation target S12.4 pins with a red-able test.
    """
    status = dd.get(development_id)
    workspace = Path(str(status.get("worktree_path") or ""))
    record = {
        "acceptance_commands": status.get("acceptance_commands") or [],
        "target_base_commit": status.get("target_base_commit") or "",
        "repo_path": status.get("repo_path") or str(workspace),
        "run_root": "/data/fleet-graph/runs",
    }
    head = str(status.get("head_commit") or "")
    base = str(status.get("target_base_commit") or "")
    if regression_baseline is None:
        regression_baseline = {"base_commit": base, "counts": {}, "failed_tests": []}
    if regression_current is None:
        regression_current = {"counts": {}, "failed_tests": []}
    result = deliver_self_gate_decision(
        development_id=development_id,
        principal=principal,
        record=record,
        repo=Path(str(record["repo_path"])),
        base=base,
        head=head,
        spec_acceptance=spec_acceptance,
        spec_surface=spec_surface,
        stage_receipt_command=stage_receipt_command,
        mutation_receipt=mutation_receipt,
        regression_baseline=regression_baseline,
        regression_current=regression_current,
        workspace=workspace,
        flake_evidence=flake_evidence,
        acceptance_runner=acceptance_runner,
        deliver=deliver,
        clock=clock,
    )
    return result.as_dict()


__all__ = [
    "DUTY_ACCEPTANCE_RUN",
    "DUTY_ACCEPTANCE_THREE_WAY",
    "DUTY_MUTATION_RECEIPT",
    "DUTY_ORDER",
    "DUTY_PRODUCT_DIFF_BOUNDARY",
    "DUTY_REGRESSION_BASELINE",
    "DUTY_ZERO_TEST_DELETIONS",
    "EVIDENCE_VERSION",
    "GATE_ACCEPTANCE_TIMEOUT_SECONDS",
    "AcceptanceRunTimeout",
    "SelfGateDecision",
    "changed_product_files",
    "deliver_self_gate_decision",
    "duty_acceptance_run",
    "duty_acceptance_three_way",
    "duty_mutation_receipt",
    "duty_product_diff_boundary",
    "duty_regression_baseline",
    "duty_zero_test_deletions",
    "handle_dd_awaiting_gate_wake",
]
