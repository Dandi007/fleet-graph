"""Line-completion gate: the mechanical "is the product on the default branch" test.

The single-level over-gating / line-level no-gating inversion (goal.md 16:4x G2):
a line declaring ``terminal=done`` used to retire immediately, on the line's own
word. Two real deliveries -- ``dev-fg-81dbb77434fa`` (253 lines added, zero on
canonical, four days stale) and ``dev-fg-b0ea914caf0e`` (102 lines, only 2/8
present) -- were approved and ``complete`` yet never entered the default branch.
This module is the ``本线`` half of the fix: the *mechanical judgement* of
whether the product is really on the default branch, computed from facts the
supervision surface measured. The supervision surface still runs the git
commands (fetch canonical, reverse-apply the patch, grep the feature lines); it
hands the measurements here and this function returns the verdict, never doing
git itself.

Two usable methods, both mechanical and both already proven on the real fleet:

- **reverse-apply**: if the product's patch cleanly reverses off the target
  tree, the product is already there. Positive, and doesn't need the grep.
- **feature-grep**: ``grep`` the product's *newly-added feature lines* in the
  target branch. Every line present is evidence; a line absent is a gap.

Two methods are explicitly *not* used, because both were disproven:

- ``git merge-base --is-ancestor``: squash merges make it systematically
  false-negative -- an already-merged product reads as "not present".
- whole-file content comparison: systematically false-positive -- a file whose
  other edits drifted reads as "present".

And the canonical branch is reached by ``remote_url``, never
``git rev-parse --git-common-dir`` (for a worktree nested in a worktree that
points at the middle tree, not the real one).

Negative is never weakened: a verdict is positive only on positive evidence.
``on_default_branch`` is ``True`` only when the patch reverse-applies cleanly,
or when every counted feature line was found. Anything else -- a gap, or no
feature lines to count and no clean reverse-apply -- is negative, and the
verdict names the development and the missing lines.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: The reverse-apply method: the product patch reverses cleanly off the target.
METHOD_REVERSE_APPLY = "reverse-apply"
#: The feature-grep method: the product's added lines are grepped on the target.
METHOD_FEATURE_GREP = "feature-grep"
#: What the daemon records when the gate itself could not answer (fail-closed:
#: no confirmation means no retirement).
METHOD_UNVERIFIED = "unverified"


@dataclass(frozen=True)
class CompletionVerdict:
    """Whether one ``done`` line's product is really on the default branch.

    ``found`` / ``total`` count feature lines on the target branch; alongside
    ``missing`` they name the gap when the verdict is negative. ``method`` says
    which of the two usable tests settled the outcome, so a reader can tell
    "proven by reverse-apply" from "proven by grep" from "could not verify".
    """

    development_id: str
    on_default_branch: bool
    found: int
    total: int
    missing: tuple[str, ...] = ()
    method: str = METHOD_FEATURE_GREP
    reverse_applies_cleanly: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "development_id": self.development_id,
            "on_default_branch": self.on_default_branch,
            "found": self.found,
            "total": self.total,
            "missing": list(self.missing),
            "method": self.method,
            "reverse_applies_cleanly": self.reverse_applies_cleanly,
        }


def product_on_default_branch(
    development_id: str,
    feature_lines: Sequence[str],
    *,
    found_lines: Sequence[str] = (),
    reverse_applies_cleanly: bool | None = None,
) -> CompletionVerdict:
    """The mechanical product-on-default-branch judgement, from measured facts.

    ``feature_lines`` are the product's newly-added feature lines (the grep
    targets); ``found_lines`` the subset the supervision surface's grep found on
    the target branch; ``reverse_applies_cleanly`` whether the product patch
    cleanly reverses off the target. None of these are derived from the dd
    record's ``terminal`` -- a ``complete`` record alone is never proof the
    branch has the product, which is exactly the mutation the regression suite
    guards against.

    Positive only on positive evidence: a clean reverse-apply, or every counted
    feature line found. A gap, or an empty product with no reverse-apply, is a
    negative verdict naming the development and the missing lines.
    """
    total = len(feature_lines)
    if reverse_applies_cleanly is True:
        return CompletionVerdict(
            development_id=development_id,
            on_default_branch=True,
            found=total,
            total=total,
            method=METHOD_REVERSE_APPLY,
            reverse_applies_cleanly=True,
        )
    found_set = set(found_lines)
    missing = tuple(line for line in feature_lines if line not in found_set)
    found = total - len(missing)
    on_default_branch = bool(feature_lines) and not missing
    return CompletionVerdict(
        development_id=development_id,
        on_default_branch=on_default_branch,
        found=found,
        total=total,
        missing=missing,
        method=METHOD_FEATURE_GREP,
        reverse_applies_cleanly=reverse_applies_cleanly,
    )


__all__ = [
    "METHOD_FEATURE_GREP",
    "METHOD_REVERSE_APPLY",
    "METHOD_UNVERIFIED",
    "CompletionVerdict",
    "product_on_default_branch",
]
