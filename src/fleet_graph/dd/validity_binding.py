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

from fleet_graph.dd.validity import (
    ValidityInputs,
    ValidityKey,
    affected_stages,
    build_validity_key,
    changed_fields,
    is_bookkeeping_only,
    verify_validity,
)

__all__ = [
    "BindingFacts",
    "binding_affected",
    "binding_is_bookkeeping",
    "build_validity_binding",
    "git_product_facts",
    "verify_binding",
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