"""B1 scope isolation: the declared B1-B3 boundary as an attributable refusal.

The B1-B3 development is isolated by a hard scope boundary. Any work item that
*actively* crosses it -- adding a phase outside the declared set, or reviving
work this development must not touch -- is refused, and the refusal names the
rule that did it (``RULE_ID``) rather than surfacing as whatever downstream
failure happened to fire first.

Two layers, and the split is the point:

- ``evaluate`` is the *mechanism*: a pure, structured comparison of a declared
  footprint against a boundary. No prose parsing, no guessing, no fallback to
  "allow".
- ``evaluate_text`` is the *detection*: it extracts the footprint a free-text
  work item claims, with a small, documented deferral recognition so that "B4
  is deferred / must not be implemented" (which respects the boundary) is not
  confused with "implement B4" (which crosses it).

The boundary is data, declared in one place. A future re-scope (B4 becomes
in scope) is an edit to ``default_boundary``, not a hunt through call sites --
and a refusal stays attributable to the rule, not to the caller's context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The attributable rule id. Every refusal carries this, never a bare error.
RULE_ID = "b1-scope-boundary"
SCOPE_LABEL = "B1-B3 isolated scope"

#: The phases this development is allowed to add.
SCOPE_PHASES = frozenset({"B1", "B2", "B3"})

#: katana work items this development must not revive, per the frozen scope.
FORBIDDEN_REVIVALS = frozenset({"katana#150", "katana#151"})

_PHASE_RE = re.compile(r"\bB(?P<n>[0-9]+)\b")
_REVIVAL_RE = re.compile(r"\bkatana#(?P<n>[0-9]+)\b")

#: How far either side of a match counts as "same context" for deferral.
DEFERRAL_WINDOW = 48

#: The deferral / out-of-scope vocabulary. A forbidden reference sitting next
#: to one of these is the boundary *speaking*, not the boundary being crossed.
_DEFERRAL_RE = re.compile(
    r"(?:defer|out of scope|non-?goals?|excluded|forbidden|"
    r"must not|do not|does not|will not|shall not|not in scope)",
    re.IGNORECASE,
)


class ScopeViolationError(RuntimeError):
    """A work item crosses the declared boundary; the refusal is attributable."""

    def __init__(self, boundary: ScopeBoundary, violations: tuple[ScopeViolation, ...]) -> None:
        self.rule_id = boundary.rule_id
        self.violations = violations
        detail = "; ".join(f"{violation.reference}: {violation.label}" for violation in violations)
        super().__init__(f"{boundary.rule_id}: {boundary.label} refused -- {detail}")


@dataclass(frozen=True)
class ScopeBoundary:
    """The declared scope a work item must stay inside."""

    rule_id: str
    label: str
    phases: frozenset[str]
    forbidden_revivals: frozenset[str]

    @property
    def allowed_phases(self) -> frozenset[str]:
        return self.phases


@dataclass(frozen=True)
class ScopeViolation:
    """One observed crossing, attributable rather than incidental."""

    reference: str
    label: str
    excerpt: str = ""


@dataclass(frozen=True)
class ScopeVerdict:
    """The result of evaluating a work item against a boundary."""

    admitted: bool
    rule_id: str = ""
    violations: tuple[ScopeViolation, ...] = ()
    observed_phases: tuple[str, ...] = ()
    observed_revivals: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "admitted": self.admitted,
            "rule_id": self.rule_id,
            "violations": [
                {
                    "reference": violation.reference,
                    "label": violation.label,
                    "excerpt": violation.excerpt,
                }
                for violation in self.violations
            ],
            "observed_phases": list(self.observed_phases),
            "observed_revivals": list(self.observed_revivals),
        }


def default_boundary() -> ScopeBoundary:
    """The frozen B1-B3 boundary this development lives under."""
    return ScopeBoundary(
        rule_id=RULE_ID,
        label=SCOPE_LABEL,
        phases=SCOPE_PHASES,
        forbidden_revivals=FORBIDDEN_REVIVALS,
    )


def _deferred(text: str, start: int, end: int) -> bool:
    """Whether the forbidden reference sits in a deferral / out-of-scope context.

    Only the immediate neighbourhood counts: the boundary is a hard edge, not a
    free-text classifier, and a word half a page away says nothing about this
    reference.
    """
    before = text[max(0, start - DEFERRAL_WINDOW) : start]
    after = text[end : min(len(text), end + DEFERRAL_WINDOW)]
    return bool(_DEFERRAL_RE.search(before) or _DEFERRAL_RE.search(after))


def _excerpt(text: str, start: int, end: int) -> str:
    left = text[max(0, start - DEFERRAL_WINDOW) : start]
    right = text[end : min(len(text), end + DEFERRAL_WINDOW)]
    return f"{left}{text[start:end]}{right}"


def evaluate(
    *, phases: tuple[str, ...], revives: tuple[str, ...], boundary: ScopeBoundary
) -> ScopeVerdict:
    """The structured mechanism: compare a declared footprint to the boundary.

    No prose, no patterns -- this is the pure comparison the free-text path is
    built on top of, and the one place the boundary is enforced by construction.
    """
    phases = tuple(phases or ())
    revives = tuple(revives or ())
    violations: list[ScopeViolation] = []
    for phase in sorted(set(phases)):
        if phase not in boundary.phases:
            violations.append(
                ScopeViolation(
                    reference=phase,
                    label=f"adds phase {phase} outside {boundary.label}",
                )
            )
    for revived in sorted(set(revives)):
        if revived in boundary.forbidden_revivals:
            violations.append(
                ScopeViolation(
                    reference=revived,
                    label=f"revives {revived}, forbidden by {boundary.label}",
                )
            )
    return ScopeVerdict(
        admitted=not violations,
        rule_id=boundary.rule_id,
        violations=tuple(violations),
        observed_phases=tuple(sorted(set(phases))),
        observed_revivals=tuple(sorted(set(revives))),
    )


def evaluate_text(text: str, boundary: ScopeBoundary | None = None) -> ScopeVerdict:
    """Detect an active boundary crossing in free text.

    A forbidden reference only counts when it is *active*: "B4 is deferred,
    must not be implemented" respects the boundary and is admitted; "implement
    B4" crosses it and is refused. The verdict always names the rule id.
    """
    boundary = boundary or default_boundary()
    observed_phases: list[str] = []
    observed_revivals: list[str] = []
    violations: list[ScopeViolation] = []

    for match in _PHASE_RE.finditer(text):
        phase = f"B{match.group('n')}"
        if phase in observed_phases:
            continue
        observed_phases.append(phase)
        if phase not in boundary.phases and not _deferred(text, match.start(), match.end()):
            violations.append(
                ScopeViolation(
                    reference=phase,
                    label=f"adds phase {phase} outside {boundary.label}",
                    excerpt=_excerpt(text, match.start(), match.end()),
                )
            )

    for match in _REVIVAL_RE.finditer(text):
        revived = f"katana#{match.group('n')}"
        if revived in observed_revivals:
            continue
        observed_revivals.append(revived)
        if revived in boundary.forbidden_revivals and not _deferred(
            text, match.start(), match.end()
        ):
            violations.append(
                ScopeViolation(
                    reference=revived,
                    label=f"revives {revived}, forbidden by {boundary.label}",
                    excerpt=_excerpt(text, match.start(), match.end()),
                )
            )

    return ScopeVerdict(
        admitted=not violations,
        rule_id=boundary.rule_id,
        violations=tuple(violations),
        observed_phases=tuple(sorted(observed_phases)),
        observed_revivals=tuple(sorted(observed_revivals)),
    )


def require_scope(text: str, boundary: ScopeBoundary | None = None) -> ScopeVerdict:
    """Evaluate and refuse by raising ``ScopeViolationError`` on a crossing."""
    boundary = boundary or default_boundary()
    verdict = evaluate_text(text, boundary)
    if not verdict.admitted:
        raise ScopeViolationError(boundary, verdict.violations)
    return verdict


__all__ = [
    "DEFERRAL_WINDOW",
    "FORBIDDEN_REVIVALS",
    "RULE_ID",
    "SCOPE_LABEL",
    "SCOPE_PHASES",
    "ScopeBoundary",
    "ScopeVerdict",
    "ScopeViolation",
    "ScopeViolationError",
    "default_boundary",
    "evaluate",
    "evaluate_text",
    "require_scope",
]
