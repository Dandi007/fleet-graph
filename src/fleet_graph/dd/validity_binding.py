"""The version-bound validity key, wired to real git/product facts (spec L3).

`validity.py` is the pure binding: it diffs six facts and reports which ones
moved, without knowing where those facts come from. This module is the other
half the spec's L3 requires -- it *measures* the facts out of the product's own
git tree and binds them, so the dispatch, materializer, replay, goal gate and
recovery paths all seal and re-verify against the same real inputs instead of
an agent's self-report.

Two facts are measured from git plumbing and never trusted:

- **product_revision** -- the full commit id (``git_ops.exact_commit_identity``)
  the work is sealed at;
- **product_tree** -- the tree that commit resolves to, which is what an
  acceptance/review actually graded (a bookkeeping commit advances the revision
  while leaving the tree identical).

The other four are the caller's own frozen facts: the committed SPEC digest,
the acceptance-context revision (the run-config the commands were graded
against), the target identity (the durable ref the merge lands on) and the PR
identity (head/base pair). The caller measures them at the same commit where it
read them, so the binding closes over one coherent point in time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fleet_graph.dd.validity import (
    ValidityInputs,
    ValidityKey,
    affected_stages,
    build_validity_key,
    changed_fields,
    is_bookkeeping_only,
    validity_fields,
    verify_validity,
)

__all__ = [
    "RUN_CONFIG_PATH",
    "BindingFacts",
    "binding_affected",
    "binding_is_bookkeeping",
    "binding_key_from_fields",
    "build_validity_binding",
    "git_product_facts",
    "measure_acceptance_context_revision",
    "verify_binding",
]

#: The committed run-config that pins the acceptance context. Its blob oid is
#: the acceptance-context revision the validity key binds (spec L3): the exact
#: run-config the acceptance commands were graded against, never a report.
RUN_CONFIG_PATH = ".dev-dispatch/run-config.json"


def measure_acceptance_context_revision(workspace_path: str, input_commit: str) -> str:
    """The acceptance-context revision: the committed run-config's blob oid.

    Read from git plumbing at ``input_commit``. Raises
    ``git_ops.ExactWorkspaceError`` when the run-config is absent or unreadable
    -- a fail-closed read, never an empty or guessed revision.
    """
    from fleet_graph.dd.vendor import git_ops

    return git_ops.exact_artifact_identity(workspace_path, input_commit, RUN_CONFIG_PATH)[
        "blob_oid"
    ]


def git_product_facts(workspace_path: str, input_commit: str) -> tuple[str, str]:
    """Measure the product revision and tree out of git, never from a report."""
    from fleet_graph.dd.vendor import git_ops

    identity = git_ops.exact_commit_identity(workspace_path, input_commit)
    return identity["sha"], identity["tree_sha"]


@dataclass(frozen=True)
class BindingFacts:
    """The four non-git facts a binding closes over. Each is a string and an
    empty value is bound too -- the transition unknown -> known is itself a
    change worth detecting (``validity.py`` treats "" as a bound fact, never a
    silent skip)."""

    spec_digest: str = ""
    acceptance_context_revision: str = ""
    target_identity: str = ""
    pr_identity: str = ""

    def as_inputs(self, revision: str, tree: str) -> ValidityInputs:
        return ValidityInputs(
            product_revision=revision.lower(),
            product_tree=tree.lower(),
            spec_digest=self.spec_digest,
            acceptance_context_revision=self.acceptance_context_revision,
            target_identity=self.target_identity,
            pr_identity=self.pr_identity,
        )


def binding_key_from_fields(fields: Any) -> ValidityKey | None:
    """Reconstruct a sealed validity key from its ``fields`` record.

    A sealed key travels as ``{"digest", "fields"}`` on the raw-event boundary
    and in a gate decision file. To verify a *later* set of facts against it,
    the verifier needs the key's bound inputs -- this rebuilds an immutable
    ``ValidityKey`` from the recorded fields, refusing (``None``) any record
    that does not name the complete bound field set rather than comparing
    against a half-read key.
    """
    if not isinstance(fields, dict):
        return None
    names = validity_fields()
    if any(name not in fields for name in names):
        return None
    inputs = ValidityInputs(**{name: fields[name] for name in names})
    return build_validity_key(inputs)


def build_validity_binding(
    workspace_path: str, input_commit: str, facts: BindingFacts
) -> ValidityKey:
    """Measure the product facts and bind them with the caller's frozen facts.

    Raises ``git_ops.ExactWorkspaceError`` when the commit/tree cannot be read
    -- a fail-closed read, never a guessed SHA.
    """
    revision, tree = git_product_facts(workspace_path, input_commit)
    return build_validity_key(facts.as_inputs(revision, tree))


def verify_binding(
    workspace_path: str,
    input_commit: str,
    facts: BindingFacts,
    key: ValidityKey,
) -> tuple[tuple[str, ...], bool]:
    """Re-measure the current facts against a sealed key.

    Returns ``(changed, matches_digest)`` exactly as ``verify_validity`` does,
    but with the product facts read out of git at ``input_commit`` rather than
    supplied by the caller.
    """
    revision, tree = git_product_facts(workspace_path, input_commit)
    current = facts.as_inputs(revision, tree)
    return verify_validity(key, current)


def binding_affected(
    workspace_path: str,
    input_commit: str,
    facts: BindingFacts,
    key: ValidityKey,
) -> tuple[str, ...]:
    """The stages a change to any bound field invalidates, for the current facts."""
    revision, tree = git_product_facts(workspace_path, input_commit)
    current = facts.as_inputs(revision, tree)
    return affected_stages(changed_fields(current, key.inputs))


def binding_is_bookkeeping(
    workspace_path: str,
    input_commit: str,
    facts: BindingFacts,
    key: ValidityKey,
) -> bool:
    """Whether moving from ``key``'s facts to the current ones is bookkeeping."""
    revision, tree = git_product_facts(workspace_path, input_commit)
    current = facts.as_inputs(revision, tree)
    return is_bookkeeping_only(current, key.inputs)