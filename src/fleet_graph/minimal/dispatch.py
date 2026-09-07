"""Dispatch launch protocol -> DDRepoRef conversion and the DD-ready gate (GO-36).

Pure conversion layer: no I/O, no git, no agent calls. The Goal Agent's
``goal.turn/1`` ``stop: dispatch`` output object is the DD launch protocol; this
module turns its already-validated ``repos`` into :class:`gitgate.DDRepoRef`
entries and hands them to :func:`gitgate.check_dd_ready`. Field-level validation
lives in :mod:`fleet_graph.minimal.protocol` (``_check_dispatch``); this module
assumes a validated dispatch sub-object and only re-checks what construction
would otherwise blow up on, raising ``ValueError`` with a repo index rather than
returning a half-built list.

The dispatch schema is ``protocol.SCHEMA_GOAL_TURN`` -- not redefined here.
"""

from __future__ import annotations

import os

from fleet_graph.minimal import acceptance, gitgate, protocol


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


def dd_repo_refs(dispatch: dict) -> list[gitgate.DDRepoRef]:
    """Convert a validated ``dispatch`` sub-object into ``DDRepoRef`` entries.

    Each ``repos[i]`` maps to ``DDRepoRef(worktree=path, remote=remote,
    branch=branch, spec_path=spec_path, label=basename(path))``. The dispatch
    object is assumed to have passed ``protocol``'s ``_check_dispatch``; if a
    field is still not a non-empty string (defensive), raise ``ValueError``
    with the repo index instead of returning a partial result.
    """

    repos = dispatch.get("repos")
    if not isinstance(repos, list) or not repos:
        raise ValueError(f"dispatch.repos must be a non-empty list ({protocol.SCHEMA_GOAL_TURN})")

    refs: list[gitgate.DDRepoRef] = []
    for index, repo in enumerate(repos):
        if not isinstance(repo, dict):
            raise ValueError(f"dispatch.repos[{index}] must be an object")
        path = repo.get("path")
        remote = repo.get("remote")
        branch = repo.get("branch")
        spec_path = repo.get("spec_path")
        for field, value in (
            ("path", path),
            ("remote", remote),
            ("branch", branch),
            ("spec_path", spec_path),
        ):
            if not _is_nonempty_str(value):
                raise ValueError(f"dispatch.repos[{index}].{field} must be a non-empty string")
        refs.append(
            gitgate.DDRepoRef(
                worktree=path,
                remote=remote,
                branch=branch,
                spec_path=spec_path,
                label=os.path.basename(path),
            )
        )
    return refs


def dd_acceptance(dispatch: dict, goal_acceptance: list[str]) -> list[str]:
    """The DD's full acceptance batch: goal acceptance plus ``acceptance_extra``.

    Order-preserving dedup via :func:`acceptance.combine_acceptance`.
    """

    return acceptance.combine_acceptance(goal_acceptance, dispatch.get("acceptance_extra"))


def check_dispatch_ready(
    dispatch: dict,
    *,
    goal_acceptance: list[str],
    runner: gitgate.GitRunner,
) -> gitgate.GateResult:
    """GO-36's three-mechanical-repo check for a dispatch object.

    Build the ``DDRepoRef`` list then delegate to ``gitgate.check_dd_ready``;
    no event is written, no PR opened, no side effect beyond the read-only git
    queries the gate itself performs.
    """

    refs = dd_repo_refs(dispatch)
    return gitgate.check_dd_ready(refs, runner=runner)


__all__ = [
    "check_dispatch_ready",
    "dd_acceptance",
    "dd_repo_refs",
]
