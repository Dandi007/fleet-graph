"""The line self-gate's six evidence obligations (wf-8d9737 M3).

After a goal line wakes on ``dd_awaiting_gate(dev_id)`` (M1) it performs a
closed set of *six* evidence obligations before delivering the decision through
``decision_deliver`` (M2). Those six are the gate's **mandatory answer fields**:
any one of them missing must refuse (or mark) the delivery -- a delivery that
leaks through without them is a red test. The obligations:

1. **三方验收逐字相等** -- the frozen spec's acceptance argv, the single's
   ``record.json.acceptance_commands``, and the stage receipt's command are
   byte-equal (machine-compared, never prose-compared).
2. **产品 diff 未越 spec 边界** -- every changed product file maps back to a
   surface the spec declared deliverable; the ``.dev-dispatch/`` and
   ``.dd-evidence/`` machinery is always excluded.
3. **零测试删除** -- ``base..head`` has no ``--diff-filter=D`` deleted tests.
4. **亲跑验收** -- the line re-runs the frozen acceptance commands and keeps
   the echo.
5. **变异枪两发** -- two product mutations must make the frozen acceptance
   fail; afterwards the bytes are restored (sha/mode check).
6. **全量回归与放行前基线对比 (S9, 不可弱化)** -- a machine-comparable
   baseline snapshot at the *frozen* ``target_base_commit`` (never the drifting
   main head) vs. the head snapshot: the red set must not grow, and any
   green->red flip refuses. A sole red increment that is intermittent on the
   clean base must be isolated re-run before it may pass.

Nothing here invokes ``decision_deliver``; it produces (and validates) the
evidence payload that delivery embeds as its ``rationale``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The six mandatory answer fields, in spec order. A gate delivery whose
#: evidence lacks any of these is refused/标记 (spec item 2).
GATE_EVIDENCE_FIELDS: tuple[str, ...] = (
    "acceptance_equality",
    "diff_in_scope",
    "zero_test_deletion",
    "rerun_acceptance",
    "mutation",
    "regression",
)

#: Machinery paths never counted against the product-diff in-scope check
#: (spec item 2's explicit exemption).
MACHINE_PREFIXES: tuple[str, ...] = (".dev-dispatch/", ".dd-evidence/")


class GateEvidenceMissing(RuntimeError):
    """A self-gate delivery skipped into without all six evidence fields."""

    def __init__(self, missing: tuple[str, ...]) -> None:
        super().__init__("gate evidence missing mandatory field(s): " + ", ".join(missing))
        self.missing = missing


def missing_gate_evidence(evidence: dict[str, Any]) -> tuple[str, ...]:
    """The mandatory fields absent from an evidence payload (empty == complete)."""
    return tuple(name for name in GATE_EVIDENCE_FIELDS if not evidence.get(name))


def require_gate_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Refuse a self-gate evidence payload lacking any mandatory field.

    The refusal names every missing field so a red case is legible, not a bare
    "something was missing".
    """
    missing = missing_gate_evidence(evidence)
    if missing:
        raise GateEvidenceMissing(missing)
    return evidence


class GateEvidenceError(RuntimeError):
    """A concrete evidence obligation failed (a mutation that did not red, etc.)."""


# --- obligation 1: three-way acceptance equality -------------------------


def acceptance_equality(
    spec_argv: list[list[str]],
    record_argv: list[list[str]],
    receipt_argv: list[list[str]],
) -> dict[str, Any]:
    """Byte/argv-equal across three independent sources (spec item 2.1).

    Each input is a list of argv lists; comparison is exactly-ordered, so a
    reordered acceptance declaration is a mismatch, not a cosmetic diff.
    """
    equal = spec_argv == record_argv == receipt_argv
    return {
        "equal": equal,
        "spec_argv": [list(a) for a in spec_argv],
        "record_argv": [list(a) for a in record_argv],
        "receipt_argv": [list(a) for a in receipt_argv],
    }


# --- obligation 2: product diff within the spec boundary -----------------


def _is_machine_path(path: str) -> bool:
    cleaned = path.strip().lstrip("/")
    return cleaned.startswith(MACHINE_PREFIXES)


def diff_in_scope(
    changed_paths: list[str],
    declared_paths: list[str],
) -> dict[str, Any]:
    """Every changed file maps back to a declared deliverable surface.

    Machinery paths (``.dev-dispatch/``, ``.dd-evidence/``) are always in scope;
    anything else must appear in ``declared_paths``. ``declared_paths`` entries
    may name a directory (everything under it is in scope).
    """
    out_of_scope: list[str] = []
    for path in changed_paths:
        if _is_machine_path(path):
            continue
        if path in declared_paths:
            continue
        if any(path == d or path.startswith(d.rstrip("/") + "/") for d in declared_paths):
            continue
        out_of_scope.append(path)
    return {
        "in_scope": not out_of_scope,
        "changed": list(changed_paths),
        "declared": list(declared_paths),
        "out_of_scope": out_of_scope,
    }


# --- obligation 3: zero test deletion ------------------------------------


def _is_test_path(path: str) -> bool:
    return path.startswith("tests/") or (path.endswith(".py") and "test" in path)


def zero_test_deletion(deleted_paths: list[str]) -> dict[str, Any]:
    """``--diff-filter=D`` over the product range must be empty for tests.

    A test whose *assertion* changed is not a deletion; only a removed test
    file (or a test path deleted from the tree) counts. Both ``tests/...`` and
    ``*test*.py`` are treated as tests.
    """
    deleted = [path for path in deleted_paths if _is_test_path(path)]
    return {
        "zero": not deleted,
        "deleted_tests": deleted,
        "all_deleted": list(deleted_paths),
    }


# --- obligation 4: personally re-run acceptance --------------------------


@dataclass
class CommandOutcome:
    argv: list[str]
    exit_code: int
    output: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"argv": self.argv, "exit_code": self.exit_code, "output": self.output}


def rerun_acceptance(
    commands: list[list[str]],
    runner: Callable[[list[str]], tuple[int, str]],
) -> dict[str, Any]:
    """Re-run the frozen acceptance argv and keep the echo (spec item 2.4).

    ``runner`` maps an argv to ``(exit_code, combined_output)``; it is the seam
    that keeps this pure and lets the gate run the real commands while tests
    inject a scripted runner.
    """
    outcomes: list[dict[str, Any]] = []
    for argv in commands:
        code, output = runner(list(argv))
        outcomes.append(CommandOutcome(list(argv), code, output).as_dict())
    return {
        "rerun": all(outcome["exit_code"] == 0 for outcome in outcomes),
        "commands": outcomes,
    }


# --- obligation 5: mutation gun, two shots -------------------------------


def _sha256_of_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def mutation_gun(
    target: Path,
    *,
    mutate: Callable[[bytes], bytes],
    accept: Callable[[], int],
    restore: Callable[[Path, bytes], None] | None = None,
) -> dict[str, Any]:
    """Mutate the product once, the frozen acceptance must red, restore bytes.

    Two shots are fired by the caller (``accept`` returns the frozen acceptance
    exit code for the *current* tree; a mutation that still returns 0 fails).
    Restoration is byte-exact and verified (sha/mode), so a mutation gun that
    mutates but does not restore is a hard error (spec item 2.5's sha/mode
    check).
    """
    if not target.is_file():
        raise GateEvidenceError(f"mutation target {target} is not a file")
    original = target.read_bytes()
    try:
        original_stat = target.stat()
    except OSError:
        original_stat = None
    original_sha = _sha256_of_bytes(original)

    mutated = mutate(original)
    target.write_bytes(mutated)
    post_mutation_exit = accept()

    if restore is not None:
        restore(target, original)
    else:
        target.write_bytes(original)

    restored = target.read_bytes()
    restored_sha = _sha256_of_bytes(restored)
    restored_ok = restored_sha == original_sha
    if original_stat is not None:
        restored_ok = restored_ok and oct(target.stat().st_mode) == oct(original_stat.st_mode)

    return {
        "red": post_mutation_exit != 0,
        "accept_exit_after_mutation": post_mutation_exit,
        "restored": restored_ok,
        "original_sha": original_sha,
        "restored_sha": restored_sha,
    }


def two_shot_mutation_gun(
    target: Path,
    *,
    mutations: list[Callable[[bytes], bytes]],
    accept: Callable[[], int],
) -> dict[str, Any]:
    """Fire two mutations; both must red the frozen acceptance, then restore.

    The second shot is independent (no first-shot residue may make the second
    pass or fail), and the target is restored byte-exact after all shots.
    """
    if len(mutations) != 2:
        raise GateEvidenceError(f"mutation gun needs exactly two shots, got {len(mutations)}")
    shots: list[dict[str, Any]] = []
    for mutate in mutations:
        shots.append(mutation_gun(target=target, mutate=mutate, accept=accept))
    return {
        "two_shots": True,
        "red": all(shot["red"] for shot in shots),
        "restored": all(shot["restored"] for shot in shots),
        "shots": shots,
    }


# --- obligation 6: full regression vs the frozen baseline (S9) -----------


@dataclass
class SuiteSnapshot:
    """A machine-comparable full-suite snapshot: counts + the failed set.

    ``green_tests`` is the *optional* set of test names observed passing at the
    snapshot; it is what distinguishes a green->red flip from a red added to a
    baseline that was already red (spec item 2.6's four negative sub-cases).
    """

    passed: int
    failed: int
    total: int
    failed_tests: frozenset[str] = field(default_factory=frozenset)
    skipped: int = 0
    green_tests: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SuiteSnapshot:
        return cls(
            passed=int(raw.get("passed") or 0),
            failed=int(raw.get("failed") or 0),
            total=int(raw.get("total") or 0),
            skipped=int(raw.get("skipped") or 0),
            failed_tests=frozenset(str(t) for t in (raw.get("failed_tests") or [])),
            green_tests=frozenset(str(t) for t in (raw.get("green_tests") or [])),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "total": self.total,
            "skipped": self.skipped,
            "failed_tests": sorted(self.failed_tests),
            "green_tests": sorted(self.green_tests),
        }


def regression_verdict(
    baseline: SuiteSnapshot,
    head: SuiteSnapshot,
    *,
    flake_attribution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """S9 judgement: the red set must not grow; a green->red flip refuses.

    The baseline is anchored at the *frozen* ``target_base_commit`` (never the
    drifting main head). ``baseline`` may itself be red -- pre-existing red is
    not this single's fault (红->绿 is an improvement, never refused). The two
    root refusals are:

    - **red set grew** -- a test red on head that was not red on baseline
      (includes "基线本身红时再添新红");
    - **green->red flip** -- a test that was observed passing (green) on the
      baseline is now failed ("把绿的打红").

    ``flake_attribution`` carries the isolation re-run that cleared an
    intermittent red (``{"cleared": [test names]}``); those names are removed
    from the red increment before the pass is computed, but the attribution
    itself is carried into the verdict so the load stays evidence-backed.
    """
    baseline_red = set(baseline.failed_tests)
    head_red = set(head.failed_tests)
    red_growth = head_red - baseline_red
    green_to_red = head_red & set(baseline.green_tests)

    if flake_attribution:
        cleared = set(str(t) for t in flake_attribution.get("cleared") or [])
        red_growth = red_growth - cleared
        green_to_red = green_to_red - cleared

    verdict: dict[str, Any] = {
        "red_set_grew": bool(red_growth),
        "green_to_red_flip": bool(green_to_red),
        "red_growth": sorted(red_growth),
        "green_to_red": sorted(green_to_red),
        "baseline": baseline.as_dict(),
        "head": head.as_dict(),
    }
    if flake_attribution:
        verdict["flake_attribution"] = flake_attribution
    verdict["pass"] = not verdict["red_set_grew"] and not verdict["green_to_red_flip"]
    return verdict


__all__ = [
    "GATE_EVIDENCE_FIELDS",
    "MACHINE_PREFIXES",
    "GateEvidenceError",
    "GateEvidenceMissing",
    "SuiteSnapshot",
    "acceptance_equality",
    "diff_in_scope",
    "missing_gate_evidence",
    "mutation_gun",
    "regression_verdict",
    "require_gate_evidence",
    "rerun_acceptance",
    "two_shot_mutation_gun",
    "zero_test_deletion",
]
