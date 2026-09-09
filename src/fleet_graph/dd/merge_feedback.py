"""Typed merge feedback (spec L6).

The merge stage can decline in several *different* ways, and the difference is
load-bearing: a target that moved is not a content conflict, a transport
failure is not a verdict, and `PREPARED` is not a merge. Conflating them is how
a line that raced another order reports a "conflict" that is really a relay, or
a network hiccup reads as "your work was rejected".

So the merge outcome is carried as a *typed* result. Each kind maps to the
structured code the existing `MergeStage` already raises, so nothing about the
existing CAS protection changes (spec L6: keep the CAS, do not extend a real
PR platform's algorithm) -- this module only names the distinction the CAS
already produces.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MergeFeedbackKind(StrEnum):
    # The target moved under the order (another line advanced the release
    # branch). A relay, not a judgement on the work: the line re-runs configure.
    TARGET_COMPETITION = "target_competition"

    # A real content conflict: the work cannot land on the base as-is and needs
    # a code change -- this is the merge feedback that returns to implement.
    CONTENT_CONFLICT = "content_conflict"

    # Transport / unknown: the remote could not be reached or the outcome is
    # unknowable. Never a business verdict, never a fault of the work.
    TRANSPORT_UNKNOWN = "transport_unknown"

    # The work is already on the target: nothing to do, and crucially not an
    # error.
    ALREADY_MERGED = "already_merged"

    # The stage was only prepared, not merged. `PREPARED` is a first-class
    # result, never a success the way a measured merge is.
    PREPARED_ONLY = "prepared"

    # A measured merge: the CAS fast-forward actually landed.
    MERGED = "merged"


@dataclass(frozen=True)
class MergeFeedback:
    kind: MergeFeedbackKind
    detail: str = ""
    # True when the resolution is "fix the product code and re-implement" --
    # the merge feedback that returns to implement under spec L2.
    requires_code_change: bool = False
    # The structured refusal code the stage would carry, when one applies.
    code: str = ""


# The existing structured codes, mapped to the typed distinction the spec
# demands. A code absent from this table classifies as transport/unknown --
# unknown is deliberately the fallback, because guessing "conflict" is how a
# network failure reads as a rejected delivery.
_CODE_KINDS: dict[str, MergeFeedbackKind] = {
    # Target advanced past the frozen base: a race, remedied by re-dispatch.
    "RELEASE_HEAD_ADVANCED": MergeFeedbackKind.TARGET_COMPETITION,
    # A genuine rebase/fast-forward incompatibility on the content.
    "REBASE_SPEC_INCOMPATIBLE": MergeFeedbackKind.CONTENT_CONFLICT,
    # Already there: the CAS found the remote already at the handoff commit.
    "ALREADY_MERGED": MergeFeedbackKind.ALREADY_MERGED,
    # Transport-class failures surface as provider/target unavailable.
    "PROVIDER_UNAVAILABLE": MergeFeedbackKind.TRANSPORT_UNKNOWN,
}


#: Codes whose meaning is "the content must change and return to implement".
_CONTENT_CODES = frozenset({"REBASE_SPEC_INCOMPATIBLE"})


def classify_merge_feedback(code: str = "", detail: str = "", *, result: str = "") -> MergeFeedback:
    """Classify a merge stage outcome.

    ``result`` names the stage's own result (`MERGED` / `PREPARED`); ``code``
    and ``detail`` name the structured refusal, when the stage declined. The
    combination is what makes the distinction: `PREPARED` with no code is
    prepared-only, `MERGED` is a measured merge, and a refusal code picks its
    kind from the table above (falling back to transport/unknown so that an
    unknown failure is never misread as a content conflict).
    """
    if result == "MERGED":
        return MergeFeedback(kind=MergeFeedbackKind.MERGED, detail=detail)
    if result == "PREPARED":
        return MergeFeedback(kind=MergeFeedbackKind.PREPARED_ONLY, detail=detail)
    kind = _CODE_KINDS.get(code, MergeFeedbackKind.TRANSPORT_UNKNOWN)
    return MergeFeedback(
        kind=kind,
        detail=detail,
        requires_code_change=code in _CONTENT_CODES,
        code=code,
    )


def is_merge_success(feedback: MergeFeedback) -> bool:
    """True only for a measured merge -- `PREPARED` is deliberately not it."""
    return feedback.kind == MergeFeedbackKind.MERGED


def requires_rework(feedback: MergeFeedback) -> bool:
    """True when the merge feedback must return to implement (a code change)."""
    return feedback.kind == MergeFeedbackKind.CONTENT_CONFLICT or feedback.requires_code_change


BOUNDARY_CODES = (
    "RELEASE_HEAD_ADVANCED",
    "REBASE_SPEC_INCOMPATIBLE",
    "ALREADY_MERGED",
    "PROVIDER_UNAVAILABLE",
)

__all__ = [
    "BOUNDARY_CODES",
    "MergeFeedback",
    "MergeFeedbackKind",
    "classify_merge_feedback",
    "is_merge_success",
    "requires_rework",
]
