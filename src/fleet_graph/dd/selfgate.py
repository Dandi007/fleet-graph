"""M3 line self-gate: six evidence duties the dispatching line fulfils at a dd gate.

design.md D5 puts the dd gate on the dispatching line, not on a human. The spec
(wf-8d9737 M3) turns that into six *mechanical* evidence duties the engine
collects and validates before a self-judged ``decision_deliver`` is admitted.
This module is the mechanical part: the closed duty vocabulary, the per-duty
evidence shape, the missing-duty refusal, the regression-vs-baseline judgement
(S9, not weakenable), and the ``merge-then-harvest`` trigger ordering (S7).
The LLM-free, deterministically-testable core -- the line's agent run wires its
evidence in here; this module never runs a subprocess or reads a worktree
itself, it only judges the facts it is handed.

The six duties, in order (a decision is admitted only when all six are present):

1. **acceptance_triple_equal** -- the frozen spec acceptance argv, record.json's
   ``acceptance_commands``, and the stage receipt's command are verbatim-equal
   (machine comparison).
2. **diff_within_spec_bounds** -- every changed product file maps to a path the
   spec declares; ``.dev-dispatch/`` and ``.dd-evidence/`` machine artifacts are
   out of scope by construction.
3. **zero_test_deletion** -- the ``base..head --diff-filter=D`` product set is
   empty (editing an assertion inside an existing test is not a deletion).
4. **self_run_acceptance** -- the line re-ran the frozen acceptance commands at
   the gate and kept the echo.
5. **mutation_gun** -- the product was mutated in two places and the frozen
   acceptance went red both times; the shots were byte-restored (sha + mode).
6. **regression_baseline** -- a full-suite snapshot on the frozen
   ``target_base_commit`` (never a drifted main head) whose red-set must not
   grow, with a flake-attribution escape hatch (S9).

Item 6 is the ``dd`` gate's duty, never part of a single's frozen acceptance:
the full-suite run is deliberately kept *out* of ``acceptance_commands`` so the
judgement criterion stays frozen while main drifts (S9's own warning).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from typing import Any

#: The six duties, in the order the spec lists them. The order is load-bearing:
#: it keys the ``rationale`` payload and the missing-duty refusal message.
DUTY_ACCEPTANCE_TRIPLE_EQUAL = "acceptance_triple_equal"
DUTY_DIFF_WITHIN_SPEC_BOUNDS = "diff_within_spec_bounds"
DUTY_ZERO_TEST_DELETION = "zero_test_deletion"
DUTY_SELF_RUN_ACCEPTANCE = "self_run_acceptance"
DUTY_MUTATION_GUN = "mutation_gun"
DUTY_REGRESSION_BASELINE = "regression_baseline"

EVIDENCE_DUTIES: tuple[str, ...] = (
    DUTY_ACCEPTANCE_TRIPLE_EQUAL,
    DUTY_DIFF_WITHIN_SPEC_BOUNDS,
    DUTY_ZERO_TEST_DELETION,
    DUTY_SELF_RUN_ACCEPTANCE,
    DUTY_MUTATION_GUN,
    DUTY_REGRESSION_BASELINE,
)

#: The machine artifacts that carry no product meaning and therefore never count
#: as an out-of-bounds edit. Byte-identical to the controller-reserved namespace.
MACHINE_PATHS: tuple[str, ...] = (".dev-dispatch", ".dd-evidence")

#: The mutation gun must fire exactly two shots; one shot is not an attack.
MUTATION_SHOTS_REQUIRED = 2

#: Self-gate refusal codes (closed). ``decide`` returns one of these when the
#: self-judged delivery is not admitted, distinct from the M2 delivery refusals.
CODE_SELFGATE_INCOMPLETE = "SELFGATE_INCOMPLETE"
CODE_SELFGATE_REGRESSION = "SELFGATE_REGRESSION"
CODE_SELFGATE_BASELINE_UNANCHORED = "SELFGATE_BASELINE_UNANCHORED"

#: The specified verdict vocabulary -- the decision this gate carries is still
#: APPROVE / REJECT, never anything else.
APPROVE = "APPROVE"
REJECT = "REJECT"
ALLOWED_DECISIONS: frozenset[str] = frozenset({APPROVE, REJECT})


@dataclass(frozen=True)
class RegressionBaseline:
    """The frozen-base full-suite snapshot the gate compares against (S9).

    ``target_base_commit`` is the single's frozen ``record.json.target_base_commit``
    -- never the main head at gate time, which drifts under the single (M1's
    daemon.py:93, M2's #250). The snapshot is the two-tuple the spec mandates:
    the pass/fail/skip counts plus the failed-test set. Missing/absent is a
    refusal, not a default of zero.
    """

    target_base_commit: str
    passed: int
    failed: int
    skipped: int
    failed_tests: frozenset[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_base_commit": self.target_base_commit,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "failed_tests": sorted(self.failed_tests),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RegressionBaseline | None:
        """Parse a baseline snapshot; any missing/malformed field means None.

        None is the "missing baseline fields" negative (S9 item 6 first 款): a
        baseline with absent counts or an absent failed-test set is not a
        baseline at all, and must refuse rather than be read as "all green".
        """
        if not isinstance(raw, Mapping):
            return None
        commit = raw.get("target_base_commit")
        if not isinstance(commit, str) or not commit.strip():
            return None
        try:
            passed = int(raw.get("passed"))
            failed = int(raw.get("failed"))
            skipped = int(raw.get("skipped"))
        except (TypeError, ValueError):
            return None
        tests = raw.get("failed_tests")
        if not isinstance(tests, Sequence) or isinstance(tests, (str, bytes)):
            return None
        for test in tests:
            if not isinstance(test, str):
                return None
        return cls(
            target_base_commit=commit.strip(),
            passed=passed,
            failed=failed,
            skipped=skipped,
            failed_tests=frozenset(tests),
        )


@dataclass(frozen=True)
class RegressionVerdict:
    """The item-6 judgement: did the red set grow, and can a flake explain it."""

    acceptable: bool
    #: Tests red now that were not red on the baseline (the red increment).
    new_red: frozenset[str] = frozenset()
    #: The subset of the red increment attributed to net-base intermittent red
    #: via an isolated re-run; present in the payload as the flake evidence.
    flaky_attributed: frozenset[str] = frozenset()
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "acceptable": self.acceptable,
            "new_red": sorted(self.new_red),
            "flaky_attributed": sorted(self.flaky_attributed),
            "reason": self.reason,
        }


def compare_regression(
    baseline: RegressionBaseline | None,
    current_failed_tests: Set[str],
    *,
    flaky_tests: Set[str] = frozenset(),
    flaky_attribution: Set[str] = frozenset(),
) -> RegressionVerdict:
    """The S9 judgement: red-set must not grow; a flake must be attributed.

    - ``baseline is None`` (missing baseline fields) refuses; a baseline with
      its own reds is *not* a refusal -- those reds are not this single's fault.
    - the red increment is ``current_failed_tests - baseline.failed_tests``;
      an empty increment passes no matter how red the baseline was.
    - a non-empty increment passes only when every newcomer is a known
      net-base-intermittent red (``flaky_tests``) *and* an isolated re-run
      already attributed it (``flaky_attribution`` names the same tests). The
      attribution evidence rides the payload, never silently dropped.
    - anything else -- a green test flipped red, or a new red atop an
      already-red baseline -- is a refusal (红项集合扩大 / 绿→红翻转).
    """
    if baseline is None:
        return RegressionVerdict(
            acceptable=False, reason="regression baseline is missing (no counts or failed set)"
        )
    new_red = frozenset(current_failed_tests) - baseline.failed_tests
    if not new_red:
        return RegressionVerdict(acceptable=True, reason="red set did not grow")
    attributed = frozenset(flaky_attribution)
    flaky = frozenset(flaky_tests)
    unattributed = (new_red - flaky) | (new_red & flaky - attributed)
    if not unattributed:
        return RegressionVerdict(
            acceptable=True,
            new_red=new_red,
            flaky_attributed=new_red,
            reason="red increment is net-base intermittent red, attributed by isolated re-run",
        )
    return RegressionVerdict(
        acceptable=False,
        new_red=new_red,
        flaky_attributed=new_red - unattributed,
        reason=(
            "red set grew: "
            + ", ".join(sorted(unattributed))
            + " (a green test flipped red, or a new red appeared atop an already-red baseline)"
        ),
    )


@dataclass(frozen=True)
class AcceptanceTriple:
    """Duty 1 evidence: the three argv sets that must be verbatim-equal."""

    spec_argv: tuple[tuple[str, ...], ...]
    record_argv: tuple[tuple[str, ...], ...]
    receipt_argv: tuple[tuple[str, ...], ...]

    def equal(self) -> bool:
        return self.spec_argv == self.record_argv == self.receipt_argv


@dataclass(frozen=True)
class DiffBoundary:
    """Duty 2 evidence: did every product edit stay inside the spec's surface."""

    changed_product_paths: tuple[str, ...]
    spec_declared_paths: tuple[str, ...]

    def out_of_bounds(self) -> tuple[str, ...]:
        out: list[str] = []
        for path in self.changed_product_paths:
            if path in MACHINE_PATHS or path.startswith(tuple(p + "/" for p in MACHINE_PATHS)):
                continue
            if not _within_declared(path, self.spec_declared_paths):
                out.append(path)
        return tuple(out)


def _within_declared(path: str, declared: tuple[str, ...]) -> bool:
    """Is ``path`` itself (or a child of) any declared spec surface? Prefix match."""
    for surface in declared:
        if path == surface:
            return True
        if path.startswith(surface.rstrip("/") + "/"):
            return True
    return False


@dataclass(frozen=True)
class SelfRun:
    """Duty 4 evidence: the line's own gate-side acceptance re-run echo."""

    argv: tuple[str, ...]
    exit_code: int
    tail: str = ""

    @property
    def ran(self) -> bool:
        return bool(self.argv)


@dataclass(frozen=True)
class MutationShot:
    """One mutation-gun shot: what was mutated, and that acceptance went red."""

    index: int
    mutator: str
    acceptance_exit_code: int

    @property
    def turned_acceptance_red(self) -> bool:
        return self.acceptance_exit_code != 0


@dataclass(frozen=True)
class MutationGun:
    """Duty 5 evidence: two shots, both red, then a byte-perfect restore."""

    shots: tuple[MutationShot, ...] = ()
    restored_sha: str = ""
    expected_sha: str = ""
    restored_mode_ok: bool = True

    def shot_count(self) -> int:
        return len(self.shots)

    def all_red(self) -> bool:
        return bool(self.shots) and all(shot.turned_acceptance_red for shot in self.shots)

    def restored_intact(self) -> bool:
        return (
            bool(self.restored_sha)
            and self.restored_sha == self.expected_sha
            and (self.restored_mode_ok)
        )


@dataclass(frozen=True)
class SelfGateEvidence:
    """The six duties, each a typed fact. Absent duty == a missing-field refusal.

    ``None`` / empty on any duty means that duty was not performed, which is
    itself the negative the gate must refuse: a line that skips one of the six
    cannot self-judge. Vacuous evidence (an empty triple, zero shots, no
    self-run argv) is treated exactly like the missing field it masquerades as.
    """

    principal: str = ""
    dispatched_by: str = ""
    decision: str = ""
    #: The single's frozen ``record.json.target_base_commit``. The regression
    #: baseline must be anchored to it -- a baseline captured against a drifted
    #: main head is the S9 "drifted main as baseline" negative and refuses.
    target_base_commit: str = ""
    acceptance_triple: AcceptanceTriple | None = None
    diff_boundary: DiffBoundary | None = None
    zero_test_deletion: tuple[str, ...] | None = None
    self_run: SelfRun | None = None
    mutation_gun: MutationGun | None = None
    regression_baseline: RegressionBaseline | None = None
    current_failed_tests: frozenset[str] = frozenset()
    flaky_tests: frozenset[str] = frozenset()
    flaky_attribution: frozenset[str] = frozenset()

    def missing_duties(self) -> tuple[str, ...]:
        """The duties absent (or vacuous) from this evidence, in declared order."""
        missing: list[str] = []
        if self.acceptance_triple is None or not self.acceptance_triple.equal():
            missing.append(DUTY_ACCEPTANCE_TRIPLE_EQUAL)
        boundary = self.diff_boundary
        if boundary is None or boundary.out_of_bounds():
            missing.append(DUTY_DIFF_WITHIN_SPEC_BOUNDS)
        if self.zero_test_deletion is None or self.zero_test_deletion:
            missing.append(DUTY_ZERO_TEST_DELETION)
        if self.self_run is None or not self.self_run.ran:
            missing.append(DUTY_SELF_RUN_ACCEPTANCE)
        gun = self.mutation_gun
        if (
            gun is None
            or gun.shot_count() != MUTATION_SHOTS_REQUIRED
            or not gun.all_red()
            or not gun.restored_intact()
        ):
            missing.append(DUTY_MUTATION_GUN)
        if self.regression_baseline is None:
            missing.append(DUTY_REGRESSION_BASELINE)
        return tuple(missing)


@dataclass(frozen=True)
class SelfGateResult:
    """The self-judged answer: admitted or refused, with the rationale payload.

    ``rationale`` is the templated six-duty evidence+conclusion record the spec
    item 4 wants landed on progress and the evidence note; its keys follow
    ``EVIDENCE_DUTIES`` order so the payload is stable and diffable.
    """

    outcome: str  # "approve" | "reject" | "refused"
    code: str = ""
    reason: str = ""
    rationale: dict[str, Any] = field(default_factory=dict)

    @property
    def admitted(self) -> bool:
        return self.outcome == "approve"


def _decision_payloads(evidence: SelfGateEvidence) -> dict[str, Any]:
    """Templated rationale payload (item 4), keyed in duty order."""
    triple = evidence.acceptance_triple
    boundary = evidence.diff_boundary
    gun = evidence.mutation_gun
    self_run = evidence.self_run
    baseline = evidence.regression_baseline
    regression = compare_regression(
        baseline,
        evidence.current_failed_tests,
        flaky_tests=evidence.flaky_tests,
        flaky_attribution=evidence.flaky_attribution,
    )
    return {
        DUTY_ACCEPTANCE_TRIPLE_EQUAL: {
            "equal": triple.equal() if triple is not None else False,
            "spec_argv": [list(a) for a in triple.spec_argv] if triple is not None else None,
            "record_argv": [list(a) for a in triple.record_argv] if triple is not None else None,
            "receipt_argv": [list(a) for a in triple.receipt_argv] if triple is not None else None,
        },
        DUTY_DIFF_WITHIN_SPEC_BOUNDS: {
            "changed_paths": list(boundary.changed_product_paths) if boundary is not None else None,
            "out_of_bounds": list(boundary.out_of_bounds()) if boundary is not None else None,
        },
        DUTY_ZERO_TEST_DELETION: {
            "deleted_paths": list(evidence.zero_test_deletion or ()),
        },
        DUTY_SELF_RUN_ACCEPTANCE: (
            {
                "argv": list(self_run.argv),
                "exit_code": self_run.exit_code,
                "tail": self_run.tail,
            }
            if self_run is not None
            else None
        ),
        DUTY_MUTATION_GUN: (
            {
                "shots": [
                    {
                        "index": shot.index,
                        "mutator": shot.mutator,
                        "acceptance_exit_code": shot.acceptance_exit_code,
                        "turned_acceptance_red": shot.turned_acceptance_red,
                    }
                    for shot in gun.shots
                ],
                "restored_intact": gun.restored_intact(),
                "restored_sha": gun.restored_sha,
                "expected_sha": gun.expected_sha,
                "restored_mode_ok": gun.restored_mode_ok,
            }
            if gun is not None
            else None
        ),
        DUTY_REGRESSION_BASELINE: {
            "baseline": baseline.as_dict() if baseline is not None else None,
            "verdict": regression.as_dict(),
        },
        "decided_by": evidence.principal,
        "dispatched_by": evidence.dispatched_by,
    }


def decide(evidence: SelfGateEvidence) -> SelfGateResult:
    """The engine-side self-gate: admit only a complete, in-bounds self-judgement.

    The three refusals, in order:
    1. the principal is not the dispatching line (design §6.4 / M2's
       ``NOT_DISPATCHING_LINE``), or the decision is not APPROVE/REJECT;
    2. a duty is missing or vacuous (SELFGATE_INCOMPLETE);
    3. the regression judgement is unacceptable (SELFGATE_REGRESSION), or the
       baseline is anchored to a drifted main instead of the frozen
       ``target_base_commit`` (SELFGATE_BASELINE_UNANCHORED).

    A REJECT decision with complete evidence is admitted: the line judged the
    single REJECT against the facts -- it is not a refusal, it is a verdict.
    """
    if evidence.decision not in ALLOWED_DECISIONS:
        return SelfGateResult(
            outcome="refused",
            code=CODE_SELFGATE_INCOMPLETE,
            reason=f"decision must be APPROVE or REJECT, got {evidence.decision!r}",
        )
    if evidence.principal != evidence.dispatched_by:
        return SelfGateResult(
            outcome="refused",
            code=CODE_SELFGATE_INCOMPLETE,
            reason=(
                f"principal {evidence.principal!r} is not the dispatching line "
                f"{evidence.dispatched_by!r}"
            ),
        )

    missing = evidence.missing_duties()
    if missing:
        return SelfGateResult(
            outcome="refused",
            code=CODE_SELFGATE_INCOMPLETE,
            reason="missing self-gate evidence duties: " + ", ".join(missing),
        )

    baseline = evidence.regression_baseline
    assert baseline is not None
    if baseline.target_base_commit != evidence.target_base_commit:
        return SelfGateResult(
            outcome="refused",
            code=CODE_SELFGATE_BASELINE_UNANCHORED,
            reason=(
                f"regression baseline anchored to {baseline.target_base_commit!r}, "
                f"not the frozen target base {evidence.target_base_commit!r}"
            ),
        )
    regression = compare_regression(
        baseline,
        evidence.current_failed_tests,
        flaky_tests=evidence.flaky_tests,
        flaky_attribution=evidence.flaky_attribution,
    )
    if not regression.acceptable:
        return SelfGateResult(
            outcome="refused",
            code=CODE_SELFGATE_REGRESSION,
            reason=regression.reason,
            rationale=_decision_payloads(evidence),
        )

    outcome = "approve" if evidence.decision == APPROVE else "reject"
    return SelfGateResult(
        outcome=outcome,
        reason=f"self-gate {outcome}d for {evidence.dispatched_by}",
        rationale=_decision_payloads(evidence),
    )


def _argv_sets(raw: Any) -> tuple[tuple[str, ...], ...] | None:
    """A JSON list of argv lists -> tuple of tuples; malformed -> None.

    Each argv is a list of strings. A present-but-empty list yields ``()``;
    an absent/malformed value yields None so the caller can tell "vacuous"
    (empty triple) from "missing" (no field).
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    sets: list[tuple[str, ...]] = []
    for entry in raw:
        if not isinstance(entry, Sequence) or isinstance(entry, (str, bytes)):
            return None
        if any(not isinstance(part, str) for part in entry):
            return None
        sets.append(tuple(str(part) for part in entry))
    return tuple(sets)


def _str_list(raw: Any) -> tuple[str, ...] | None:
    """A JSON list of strings -> tuple; malformed/absent -> None."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    if any(not isinstance(item, str) for item in raw):
        return None
    return tuple(str(item) for item in raw)


def _str(raw: Any) -> str | None:
    return raw if isinstance(raw, str) else None


def _int(raw: Any) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def parse_self_gate_evidence(raw: Mapping[str, Any]) -> SelfGateEvidence | None:
    """Parse a JSON-serializable six-duty evidence object into typed evidence.

    The engine's default delivery surface (``decision_deliver``) receives the
    dispatching line's assembled evidence as a plain JSON object; this converts
    it into the typed :class:`SelfGateEvidence` the gate judges. Any missing or
    malformed field yields ``None`` -- the caller refuses (SELFGATE_INCOMPLETE),
    never guesses a default that would let a vacuous duty pass. Each of the six
    duties is parsed independently, so an absent duty lands as ``None`` and is
    judged missing exactly as if it had never been supplied.
    """
    if not isinstance(raw, Mapping):
        return None

    principal = _str(raw.get("principal"))
    dispatched_by = _str(raw.get("dispatched_by"))
    decision = _str(raw.get("decision"))
    target_base_commit = _str(raw.get("target_base_commit"))
    if principal is None or dispatched_by is None or decision is None or target_base_commit is None:
        return None

    fields: dict[str, Any] = {
        "principal": principal,
        "dispatched_by": dispatched_by,
        "decision": decision,
        "target_base_commit": target_base_commit,
    }

    triple_raw = raw.get("acceptance_triple")
    if isinstance(triple_raw, Mapping):
        spec_argv = _argv_sets(triple_raw.get("spec_argv"))
        record_argv = _argv_sets(triple_raw.get("record_argv"))
        receipt_argv = _argv_sets(triple_raw.get("receipt_argv"))
        fields["acceptance_triple"] = (
            AcceptanceTriple(
                spec_argv=spec_argv, record_argv=record_argv, receipt_argv=receipt_argv
            )
            if spec_argv is not None and record_argv is not None and receipt_argv is not None
            else None
        )
    else:
        fields["acceptance_triple"] = None

    boundary_raw = raw.get("diff_boundary")
    if isinstance(boundary_raw, Mapping):
        changed = _str_list(boundary_raw.get("changed_product_paths"))
        declared = _str_list(boundary_raw.get("spec_declared_paths"))
        fields["diff_boundary"] = (
            DiffBoundary(changed_product_paths=changed, spec_declared_paths=declared)
            if changed is not None and declared is not None
            else None
        )
    else:
        fields["diff_boundary"] = None

    fields["zero_test_deletion"] = _str_list(raw.get("zero_test_deletion"))

    self_run_raw = raw.get("self_run")
    if isinstance(self_run_raw, Mapping):
        argv = _str_list(self_run_raw.get("argv"))
        exit_code = _int(self_run_raw.get("exit_code"))
        tail = self_run_raw.get("tail")
        fields["self_run"] = (
            SelfRun(argv=argv, exit_code=exit_code, tail=str(tail) if isinstance(tail, str) else "")
            if argv is not None and exit_code is not None
            else None
        )
    else:
        fields["self_run"] = None

    gun_raw = raw.get("mutation_gun")
    if isinstance(gun_raw, Mapping):
        shots_raw = gun_raw.get("shots")
        shots: list[MutationShot] = []
        shots_ok = isinstance(shots_raw, Sequence) and not isinstance(shots_raw, (str, bytes))
        if shots_ok:
            for entry in shots_raw:
                if not isinstance(entry, Mapping):
                    shots_ok = False
                    break
                index = _int(entry.get("index"))
                mutator = _str(entry.get("mutator"))
                acceptance_exit_code = _int(entry.get("acceptance_exit_code"))
                if index is None or mutator is None or acceptance_exit_code is None:
                    shots_ok = False
                    break
                shots.append(
                    MutationShot(
                        index=index,
                        mutator=mutator,
                        acceptance_exit_code=acceptance_exit_code,
                    )
                )
        restored_sha = _str(gun_raw.get("restored_sha"))
        expected_sha = _str(gun_raw.get("expected_sha"))
        mode_ok = gun_raw.get("restored_mode_ok", True)
        fields["mutation_gun"] = (
            MutationGun(
                shots=tuple(shots),
                restored_sha=restored_sha or "",
                expected_sha=expected_sha or "",
                restored_mode_ok=bool(mode_ok),
            )
            if shots_ok
            else None
        )
    else:
        fields["mutation_gun"] = None

    baseline_raw = raw.get("regression_baseline")
    fields["regression_baseline"] = (
        RegressionBaseline.from_dict(baseline_raw) if isinstance(baseline_raw, Mapping) else None
    )

    fields["current_failed_tests"] = frozenset(_str_list(raw.get("current_failed_tests")) or ())
    fields["flaky_tests"] = frozenset(_str_list(raw.get("flaky_tests")) or ())
    fields["flaky_attribution"] = frozenset(_str_list(raw.get("flaky_attribution")) or ())

    return SelfGateEvidence(**fields)


def harvest_trigger(decision: str, *, merged: bool) -> bool:
    """S7: the harvest reactor fires only after merge, never straight off the gate.

    A gate APPROVE that is not yet merged must *not* harvest -- the trigger point
    moved from "闸后" to "merge 后". Only an APPROVE whose merge completed fires
    the harvest. A REJECT never harvests. Alongside ``merge_then_harvest`` this
    is the ordering contract the dispatcher's scheduler wiring honours.
    """
    return decision == APPROVE and merged


def merge_then_harvest(decision: str, merged: bool) -> bool:
    """Alias for :func:`harvest_trigger`, kept under the spec's wording."""
    return harvest_trigger(decision, merged=merged)


__all__ = [
    "ALLOWED_DECISIONS",
    "APPROVE",
    "CODE_SELFGATE_BASELINE_UNANCHORED",
    "CODE_SELFGATE_INCOMPLETE",
    "CODE_SELFGATE_REGRESSION",
    "DUTY_ACCEPTANCE_TRIPLE_EQUAL",
    "DUTY_DIFF_WITHIN_SPEC_BOUNDS",
    "DUTY_MUTATION_GUN",
    "DUTY_REGRESSION_BASELINE",
    "DUTY_SELF_RUN_ACCEPTANCE",
    "DUTY_ZERO_TEST_DELETION",
    "EVIDENCE_DUTIES",
    "MACHINE_PATHS",
    "MUTATION_SHOTS_REQUIRED",
    "REJECT",
    "AcceptanceTriple",
    "DiffBoundary",
    "MutationGun",
    "MutationShot",
    "RegressionBaseline",
    "RegressionVerdict",
    "SelfGateEvidence",
    "SelfGateResult",
    "SelfRun",
    "compare_regression",
    "decide",
    "harvest_trigger",
    "merge_then_harvest",
    "parse_self_gate_evidence",
]
