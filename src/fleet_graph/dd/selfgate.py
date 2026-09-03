"""M3 line self-gate: the six mechanical evidence obligations before a dd verdict.

wf-8d9737 M3 makes the line self-gate the fleet default. A line woken by
``dd_awaiting_gate(dev_id)`` (M1) must discharge six mechanical obligations
before it may deliver ``APPROVE``/``REJECT`` through M2's
``decision_deliver``. The obligations are the *gate's mandatory fields*: a
delivery that skips any one of them is refused or marked, and the acceptance
suite can drive each one red in isolation.

The six obligations (spec §2, each a pure, deterministic function here):

1. ``acceptance_verbatim``      -- the spec's frozen acceptance argv equals the
   record's ``acceptance_commands`` equals the stage receipt command, machine-
   compared three ways.
2. ``product_diff_in_scope``    -- every product file change maps onto the
   spec's declared delivery surface; ``.dev-dispatch/`` / ``.dd-evidence/``
   machinery is exempt by construction.
3. ``zero_test_deletion``       -- ``base..head`` ``--diff-filter=D`` is empty
   (an updated assertion is a modification, not a deletion).
4. ``personally_ran_acceptance``-- the line actually re-ran the frozen
   acceptance at the gate and kept the transcript.
5. ``mutation_gun``             -- two mutations of the product, each must turn
   the frozen acceptance red, then the bytes are restored (sha/mode verified).
6. ``regression_baseline``      -- full-suite regression against the *frozen*
   ``target_base_commit`` (never the drifted main): the red set must not expand
   and any green->red flip is refused; a proven flake may be released only with
   a clean-base isolated re-run attribution in the payload (S9, 2026-09-03).

The ``assess_evidence`` composite is the one mechanical gate: any missing or
negative obligation yields a violation, and the verdict is APPROVE only when
the composite is clean *and* the principal equals the single's
``dispatched_by``. The harvest-trigger ordering (S7) is captured by
:func:`harvest_eligible`: harvest fires only after merge completes, never on a
bare gate APPROVE.

None of these functions touch git, the board, or the bus: they consume facts
the orchestration layer measured and return a judgement, which keeps them
testable in isolation and keeps judgement out of the write path (INV-3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DECISION_APPROVE = "APPROVE"
DECISION_REJECT = "REJECT"

#: The six obligation keys, in spec order. A gate delivery that does not carry
#: a positive ``ok`` under each of these is refused/marked -- never a silent pass.
REQUIRED_EVIDENCE: tuple[str, ...] = (
    "acceptance_verbatim",
    "product_diff_in_scope",
    "zero_test_deletion",
    "personally_ran_acceptance",
    "mutation_gun",
    "regression_baseline",
)

#: Protocol subtrees the product-diff obligation never counts as product changes.
#: These are controller/materializer machinery, exempt by construction.
PROTOCOL_ROOTS: tuple[str, ...] = (".dev-dispatch", ".dd-evidence")

#: How many mutations the mutation-gun obligation demands (spec: "两发").
MUTATION_GUN_SHOTS = 2


@dataclass(frozen=True)
class GateAssessment:
    """The composite gate verdict: clean, or a closed list of violations.

    ``ok`` is ``True`` exactly when no obligation is missing and none is
    negative. ``violations`` is sorted and stable so a failing gate names which
    obligation failed, never just "invalid".
    """

    ok: bool
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": list(self.violations)}


@dataclass(frozen=True)
class RegressionRun:
    """One full-suite run's machine-comparable baseline tuple (S9).

    ``failed_set`` is the set of test ids that were red. ``passed``/``failed``/
    ``skipped`` are the counts. A baseline captured on the clean frozen base and
    a head captured on the patched product are compared by :func:`regression_ok`;
    the comparison is on the *set*, never on "is it all green now".
    """

    passed: int
    failed: int
    skipped: int = 0
    failed_set: frozenset[str] = frozenset()


def _ok(reason: str = "") -> dict[str, Any]:
    return {"ok": True, "reason": reason}


def _bad(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason}


def _is_protocol_path(path: str, protocol_roots: tuple[str, ...]) -> bool:
    stripped = str(path).strip().lstrip("/")
    return any(stripped == root or stripped.startswith(f"{root}/") for root in protocol_roots)


def acceptance_argv_verbatim(
    *, spec: list[str] | tuple[str, ...], record: Any, receipt: Any
) -> dict[str, Any]:
    """Obligation 1: the three acceptance argv are byte-for-byte equal.

    Any of the three missing (empty) is itself a violation: the frozen spec
    argv, the record's ``acceptance_commands`` and the stage receipt command
    must all be present and identical -- a machine comparison, never prose.
    """
    spec_argv = list(spec)
    record_argv = list(record) if isinstance(record, (list, tuple)) else []
    receipt_argv = list(receipt) if isinstance(receipt, (list, tuple)) else []
    if not spec_argv or not record_argv or not receipt_argv:
        return _bad(
            "acceptance argv missing: spec/record/receipt must all be non-empty "
            f"(spec={spec_argv!r}, record={record_argv!r}, receipt={receipt_argv!r})"
        )
    if spec_argv != record_argv or record_argv != receipt_argv:
        return _bad(
            "acceptance argv differ three ways: "
            f"spec={spec_argv!r} record={record_argv!r} receipt={receipt_argv!r}"
        )
    return _ok()


def _in_scope(path: str, scope: set[str]) -> bool:
    """A path maps onto the surface when it equals an entry or sits under a
    directory-prefix entry (one ending in ``/``)."""
    if path in scope:
        return True
    return any(path.startswith(prefix) for prefix in scope if prefix.endswith("/"))


def product_diff_in_scope(
    *,
    changed_paths: Any,
    scope_paths: Any,
    protocol_roots: tuple[str, ...] = PROTOCOL_ROOTS,
) -> dict[str, Any]:
    """Obligation 2: every product change maps onto the declared delivery surface.

    ``changed_paths`` is the product file list from ``base..head``; ``scope_paths``
    is the spec's declared delivery surface (an exact path, or a directory
    prefix marked by a trailing ``/``). Protocol subtrees are exempt. A path
    that is neither protocol machinery nor in scope is a boundary crossing and
    names itself.
    """
    scope = {str(p).strip().lstrip("/") for p in scope_paths if str(p).strip()}
    violators: list[str] = []
    for raw in changed_paths:
        path = str(raw).strip().lstrip("/")
        if not path:
            continue
        if _is_protocol_path(path, protocol_roots):
            continue
        if not _in_scope(path, scope):
            violators.append(path)
    if violators:
        return _bad("product changes outside the declared surface: " + ", ".join(sorted(violators)))
    return _ok()


def zero_test_deletion(*, deleted_paths: Any) -> dict[str, Any]:
    """Obligation 3: ``--diff-filter=D`` between base and head is empty.

    ``deleted_paths`` is the git-deleted file list; an updated test assertion is
    a modification, not a deletion, so it never appears here.
    """
    deleted = [str(p) for p in deleted_paths if str(p).strip()]
    if deleted:
        return _bad("tests were deleted: " + ", ".join(sorted(deleted)))
    return _ok()


def personally_ran_acceptance(*, runs: Any) -> dict[str, Any]:
    """Obligation 4: the line re-ran the frozen acceptance and kept the transcript.

    ``runs`` is the list of ``{argv, exit_code}`` transcripts captured at the
    gate. Empty (or any entry missing its argv/exit_code) is a violation: a
    "not run" claim is exactly the false green this obligation ends.
    """
    if not isinstance(runs, (list, tuple)) or not runs:
        return _bad("no acceptance run transcript: the line never re-ran the frozen acceptance")
    for index, run in enumerate(runs):
        if not isinstance(run, dict) or "argv" not in run or "exit_code" not in run:
            return _bad(f"acceptance run {index} is missing argv/exit_code: {run!r}")
    return _ok()


def mutation_gun_satisfied(*, mutations: Any, shots: int = MUTATION_GUN_SHOTS) -> dict[str, Any]:
    """Obligation 5: exactly ``shots`` mutations, each red on the frozen acceptance.

    Each mutation must have turned the frozen acceptance red (``red``) and then
    been restored byte-for-byte (``restored``). Fewer/more than ``shots``, or a
    shot that is not both red and restored, is a violation.
    """
    if not isinstance(mutations, (list, tuple)) or len(mutations) != shots:
        count = len(mutations) if isinstance(mutations, (list, tuple)) else 0
        return _bad(f"mutation gun needs exactly {shots} shots, got {count}")
    for index, mutation in enumerate(mutations):
        if (
            not isinstance(mutation, dict)
            or not mutation.get("red")
            or not mutation.get("restored")
        ):
            return _bad(f"mutation shot {index} is not red-and-restored: {mutation!r}")
    return _ok()


@dataclass(frozen=True)
class FlakeAttribution:
    """A red increment that was proven a clean-base flake (S9 flake clause).

    ``test_id`` is the red test id; ``clean_base_reruns`` the isolated clean-base
    re-runs; ``red_count`` how many of those re-runs were red. The evidence rides
    the payload so a release on flake attribution is auditable, never silent.
    """

    test_id: str
    red_count: int
    clean_base_reruns: int = 0


def regression_ok(
    *,
    base: RegressionRun | None,
    head: RegressionRun | None,
    base_commit: str,
    compared_base_commit: str,
    flake_attribution: tuple[FlakeAttribution, ...] | list[FlakeAttribution] = (),
) -> dict[str, Any]:
    """Obligation 6 (S9): red set must not expand, judged against the frozen base.

    - A missing baseline (or a missing compared-base anchor) is a refusal.
    - The baseline must be anchored to the single's frozen
      ``target_base_commit`` -- comparing against a drifted main misattributes a
      harvest conflict as a regression, so ``compared_base_commit != base_commit``
      is refused outright.
    - Any test red in ``head`` but not in ``base`` is a green->red flip and a
      red-set expansion, both refused -- *unless* every such increment is a
      proven clean-base flake with an attribution in the payload (S9 flake rule).
    - A baseline that was already red is not this order's fault: red->green is an
      improvement, never a regression.
    """
    if base is None or head is None:
        return _bad("regression baseline missing: both a baseline run and a head run are required")
    if compared_base_commit != base_commit:
        return _bad(
            "regression baseline anchored to the wrong base: "
            f"compared {compared_base_commit!r} against frozen {base_commit!r}"
        )
    increment = head.failed_set - base.failed_set
    if not increment:
        return _ok(
            f"red set did not expand ({len(head.failed_set)} reds ⊆ "
            f"{len(base.failed_set)} baseline reds)"
        )
    attributed = {a.test_id for a in flake_attribution}
    unattributed = sorted(increment - attributed)
    if unattributed:
        return _bad("green->red flip without flake attribution: " + ", ".join(unattributed))
    return _ok(
        f"red increments ({sorted(increment)!r}) were proven clean-base flakes and attributed"
    )


def assess_evidence(evidence: Any) -> GateAssessment:
    """The one mechanical gate: clean only when all six obligations are positive.

    Each required key must be present with a truthy ``ok`` (or a plain ``True``
    for callers that short-circuit) -- anything missing or negative yields a
    named violation. This is the delivery gate the line consults before casting
    ``APPROVE``.
    """
    if not isinstance(evidence, dict):
        return GateAssessment(ok=False, violations=("evidence must be an object",))
    violations: list[str] = []
    for key in REQUIRED_EVIDENCE:
        if key not in evidence:
            violations.append(f"missing evidence: {key}")
            continue
        item = evidence[key]
        ok = bool(item.get("ok")) if isinstance(item, dict) else bool(item)
        if not ok:
            reason = item.get("reason", "") if isinstance(item, dict) else ""
            violations.append(f"{key}: {reason or 'failed'}")
    return GateAssessment(ok=not violations, violations=tuple(violations))


def decide(evidence: Any, *, principal: str, dispatched_by: str) -> tuple[str, GateAssessment]:
    """The line self-gate verdict: APPROVE only on a clean gate and matching principal.

    The gate's six obligations and the identity check compose into one decision:
    a wrong principal or a single violated obligation both yield ``REJECT``, and
    the ``GateAssessment`` names exactly why -- the rationale payload of the
    M2 ``decision_deliver``.
    """
    assessment = assess_evidence(evidence)
    if principal != dispatched_by:
        return DECISION_REJECT, GateAssessment(
            ok=False,
            violations=(f"principal {principal!r} is not the dispatching line {dispatched_by!r}",),
        )
    if assessment.ok:
        return DECISION_APPROVE, assessment
    return DECISION_REJECT, assessment


def harvest_eligible(*, gate_approved: bool, merge_complete: bool) -> tuple[bool, str]:
    """S7: harvest fires only after the merge segment completes, never on APPROVE.

    The harvest reactor's trigger moves from "after the gate APPROVE" to "after
    the merge segment". An approve without a completed merge is not harvestable
    (``release/<line-id>`` branch model stays M5; the dd/<id> semantics are
    unchanged until then).
    """
    if not gate_approved:
        return False, "gate not approved"
    if not merge_complete:
        return False, "merge not complete; harvest waits for the merge segment"
    return True, ""


__all__ = [
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "MUTATION_GUN_SHOTS",
    "PROTOCOL_ROOTS",
    "REQUIRED_EVIDENCE",
    "FlakeAttribution",
    "GateAssessment",
    "RegressionRun",
    "acceptance_argv_verbatim",
    "assess_evidence",
    "decide",
    "harvest_eligible",
    "mutation_gun_satisfied",
    "personally_ran_acceptance",
    "product_diff_in_scope",
    "regression_ok",
    "zero_test_deletion",
]
