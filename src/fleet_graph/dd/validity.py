"""Version-bound validity key (spec L3).

A DD stage is valid only while every input it was bound to still holds. The
validity key is that binding, reduced to one canonical digest plus the fields
that produced it, so a later stage can detect *which* input changed rather than
only *that* something changed.

The key binds at least:

- the product revision (full git commit, never an agent's self-report) and its
  tree;
- the SPEC digest (the frozen spec's own identity -- a SPEC change is a new DD,
  not a reconfigure);
- the acceptance-context revision (the `run-config` the acceptance commands are
  graded against);
- the target identity (the release branch / durable ref the merge lands on);
- the PR identity (the head/base pair the merge is authorized against).

Two properties make this useful rather than merely bookkeeping:

1. **Any relevant change invalidates.** Renumbering a commit while meaning
   the work, or bumping an attempt without touching the tree, must not mint
   endless re-reviews: the key binds meaningful facts, and pure bookkeeping
   commits (an empty machine-part commit, a receipt re-write) produce no change
   to the bound fields, so they do not invalidate anything.
2. **Invalidation is per-stage.** A target change invalidates the merge
   authorization but not the implement's acceptance result; a spec change
   invalidates everything (it is a new DD). `affected_stages` names that
   mapping, so invalidation triggers a re-verify/re-review of the affected
   stages only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

# The bound fields, in their canonical order. The digest is computed over this
# exact ordering, so a different ordering is a digest of something else.
_BOUND_FIELDS = (
    "product_revision",
    "product_tree",
    "spec_digest",
    "acceptance_context_revision",
    "target_identity",
    "pr_identity",
)

#: A field that invalidates every stage downstream of the implementation: the
#: spec is the frozen root input, changing it is a new DD, not a rework.
SPEC_FIELD = "spec_digest"


def _digest(canonical_text: str) -> str:
    return "sha256:" + hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidityInputs:
    """The facts a validity key binds. Every field is a string; an empty string
    means "not known yet", which is still bound (and detects the transition
    from unknown -> known), never silently skipped."""

    product_revision: str = ""
    product_tree: str = ""
    spec_digest: str = ""
    acceptance_context_revision: str = ""
    target_identity: str = ""
    pr_identity: str = ""

    def as_tuple(self) -> tuple[str, ...]:
        return tuple(str(getattr(self, name)) for name in _BOUND_FIELDS)

    def canonical_json(self) -> str:
        return json.dumps(
            {name: getattr(self, name) for name in _BOUND_FIELDS},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class ValidityKey:
    """A frozen binding, plus its canonical digest.

    The digest is the thing a receipt carries and later re-computes; the
    inputs are the fields a verifier diffs to name *what* changed.
    """

    inputs: ValidityInputs
    digest: str

    @property
    def fields(self) -> dict[str, str]:
        return {name: getattr(self.inputs, name) for name in _BOUND_FIELDS}


def build_validity_key(inputs: ValidityInputs) -> ValidityKey:
    """Bind the current facts into a validity key.

    Never trusts an agent-supplied SHA: the caller passes facts it *measured*
    (git rev-parse, git write-tree, the committed spec blob identity, the
    frozen run-config, the target ref, the PR head/base). This function only
    binds them; it does not fetch them.
    """
    return ValidityKey(inputs=inputs, digest=_digest(inputs.canonical_json()))


def changed_fields(current: ValidityInputs, bound: ValidityInputs) -> tuple[str, ...]:
    """The bound fields whose current value differs, in canonical order.

    Empty means the binding still holds and no re-verify is triggered.
    """
    return tuple(
        name for name in _BOUND_FIELDS if str(getattr(bound, name)) != str(getattr(current, name))
    )


def verify_validity(key: ValidityKey, current: ValidityInputs) -> tuple[tuple[str, ...], bool]:
    """Re-measure a key against the current facts.

    Returns ``(changed, matches_digest)``: the changed field names, and whether
    re-binding the *current* facts reproduces the key's recorded digest.
    """
    changed = changed_fields(current, key.inputs)
    matches = build_validity_key(current).digest == key.digest
    return changed, matches


def is_bookkeeping_only(current: ValidityInputs, bound: ValidityInputs) -> bool:
    """True when nothing that matters changed -- a bookkeeping commit that must
    not mint a re-review.

    The product *revision* may change (an empty machine-part commit advances the
    chain) while the product *tree*, spec, acceptance context, target and PR all
    stay put. That is pure bookkeeping: no re-verify.
    """
    return (
        current != bound
        and current.product_tree == bound.product_tree
        and current.spec_digest == bound.spec_digest
        and current.acceptance_context_revision == bound.acceptance_context_revision
        and current.target_identity == bound.target_identity
        and current.pr_identity == bound.pr_identity
    )


#: Which stages a change to each bound field invalidates. A SPEC change is a new
#: DD (invalidates everything). A product *revision* change alone invalidates
#: nothing: the chain may advance on a pure bookkeeping commit while the tree
#: (the content the acceptance/reviews actually graded) stays identical -- the
#: revision is bound for continuity, the tree is what triggers re-verify. A
#: product *tree* change invalidates implement, acceptance and both reviews; an
#: acceptance-context change invalidates acceptance through final review; a
#: target or PR change invalidates only the merge authorization (and, for PR, the
#: reviews that bound it).
_FIELD_INVALIDATES: dict[str, tuple[str, ...]] = {
    "spec_digest": (
        "configure",
        "implement",
        "acceptance",
        "continuous_review",
        "final_review",
        "human_gate",
        "merger",
    ),
    "product_revision": (),
    "product_tree": ("implement", "acceptance", "continuous_review", "final_review"),
    "acceptance_context_revision": ("acceptance", "continuous_review", "final_review"),
    "target_identity": ("merger",),
    "pr_identity": ("final_review", "human_gate", "merger"),
}


def affected_stages(changed: tuple[str, ...]) -> tuple[str, ...]:
    """The stages a set of changed fields invalidates, de-duplicated and in
    canonical order. Empty means "no re-verify needed"."""
    affected: list[str] = []
    for name in _BOUND_FIELDS:
        if name in changed:
            for stage in _FIELD_INVALIDATES[name]:
                if stage not in affected:
                    affected.append(stage)
    return tuple(affected)


def validity_fields() -> tuple[str, ...]:
    """The bound field names, exposed so a caller can diff without importing
    the dataclass's private layout."""
    return _BOUND_FIELDS


__all__ = [
    "SPEC_FIELD",
    "ValidityInputs",
    "ValidityKey",
    "affected_stages",
    "build_validity_key",
    "changed_fields",
    "is_bookkeeping_only",
    "validity_fields",
    "verify_validity",
]
