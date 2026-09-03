"""Mechanical production of the six self-gate evidence obligations (M3).

The rework review (rc-aa907dfb, finding 2) caught the defect this module
closes: the six evidence builders in ``dd/self_gate.py`` were exported but had
no production caller, so a self-gate wake delivered an *empty* evidence list
and the delivery refused on missing obligations rather than gathering real
ones. This module is the production caller: from the single's frozen admission
record, the sealed stage receipts, the subject workspace's git diff, the
line's own acceptance rerun, and the recorded regression baseline it assembles
the six grounded :class:`EvidenceItem` answers :func:`deliver_self_gate_decision`
consumes.

Sources, obligation by obligation:

1. ``acceptance_frozen`` -- spec freeze (``.dev-dispatch/spec/approved.md``
   dd-acceptance block) vs the admission record's ``acceptance_commands`` vs
   the sealed *implement* receipt's ``verification_record`` argv (the command
   the stage actually ran and reported).
2. ``diff_within_scope`` -- ``git diff --name-only base..head`` against the
   product surface prefixes (machine files ``.dev-dispatch/`` /
   ``.dd-evidence/`` exempt, per the shared obligation).
3. ``zero_test_deletion`` -- ``git diff --diff-filter=D --name-only
   base..head``.
4. ``personally_rerun`` -- the gate side runs the frozen acceptance argv in
   the subject workspace itself and keeps the echo and exit code.
5. ``mutation_receipt`` -- added lines parsed from the same diff feed
   :func:`enumerate_mutation_targets`; the receipt is the sealed *final_review*
   receipt, which must name exactly the enumerated target set with every
   target red *and* carry its ``verified_items`` checklist (S12). The gate
   never reruns the mutation experiment.
6. ``regression`` -- the recorded baseline (``.dd-evidence/regression-
   baseline.json``, taken on a frozen base) vs the patched-run probe on the
   gated head. The comparison base is what the baseline snapshot actually ran
   on, so a baseline anchored on a drifted main head is refused by the pure
   rule, exactly per S9.

Failure discipline: a fault reading any source turns that obligation into a
*failed* evidence item carrying the fault -- collection never raises into the
run, and a missing source is a refusal cause, never a silent pass.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleet_graph.dd.git import run_git
from fleet_graph.dd.self_gate import (
    EVIDENCE_ACCEPTANCE_FROZEN,
    EVIDENCE_DIFF_WITHIN_SCOPE,
    EVIDENCE_MUTATION_RECEIPT,
    EVIDENCE_PERSONALLY_RERUN,
    EVIDENCE_REGRESSION,
    EVIDENCE_ZERO_TEST_DELETION,
    REQUIRED_EVIDENCE,
    EvidenceItem,
    RegressionBaseline,
    enumerate_mutation_targets,
    evidence_acceptance_frozen,
    evidence_diff_within_scope,
    evidence_personally_rerun,
    evidence_regression,
    evidence_zero_test_deletion,
    verify_mutation_receipt,
)


def _review_sidecar_files() -> dict[str, str]:
    """The engine-side sidecars per review stage (lazy: the module also imports
    this one, and the collector must not close that cycle at import time)."""
    from fleet_graph.dd.final_review import REVIEW_SIDECAR_FILES

    return REVIEW_SIDECAR_FILES


def _review_receipt_defects(payload: dict[str, Any], *, phase: str) -> list[str]:
    from fleet_graph.dd.final_review import review_receipt_defects

    return review_receipt_defects(payload, phase=phase)


#: The dd control plane's root; the same default the decision surface reads.
DEFAULT_DD_ROOT = Path("/data/fleet-graph/dd")

#: Where the frozen spec lives inside the subject workspace.
SPEC_PATH = Path(".dev-dispatch") / "spec" / "approved.md"

#: Where the regression baseline snapshot is recorded (machine file, exempt
#: from the diff-scope obligation).
BASELINE_PATH = Path(".dd-evidence") / "regression-baseline.json"

#: The sealed stage receipts this collector verifies against, by stage id.
STAGE_RECEIPT_FILES: dict[str, str] = {
    "implement": "implement-receipt.json",
    "continuous_review": "continuous-review-receipt.json",
    "final_review": "final-review-receipt.json",
}

#: The product surface the fleet-graph spec declares. Paths outside these
#: prefixes (and outside the exempt machine files) are out-of-scope changes.
PRODUCT_SURFACE_PREFIXES = (
    "src/",
    "tests/",
    "config/",
    "scripts/",
    "deploy/",
    "skills/",
    "docs/",
    "Makefile",
    "pyproject.toml",
    "uv.lock",
    "README.md",
)

#: The personal-rerun echo is kept truncated -- evidence, not a transcript.
RERUN_TIMEOUT_SECONDS = 3600
MAX_ECHO_CHARS = 4000

#: The patched-run probe the gate runs itself when none is injected.
DEFAULT_REGRESSION_PROBE_ARGV = ("uv", "run", "pytest", "-q", "-rf", "--tb=no")

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

#: ``(workspace, argv) -> (echo, exit_code)`` -- the personal-rerun seam.
RerunFn = Callable[[Path, list[str]], tuple[str, int]]
#: ``workspace -> failed test ids`` -- the patched-run regression seam.
RegressionProbeFn = Callable[[Path], set[str]]


def spec_acceptance_argv(workspace: Path) -> list[list[str]]:
    """The frozen acceptance argv the spec itself declares."""
    from fleet_graph.dd.control_plane import derive_acceptance_commands

    return derive_acceptance_commands((workspace / SPEC_PATH).read_bytes())


def load_stage_receipts(
    dd_root: Path, development_id: str, *, generation: int = 1
) -> dict[str, list[dict[str, Any]]]:
    """Every sealed stage receipt of one generation, by stage id.

    All attempts are returned in directory order; the caller selects the one
    on the gated head. A receipt that cannot be read is skipped -- a torn
    receipt file is "no receipt", which the obligations fail on.

    Review receipts are returned as their *view*: the sealed receipt merged
    with its engine-side sidecar (S12), the sidecar winning on the fields it
    owns. The sealed receipt's fixed field set cannot carry the mutation
    record or the checklist, so an un-merged receipt would read as invalid
    even when the stage produced its evidence.
    """
    gen_root = (
        Path(dd_root) / development_id
        if generation <= 1
        else Path(dd_root) / development_id / f"g{generation}"
    )
    receipts = gen_root / "state" / "receipts"
    sidecars = _review_sidecar_files()
    out: dict[str, list[dict[str, Any]]] = {}
    if not receipts.is_dir():
        return out
    for entry in sorted(receipts.iterdir()):
        for stage, name in STAGE_RECEIPT_FILES.items():
            path = entry / name
            if not path.is_file():
                continue
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(receipt, dict):
                continue
            view = dict(receipt)
            sidecar_name = sidecars.get(stage)
            sidecar_path = entry / sidecar_name if sidecar_name else None
            if sidecar_path is not None and sidecar_path.is_file():
                try:
                    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    sidecar = None
                if isinstance(sidecar, dict):
                    view.update(sidecar)
            out.setdefault(stage, []).append(view)
    return out


def _git_head(workspace: Path) -> str:
    proc = run_git(workspace, "rev-parse", "HEAD", check=True)
    return proc.stdout.strip()


def _is_ancestor(workspace: Path, commit: str, head: str) -> bool:
    if not commit:
        return False
    if commit == head:
        return True
    return run_git(workspace, "merge-base", "--is-ancestor", commit, head).returncode == 0


def _commit_epoch(workspace: Path, commit: str) -> int:
    proc = run_git(workspace, "show", "-s", "--format=%ct", commit)
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return 0


def receipt_on_head(
    workspace: Path,
    receipts: list[dict[str, Any]],
    *,
    commit_field: str,
    fallback_fields: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """The accepted attempt's receipt: the one whose output is the gated head.

    A commit is "on the head" when it equals ``HEAD`` or is its ancestor. Of
    those (rework loops can seal several accepted attempts in one generation),
    the tip of the ancestry chain is the accepted one -- a receipt whose
    output is a proper ancestor of another candidate's output is superseded.
    Ties resolve to the newest commit time, then to directory order.
    """
    head = _git_head(workspace)
    on_head = [
        receipt
        for receipt in receipts
        if _is_ancestor(workspace, str(receipt.get(commit_field) or ""), head)
        or any(
            _is_ancestor(workspace, str(receipt.get(field) or ""), head)
            for field in fallback_fields
        )
    ]
    if not on_head:
        return None
    if len(on_head) == 1:
        return on_head[0]
    tip = on_head[0]
    tip_commit = str(tip.get(commit_field) or "")
    for candidate in on_head[1:]:
        candidate_commit = str(candidate.get(commit_field) or "")
        supersedes = bool(tip_commit) and _is_ancestor(workspace, tip_commit, candidate_commit)
        newer = (
            bool(candidate_commit)
            and bool(tip_commit)
            and not _is_ancestor(workspace, candidate_commit, tip_commit)
            and _commit_epoch(workspace, candidate_commit) > _commit_epoch(workspace, tip_commit)
        )
        if supersedes or newer:
            tip, tip_commit = candidate, candidate_commit
    return tip


def implement_receipt_acceptance_argv(receipt: dict[str, Any]) -> list[list[str]]:
    """The argv lists the implement receipt's verification record reports."""
    record = receipt.get("verification_record")
    commands = record.get("verification_commands") if isinstance(record, dict) else None
    argvs: list[list[str]] = []
    for entry in commands or []:
        if isinstance(entry, dict) and isinstance(entry.get("argv"), list):
            argvs.append([str(part) for part in entry["argv"]])
    return argvs


def diff_changed_paths(workspace: Path, base: str, head: str) -> list[str]:
    """Every path changed between the frozen base and the gated head."""
    proc = run_git(workspace, "diff", "--name-only", f"{base}..{head}", check=True)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def diff_deleted_paths(workspace: Path, base: str, head: str) -> list[str]:
    """Paths *deleted* between the frozen base and the gated head."""
    proc = run_git(
        workspace, "diff", "--diff-filter=D", "--name-only", f"{base}..{head}", check=True
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def diff_added_lines(workspace: Path, base: str, head: str) -> dict[str, list[tuple[int, str]]]:
    """Added lines with their new-file line numbers, per path (``-U0`` diff).

    This is the mechanical substrate of the S12 target enumeration: the same
    ``base..head`` diff always yields the same added-line map, so the mutation
    target set is derived, never chosen.
    """
    proc = run_git(workspace, "diff", "--unified=0", f"{base}..{head}", check=True)
    added: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    newline = 0
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :]
            continue
        if line.startswith(
            ("diff --git ", "index ", "--- ", "new file mode", "old mode", "new mode")
        ):
            continue
        hunk = _HUNK_RE.match(line)
        if hunk is not None:
            newline = int(hunk.group(1))
            continue
        if current is None:
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            added.setdefault(current, []).append((newline, line[1:]))
            newline += 1
        elif line.startswith("-"):
            continue
        else:
            newline += 1
    return added


def default_rerun(workspace: Path, argv: list[str]) -> tuple[str, int]:
    """The production personal rerun: run the frozen argv, keep the echo."""
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=RERUN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "personal rerun timed out", 124
    echo = (proc.stdout or "") + (proc.stderr or "")
    return echo[-MAX_ECHO_CHARS:], int(proc.returncode)


def default_regression_probe(workspace: Path) -> set[str]:
    """The production patched-run probe: the full suite on the gated head."""
    try:
        proc = subprocess.run(
            list(DEFAULT_REGRESSION_PROBE_ARGV),
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=RERUN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("regression probe timed out") from exc
    output = (proc.stdout or "") + (proc.stderr or "")
    failed = {match.group(1) for match in re.finditer(r"^FAILED (\S+)", output, re.MULTILINE)}
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"regression probe exited {proc.returncode}: {output[-MAX_ECHO_CHARS:]}")
    return failed


def load_regression_baseline(workspace: Path) -> tuple[RegressionBaseline, str]:
    """(baseline snapshot, the commit that snapshot actually ran on).

    The snapshot form is the machine-comparable two-tuple S9 fixes: counts and
    the failed-test set, anchored on the commit it was taken on. A missing or
    malformed file raises -- the caller fails the obligation closed.
    """
    raw = json.loads((workspace / BASELINE_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{BASELINE_PATH} does not hold an object")
    failed_tests = frozenset(str(test) for test in raw.get("failed_tests") or [])
    return (
        RegressionBaseline(
            failed_tests=failed_tests,
            passed_count=int(raw.get("passed") or 0),
            failed_count=int(raw.get("failed") or len(failed_tests)),
            skipped_count=int(raw.get("skipped") or 0),
        ),
        str(raw.get("base_commit") or ""),
    )


@dataclass(frozen=True)
class _GateContext:
    workspace: Path
    base: str
    head: str
    record: dict[str, Any]


def _resolve_context(
    *,
    development_id: str,
    dd: Any | None,
    dd_root: Path,
    workspace: Path | None,
) -> _GateContext:
    if dd is None:
        from fleet_graph.dd.control_plane import DdControlPlane

        dd = DdControlPlane(root=Path(dd_root))
    record = dd.get(development_id)
    if workspace is None:
        workspace = Path(str(record.get("worktree_path") or record.get("repo_path") or ""))
    if not workspace.is_dir():
        raise RuntimeError(f"subject workspace {workspace} does not exist")
    return _GateContext(
        workspace=workspace,
        base=str(record.get("target_base_commit") or ""),
        head=_git_head(workspace),
        record=record,
    )


def _faulted(fault: str) -> list[EvidenceItem]:
    return [
        EvidenceItem(obligation, obligation, False, f"gate evidence collection fault: {fault}")
        for obligation in REQUIRED_EVIDENCE
    ]


def _obligation_acceptance_frozen(
    ctx: _GateContext, receipts: dict[str, list[dict[str, Any]]]
) -> EvidenceItem:
    spec_argv = spec_acceptance_argv(ctx.workspace)
    record_commands = [list(command) for command in ctx.record.get("acceptance_commands") or []]
    receipt = receipt_on_head(
        ctx.workspace, receipts.get("implement") or [], commit_field="output_commit"
    )
    if receipt is None:
        return EvidenceItem(
            EVIDENCE_ACCEPTANCE_FROZEN,
            "acceptance frozen and verbatim",
            False,
            "no sealed implement receipt on the gated head; the stage receipt "
            "command cannot be compared",
        )
    return evidence_acceptance_frozen(
        spec_argv=spec_argv,
        record_acceptance_commands=record_commands,
        receipt_command=implement_receipt_acceptance_argv(receipt),
    )


def _obligation_personally_rerun(ctx: _GateContext, rerun: RerunFn) -> EvidenceItem:
    frozen = [list(command) for command in ctx.record.get("acceptance_commands") or []]
    if not frozen:
        return EvidenceItem(
            EVIDENCE_PERSONALLY_RERUN,
            "personally reran acceptance",
            False,
            "the admission record freezes no acceptance command; nothing to rerun",
        )
    echoes: list[str] = []
    for command in frozen:
        echo, exit_code = rerun(ctx.workspace, command)
        item = evidence_personally_rerun(
            rerun_command=command,
            frozen_command=command,
            rerun_echo=echo,
            rerun_exit_code=exit_code,
        )
        echoes.append(f"$ {command}\nexit={exit_code}\n{echo}")
        if not item.passed:
            return EvidenceItem(
                EVIDENCE_PERSONALLY_RERUN, item.label, False, f"{item.detail}; echo: {echo!r}"
            )
    return EvidenceItem(
        EVIDENCE_PERSONALLY_RERUN,
        "personally reran acceptance",
        True,
        f"line reran {len(frozen)} frozen command(s) (exit 0, echo retained): "
        + " | ".join(echoes),
    )


def _obligation_mutation_receipt(
    ctx: _GateContext, receipts: dict[str, list[dict[str, Any]]]
) -> EvidenceItem:
    enumerated = enumerate_mutation_targets(diff_added_lines(ctx.workspace, ctx.base, ctx.head))
    receipt = receipt_on_head(
        ctx.workspace,
        receipts.get("final_review") or [],
        commit_field="implementation_subject_commit",
        fallback_fields=("subject_commit", "output_commit"),
    )
    if receipt is None:
        return EvidenceItem(
            EVIDENCE_MUTATION_RECEIPT,
            "mutation receipt verified",
            False,
            "no sealed final_review receipt on the gated head; the mutation "
            "experiment cannot be verified",
        )
    # S12.5, engine-side schema: the receipt's view (sealed receipt + sidecar)
    # must carry the 已核验项 checklist and a position plus a red/green result
    # for every target. Missing any of it is 回执无效 -- refused, never repaired.
    defects = _review_receipt_defects(receipt, phase="final")
    if defects:
        return EvidenceItem(
            EVIDENCE_MUTATION_RECEIPT,
            "mutation receipt verified",
            False,
            "final_review receipt is not a verifiable mutation record: " + "; ".join(defects),
        )
    return verify_mutation_receipt(
        enumerated=enumerated, receipt_targets=receipt["mutation_targets"]
    )


def _obligation_regression(
    ctx: _GateContext,
    probe: RegressionProbeFn,
    flake_attributions: dict[str, str] | None,
) -> EvidenceItem:
    baseline, comparison_base = load_regression_baseline(ctx.workspace)
    patched_failed = probe(ctx.workspace)
    return evidence_regression(
        baseline=baseline,
        patched_failed=patched_failed,
        target_base_commit=ctx.base,
        comparison_base_commit=comparison_base,
        flake_attributions=flake_attributions,
    )


def collect_gate_evidence(
    *,
    development_id: str,
    dd: Any | None = None,
    dd_root: Path = DEFAULT_DD_ROOT,
    workspace: Path | None = None,
    rerun: RerunFn | None = None,
    regression_probe: RegressionProbeFn | None = None,
    flake_attributions: dict[str, str] | None = None,
) -> list[EvidenceItem]:
    """Assemble the six grounded obligations for one ``awaiting_gate`` single.

    Never raises: a fault resolving the single's frozen facts fails every
    obligation with the fault named, and a fault in one source fails only that
    obligation -- the delivery then refuses (or rejects) on real answers, and
    a missing source is never read as a pass.
    """
    try:
        ctx = _resolve_context(
            development_id=development_id, dd=dd, dd_root=dd_root, workspace=workspace
        )
    except Exception as exc:
        return _faulted(f"{type(exc).__name__}: {exc}")

    rerun = rerun or default_rerun
    regression_probe = regression_probe or default_regression_probe
    try:
        receipts = load_stage_receipts(
            dd_root, development_id, generation=int(ctx.record.get("generation") or 1)
        )
    except Exception:
        receipts = {}

    def guarded(obligation: str, produce: Callable[[], EvidenceItem]) -> EvidenceItem:
        try:
            return produce()
        except Exception as exc:
            return EvidenceItem(
                obligation,
                obligation,
                False,
                f"gate evidence collection fault: {type(exc).__name__}: {exc}",
            )

    return [
        guarded(
            EVIDENCE_ACCEPTANCE_FROZEN,
            lambda: _obligation_acceptance_frozen(ctx, receipts),
        ),
        guarded(
            EVIDENCE_DIFF_WITHIN_SCOPE,
            lambda: evidence_diff_within_scope(
                changed_product_paths=diff_changed_paths(ctx.workspace, ctx.base, ctx.head),
                spec_deliverable_prefixes=list(PRODUCT_SURFACE_PREFIXES),
            ),
        ),
        guarded(
            EVIDENCE_ZERO_TEST_DELETION,
            lambda: evidence_zero_test_deletion(
                deleted_paths=diff_deleted_paths(ctx.workspace, ctx.base, ctx.head)
            ),
        ),
        guarded(EVIDENCE_PERSONALLY_RERUN, lambda: _obligation_personally_rerun(ctx, rerun)),
        guarded(
            EVIDENCE_MUTATION_RECEIPT,
            lambda: _obligation_mutation_receipt(ctx, receipts),
        ),
        guarded(
            EVIDENCE_REGRESSION,
            lambda: _obligation_regression(ctx, regression_probe, flake_attributions),
        ),
    ]


__all__ = [
    "BASELINE_PATH",
    "DEFAULT_DD_ROOT",
    "DEFAULT_REGRESSION_PROBE_ARGV",
    "PRODUCT_SURFACE_PREFIXES",
    "SPEC_PATH",
    "STAGE_RECEIPT_FILES",
    "collect_gate_evidence",
    "default_regression_probe",
    "default_rerun",
    "diff_added_lines",
    "diff_changed_paths",
    "diff_deleted_paths",
    "implement_receipt_acceptance_argv",
    "load_regression_baseline",
    "load_stage_receipts",
    "receipt_on_head",
    "spec_acceptance_argv",
]
