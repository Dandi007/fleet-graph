"""The line self-gate decision (M3): six evidence obligations -> one verdict.

After M1 (``dd_awaiting_gate`` wake) and M2 (``decision_deliver`` for a dd
single with a principal check), the dispatching line is its own gate: on a
``dd_awaiting_gate`` wake the line mechanically discharges **six evidence
obligations** and then delivers ``APPROVE``/``REJECT`` through the exact dd
delivery path the decision surface already checks. ``decided_by`` *is* the
line's principal, and the delivery path refuses any principal that is not the
single's ``dispatched_by`` -- so a self-gate verdict can never be cast by
anyone but the dispatching line.

The six obligations are the gate's non-negotiable required fields (design.md
§6.2/§6.3; goal.md §二 M3 + S8/S9/S10/S11/S12). Missing any one of them leaves
the delivery **refused** -- the negative criterion a test must be able to turn
red:

1. **acceptance-argv frozen and verbatim** (three-way machine equality: spec
   freeze == ``record.json.acceptance_commands`` == stage receipt command).
2. **product diff stays inside the spec's declared surface** (machine files
   ``.dev-dispatch/`` / ``.dd-evidence/`` excepted).
3. **zero test deletion** (``--diff-filter=D`` for ``base..head`` is empty).
4. **the line personally reran the frozen acceptance command** and kept the
   echo (exit 0).
5. **mutation receipt** (S12): the *final_review* stage deletes each new
   production call site in a throwaway copy and proves the frozen acceptance
   goes red for every one; the gate does **not** rerun -- it only verifies the
   receipt names exactly the mechanically-enumerated target set and that every
   target landed red.
6. **regression vs. the frozen baseline** (S9): anchored on
   ``record.json.target_base_commit`` (never a drifted main head); the red set
   may not expand and no green->red flip is admitted (red->green is an
   improvement, admitted). A lone red increment that is a known net-base flake
   is admissible only with an isolated rerun attribution on the clean base.

The functions here are pure over plain data so the whole gate decision is
testable without a live dd root, a live git repo, or a live pytest run. The
production runners (``graphs/runner.py``) import :func:`deliver_self_gate_decision`
and supply the evidence it consumes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleet_graph.decision_mcp import (
    DECISION_APPROVE,
    DECISION_REJECT,
    OUTCOME_REFUSED,
    DeliveryResult,
    deliver_decision,
)

#: Evidence ids, in the six-obligation order the spec pins. Each is a required
#: field of the gate decision; a decision may not be delivered with any missing.
EVIDENCE_ACCEPTANCE_FROZEN = "acceptance_frozen"
EVIDENCE_DIFF_WITHIN_SCOPE = "diff_within_scope"
EVIDENCE_ZERO_TEST_DELETION = "zero_test_deletion"
EVIDENCE_PERSONALLY_RERUN = "personally_rerun"
EVIDENCE_MUTATION_RECEIPT = "mutation_receipt"
EVIDENCE_REGRESSION = "regression"

REQUIRED_EVIDENCE = (
    EVIDENCE_ACCEPTANCE_FROZEN,
    EVIDENCE_DIFF_WITHIN_SCOPE,
    EVIDENCE_ZERO_TEST_DELETION,
    EVIDENCE_PERSONALLY_RERUN,
    EVIDENCE_MUTATION_RECEIPT,
    EVIDENCE_REGRESSION,
)

#: The refusal code a self-gate delivery carries when an obligation is absent.
CODE_SELF_GATE_EVIDENCE_INCOMPLETE = "SELF_GATE_EVIDENCE_INCOMPLETE"

#: Machine-file prefixes exempt from the "product diff within spec surface"
#: obligation (they are produced by the pipeline, never by the product).
MACHINE_PREFIXES = (".dev-dispatch/", ".dd-evidence/")


@dataclass(frozen=True)
class EvidenceItem:
    """One grounded gate-obligation answer.

    ``id`` is one of ``EVIDENCE_*``; ``passed`` is the mechanical verdict;
    ``detail`` carries the machine-comparable rationale (digests, counts, the
    offending path, the re-run echo) so a refusal names its reason rather than
    a bare "no".
    """

    id: str
    label: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class MutationTarget:
    """One production call site mechanically enumerated from ``base..head``."""

    file: str
    line: int
    call: str


# A Python call expression on an added line: ``name(...)``, possibly assigned or
# nested. We deliberately match the call *post-fix* so chained/assigned calls
# (``result = deliver_self_gate_decision(...)``) are still one target.
_CALL_RE = re.compile(r"(?<![.\w])([A-Za-z_]\w*)\s*\(")


def _argv_equal(left: Any, right: Any) -> bool:
    """Machine-compare two acceptance argv lists (list of argv lists)."""
    return _canonical_commands(left) == _canonical_commands(right)


def _canonical_commands(commands: Any) -> list[list[str]]:
    if commands is None:
        return []
    if isinstance(commands, list):
        out: list[list[str]] = []
        for entry in commands:
            if isinstance(entry, list):
                out.append([str(p) for p in entry])
            else:
                out.append([str(entry)])
        return out
    return []


def evidence_acceptance_frozen(
    *,
    spec_argv: list[list[str]],
    record_acceptance_commands: list[list[str]],
    receipt_command: list[list[str]],
) -> EvidenceItem:
    """Obligation 1: the three assertions of the frozen acceptance argv are
    byte-equal (spec freeze == record.json == stage receipt)."""
    equal = _argv_equal(spec_argv, record_acceptance_commands) and _argv_equal(
        record_acceptance_commands, receipt_command
    )
    detail = (
        "spec/record/receipt acceptance argv identical"
        if equal
        else f"acceptance argv diverge: spec={spec_argv!r} "
        f"record={record_acceptance_commands!r} receipt={receipt_command!r}"
    )
    return EvidenceItem(EVIDENCE_ACCEPTANCE_FROZEN, "acceptance frozen and verbatim", equal, detail)


def evidence_diff_within_scope(
    *, changed_product_paths: list[str], spec_deliverable_prefixes: list[str]
) -> EvidenceItem:
    """Obligation 2: every product change maps to the spec's declared surface.

    Machine files (``.dev-dispatch/`` / ``.dd-evidence/``) are ignored; any other
    product path that matches none of the declared deliverable prefixes is an
    out-of-scope change and fails the obligation.
    """
    offenders = [
        path
        for path in changed_product_paths
        if not _is_machine_path(path) and not _within_scope(path, spec_deliverable_prefixes)
    ]
    passed = not offenders
    detail = (
        "product diff stays inside the declared surface"
        if passed
        else f"out-of-scope product changes: {offenders!r}"
    )
    return EvidenceItem(
        EVIDENCE_DIFF_WITHIN_SCOPE, "product diff within spec surface", passed, detail
    )


def evidence_zero_test_deletion(*, deleted_paths: list[str]) -> EvidenceItem:
    """Obligation 3: ``base..head`` ``--diff-filter=D`` is empty.

    Only a *deleted* file counts; a test whose assertions were updated is an
    edit, not a deletion.
    """
    test_deletions = [path for path in deleted_paths if _is_test_path(path)]
    passed = not test_deletions
    detail = (
        "no test file deleted in base..head"
        if passed
        else f"deleted test files: {test_deletions!r}"
    )
    return EvidenceItem(EVIDENCE_ZERO_TEST_DELETION, "zero test deletion", passed, detail)


def evidence_personally_rerun(
    *, rerun_command: list[str], frozen_command: list[str], rerun_echo: str, rerun_exit_code: int
) -> EvidenceItem:
    """Obligation 4: the line reran the frozen command itself and kept the echo.

    ``rerun_command`` must equal the frozen acceptance command exactly, the echo
    must be non-empty (the output is retained as evidence), and the exit code
    must be 0. Anything else means the line did not personally pass acceptance.
    """
    same_command = _argv_equal([rerun_command], [frozen_command])
    passed = same_command and bool(rerun_echo.strip()) and rerun_exit_code == 0
    detail = (
        "line reran the frozen command (exit 0, echo retained)"
        if passed
        else (
            f"rerun not satisfied: same_command={same_command}, "
            f"echo_present={bool(rerun_echo.strip())}, exit={rerun_exit_code}"
        )
    )
    return EvidenceItem(EVIDENCE_PERSONALLY_RERUN, "personally reran acceptance", passed, detail)


def enumerate_mutation_targets(
    added_lines: dict[str, list[tuple[int, str]]],
) -> list[MutationTarget]:
    """Obligation 5 (S12): mechanically enumerate new production call sites.

    ``added_lines`` maps a product-relative path to its *added* lines (``(line
    number, text)``) as reported by ``base..head``. A production call site is an
    added line in a ``src/**/*.py`` file that is neither an import nor a
    ``def``/``class`` declaration and carries a Python call expression. This is
    deliberate mechanization, never "the line chose its own targets": the same
    diff yields the same set regardless of who runs it.
    """
    targets: list[MutationTarget] = []
    for relpath, lines in added_lines.items():
        if not _is_product_source(relpath):
            continue
        for lineno, text in lines:
            stripped = text.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("from ", "import ")):
                continue
            if stripped.startswith(("def ", "class ", "async def ")):
                continue
            match = _CALL_RE.search(text)
            if match is None:
                continue
            targets.append(MutationTarget(file=relpath, line=lineno, call=match.group(1)))
    return targets


def verify_mutation_receipt(
    *,
    enumerated: list[MutationTarget],
    receipt_targets: list[dict[str, Any]],
) -> EvidenceItem:
    """Obligation 5 (S12): the gate only *verifies* the final_review receipt.

    Passed iff the receipt names exactly the mechanically-enumerated target set
    (no missing, no fabricated extra) *and* every target landed red. The gate
    never reruns the mutation experiment itself -- rerunning would be "shooting
    one's own blind spot", so it validates the receipt against the enumeration.
    """
    enumerated_set = {(t.file, t.line, t.call) for t in enumerated}
    receipt_set = {
        (str(t.get("file") or ""), int(t.get("line") or 0), str(t.get("call") or ""))
        for t in receipt_targets
        if isinstance(t, dict)
    }
    if receipt_set != enumerated_set:
        missing = sorted(enumerated_set - receipt_set)
        extra = sorted(receipt_set - enumerated_set)
        return EvidenceItem(
            EVIDENCE_MUTATION_RECEIPT,
            "mutation receipt verified",
            False,
            f"receipt target set != enumeration: missing={missing!r} extra={extra!r}",
        )
    not_red = [t for t in receipt_targets if not t.get("red")]
    if not_red:
        return EvidenceItem(
            EVIDENCE_MUTATION_RECEIPT,
            "mutation receipt verified",
            False,
            f"targets that did not land red: {not_red!r}",
        )
    return EvidenceItem(
        EVIDENCE_MUTATION_RECEIPT,
        "mutation receipt verified",
        True,
        f"receipt == enumeration ({len(enumerated_set)} targets, all red)",
    )


@dataclass(frozen=True)
class RegressionBaseline:
    """The frozen-base regression snapshot (S9): counts + the red test set."""

    failed_tests: frozenset[str]
    passed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0


def evidence_regression(
    *,
    baseline: RegressionBaseline,
    patched_failed: set[str],
    target_base_commit: str,
    comparison_base_commit: str,
    flake_attributions: dict[str, str] | None = None,
) -> EvidenceItem:
    """Obligation 6 (S9): regression vs. the *frozen* base, drift-proof.

    Rules, exactly as the spec weighs them (goal.md §二 M3 S9):

    - the baseline must be anchored on ``record.json.target_base_commit``, not a
      main head that drifted while the single ran;
    - the red set may not expand (``patched_failed ⊆ baseline.failed_tests``),
      which is also the green->red flip refusal (a green test turning red is a
      *new* red); red->green is an improvement and is admitted;
    - a lone red increment that is a known net-base flake is admitted only with
      an isolated re-run attribution on the clean base (``flake_attributions``).

    ``flake_attributions`` maps a flaky test id to its re-run evidence string.
    """
    flakes = dict(flake_attributions or {})
    reason_parts: list[str] = []
    passed = True

    if (
        target_base_commit
        and comparison_base_commit
        and target_base_commit != comparison_base_commit
    ):
        passed = False
        reason_parts.append(
            f"baseline anchored on drifted head {comparison_base_commit!r}, "
            f"not frozen target_base {target_base_commit!r}"
        )

    new_reds = sorted(set(patched_failed) - set(baseline.failed_tests))
    unattributed = [test for test in new_reds if test not in flakes]
    if unattributed:
        passed = False
        reason_parts.append(f"new reds (green->red or expanded red set): {unattributed!r}")

    excused = [test for test in new_reds if test in flakes]
    if excused:
        reason_parts.append(
            f"flake-attributed reds (re-run on clean base): {[(t, flakes[t]) for t in excused]!r}"
        )

    detail = (
        "red set did not expand; no green->red flip; baseline on frozen target_base"
        if passed
        else "; ".join(reason_parts)
    )
    return EvidenceItem(EVIDENCE_REGRESSION, "regression vs frozen baseline", passed, detail)


def _is_machine_path(path: str) -> bool:
    return path.startswith(MACHINE_PREFIXES)


def _within_scope(path: str, prefixes: list[str]) -> bool:
    matched = [prefix for prefix in prefixes if prefix and path.startswith(prefix)]
    return bool(matched)


def _is_test_path(path: str) -> bool:
    return (
        "tests/" in path
        or path.startswith("tests/")
        or path.endswith("_test.py")
        or "test_" in path
    )


def _is_product_source(path: str) -> bool:
    return path.startswith("src/") and path.endswith(".py")


def render_rationale(evidence: list[EvidenceItem]) -> str:
    """The rationale payload the delivery carries (machine-readable per-item)."""
    parts = [f"{item.id}={'PASS' if item.passed else 'FAIL'}:{item.detail}" for item in evidence]
    return "; ".join(parts)


def collect_evidence(items: list[EvidenceItem]) -> EvidenceItem | None:
    """The completeness gate: all six required ids present, else None.

    returns the first missing obligation as the refusal cause, or None when the
    six required obligations are all accounted for.
    """
    present = {item.id for item in items}
    for required in REQUIRED_EVIDENCE:
        if required not in present:
            return EvidenceItem(
                required,
                required,
                False,
                f"missing required gate obligation {required!r}",
            )
    return None


def deliver_self_gate_decision(
    *,
    development_id: str,
    principal: str,
    evidence: list[EvidenceItem],
    run_root: Path,
    dd: Any | None = None,
) -> DeliveryResult:
    """Discharge the six obligations, then deliver APPROVE/REJECT as the line.

    Missing any obligated field refuses delivery *without* touching the single;
    otherwise the verdict is derived mechanically (all six passed -> APPROVE,
    any failed -> REJECT) and delivered through :func:`deliver_decision`, whose
    dd path validates ``principal == dispatched_by`` -- so ``decided_by`` is the
    line and only the line. The rationale payload is the delivery ``reason``.
    """
    incomplete = collect_evidence(evidence)
    if incomplete is not None:
        return DeliveryResult(
            status=OUTCOME_REFUSED,
            code=CODE_SELF_GATE_EVIDENCE_INCOMPLETE,
            message=incomplete.detail,
            line=development_id,
            decision="",
        )

    passed = all(item.passed for item in evidence)
    decision = DECISION_APPROVE if passed else DECISION_REJECT
    reason = render_rationale(evidence)
    return deliver_decision(
        line=development_id,
        decision=decision,
        reason=reason,
        run_root=run_root,
        lines=[],
        principal=principal,
        dd=dd,
    )


__all__ = [
    "CODE_SELF_GATE_EVIDENCE_INCOMPLETE",
    "EVIDENCE_ACCEPTANCE_FROZEN",
    "EVIDENCE_DIFF_WITHIN_SCOPE",
    "EVIDENCE_MUTATION_RECEIPT",
    "EVIDENCE_PERSONALLY_RERUN",
    "EVIDENCE_REGRESSION",
    "EVIDENCE_ZERO_TEST_DELETION",
    "REQUIRED_EVIDENCE",
    "EvidenceItem",
    "MutationTarget",
    "RegressionBaseline",
    "collect_evidence",
    "deliver_self_gate_decision",
    "enumerate_mutation_targets",
    "evidence_acceptance_frozen",
    "evidence_diff_within_scope",
    "evidence_personally_rerun",
    "evidence_regression",
    "evidence_zero_test_deletion",
    "render_rationale",
    "verify_mutation_receipt",
]
