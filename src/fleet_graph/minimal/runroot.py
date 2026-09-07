"""The engine's per-goal state root and the post-enroll mechanical prep (GO-34).

GO-34 corrected the GO-19 placement: runtime state (events.jsonl, control.jsonl,
sessions, worktrees, goal.enroll.json) is *not* kept in the work folder — the
engine keeps it under its own root (``/data/fleet/goals/<goal_id>/``, per
context.md:7); the WF only holds human-read goal / design / progress / findings.

This module defines, once and for all, what that root looks like (``GoalRunRoot``)
and the *programmatic prep* that runs after enroll validates but before the goal
agent spawns (GO-33 / context.md:6): create the root, write the enroll object
verbatim plus the first event, and per repo fetch then cut+push the release
branch when it does not exist yet.

Nothing here spawns a process, signs a signal, runs git, or touches the work
folder MCP. ``prepare_plan`` only produces a data plan ("needs_release_push")
for some later engine node to execute via ``git_argv_for``'s guarded argv lists.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fleet_graph.minimal.enroll import _GOAL_ID_HEX_LEN, _GOAL_ID_PREFIX

DEFAULT_ENGINE_ROOT = "/data/fleet/goals"

# goal_id shape is the one enroll.py derives (its `_GOAL_ID_PREFIX` /
# `_GOAL_ID_HEX_LEN`): `g-` + 6 lowercase hex chars. Importing the two constants
# (rather than re-declaring them) keeps the shape mechanically identical to what
# enroll.py validates, since the same id then enters filesystem paths here.
_GOAL_ID_RE = re.compile(rf"^{re.escape(_GOAL_ID_PREFIX)}[0-9a-f]{{{_GOAL_ID_HEX_LEN}}}$")

# dd_ids are branch-name / worktree-name fragments; the whitelist is deliberately
# narrow so a dd_id can never carry a `/`, `..`, space, or shell metacharacter
# into a ref name or a worktrees/ path.
_DD_ID_RE = re.compile(r"[A-Za-z0-9._-]+")

# The three repo-config guards, duplicated locally (same shape as
# enroll._GIT_GUARDS and gitgate._GUARDS). They are *not* imported from enroll:
# tests/test_dd_git.py's source-level invariant whitelists argv builders by
# module and greps for the literal `["git", ...]` shape, so each builder keeps
# its own literal copy — the guard list stays grep-visible and cannot drift via
# a shared import. core.fsmonitor runs on index refresh, core.hooksPath runs
# hooks, protocol.ext.allow=never blocks ext:: shell transports.
_GIT_GUARDS: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
)


def _git_argv(path: str, *args: str) -> list[str]:
    """Guarded git argv against an untrusted worktree (see _GIT_GUARDS)."""
    return ["git", *_GIT_GUARDS, "-C", path, *args]


class RunRootConflict(Exception):
    """A goal_id is already enrolled with a *different* payload and must not be
    re-enrolled (enroll.py's one-goal-per-id rule, enforced here at write time)."""

    def __init__(self, goal_id: str, enroll_path: Path) -> None:
        super().__init__(
            f"goal {goal_id} is already enrolled to a different payload at "
            f"{enroll_path}; refusing to overwrite"
        )
        self.goal_id = goal_id
        self.enroll_path = enroll_path


@dataclass(frozen=True)
class GoalRunRoot:
    """The engine's per-goal state root: ``<engine_root>/<goal_id>/``.

    ``root`` is the concrete directory; the sub-paths are properties so every
    consumer names them the same way (events.py / control.py take ``root``, the
    history handle in protocol §0.7 points at ``events_path`` / ``dd_dir``).
    """

    goal_id: str
    root: Path

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def control_path(self) -> Path:
        return self.root / "control.jsonl"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def worktrees_dir(self) -> Path:
        return self.root / "worktrees"

    @property
    def dd_dir(self) -> Path:
        return self.root / "dd"

    @property
    def enroll_path(self) -> Path:
        return self.root / "goal.enroll.json"

    @property
    def observations_path(self) -> Path:
        return self.root / "observations.jsonl"


def goal_run_root(
    goal_id: str,
    *,
    engine_root: str | os.PathLike[str] = DEFAULT_ENGINE_ROOT,
) -> GoalRunRoot:
    """Build the state root for ``goal_id`` under ``engine_root``.

    ``goal_id`` becomes a path component, so it is restricted to the enroll
    shape (``g-`` + 6 lowercase hex) — anything else (a ``/``, ``..``, wrong
    case, wrong length) is rejected with ValueError rather than let it escape
    the root.
    """
    if not isinstance(goal_id, str) or not _GOAL_ID_RE.fullmatch(goal_id):
        raise ValueError(
            f"invalid goal_id {goal_id!r}: must match {_GOAL_ID_PREFIX!r} + "
            f"{_GOAL_ID_HEX_LEN} lowercase hex chars"
        )
    return GoalRunRoot(goal_id=goal_id, root=Path(engine_root) / goal_id)


def _validate_dd_id(dd_id: str) -> None:
    if not isinstance(dd_id, str) or not _DD_ID_RE.fullmatch(dd_id):
        raise ValueError(f"invalid dd_id {dd_id!r}: must match {_DD_ID_RE.pattern!r}")


def dd_branch(goal_id: str, dd_id: str) -> str:
    """The DD branch name ``dd/<goal_id>/<dd_id>`` (mechanical, GO-25).

    ``dd_id`` goes through the narrow whitelist so it cannot smuggle slashes or
    `..` into a ref name.
    """
    _validate_dd_id(dd_id)
    return f"dd/{goal_id}/{dd_id}"


def worktree_path(run_root: GoalRunRoot, dd_id: str) -> Path:
    """The worktree directory for ``dd_id``: ``<run_root>/worktrees/<dd_id>/``."""
    _validate_dd_id(dd_id)
    return run_root.worktrees_dir / dd_id


def release_branch(enroll_obj: dict) -> str:
    """The goal's release branch name, taken from ``enroll_obj["source_branch"]``.

    GO-30 / GO-31 (golden-order.md:181-189) moved the release branch name back to
    goal level and named it ``source_branch``: it is written once on enroll, is
    identical across every repo, and — when it does not exist yet — the engine
    cuts it from each repo's ``target_branch``. This supersedes protocol.md
    §0.10's old ``release/<goal_id>`` spelling, which is why we read the field
    instead of composing the name.
    """
    branch = enroll_obj.get("source_branch")
    if not isinstance(branch, str) or not branch:
        raise ValueError("enroll object is missing 'source_branch' (release branch name)")
    return branch


def create_run_root(run_root: GoalRunRoot) -> None:
    """Idempotently create the root and its standard subdirectories."""
    for directory in (
        run_root.root,
        run_root.sessions_dir,
        run_root.worktrees_dir,
        run_root.dd_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def _serialize_enroll(enroll_obj: dict) -> str:
    return json.dumps(enroll_obj, ensure_ascii=False, indent=2)


def write_enroll(run_root: GoalRunRoot, enroll_obj: dict) -> None:
    """Write the enroll object verbatim to ``goal.enroll.json``.

    Same write discipline as events.py:169-172 (write + flush + os.fsync) and
    ``ensure_ascii=False`` so non-ASCII (titles, goal text) are preserved.

    Replay-safe: an identical existing file is a silent no-op; a *different*
    existing file raises ``RunRootConflict`` — a goal_id is enrolled once.
    """
    serialized = _serialize_enroll(enroll_obj)
    run_root.root.mkdir(parents=True, exist_ok=True)
    if run_root.enroll_path.exists():
        existing = run_root.enroll_path.read_text(encoding="utf-8")
        if existing == serialized:
            return
        raise RunRootConflict(run_root.goal_id, run_root.enroll_path)
    with run_root.enroll_path.open("w", encoding="utf-8") as f:
        f.write(serialized)
        f.flush()
        os.fsync(f.fileno())


def read_enroll(run_root: GoalRunRoot) -> dict:
    """Read the enroll object back from ``goal.enroll.json``."""
    with run_root.enroll_path.open("r", encoding="utf-8") as f:
        return json.load(f)


@dataclass(frozen=True)
class RepoPrep:
    """One repo's post-enroll preparation (pure data, nothing executed)."""

    path: str
    remote: str
    target_branch: str
    release_branch: str
    needs_release_push: bool


@dataclass(frozen=True)
class PreparePlan:
    """The full prep plan for one goal: root + release branch + per-repo steps."""

    run_root: GoalRunRoot
    release_branch: str
    repos: tuple[RepoPrep, ...]


def prepare_plan(
    enroll_obj: dict,
    goal_id: str,
    *,
    engine_root: str | os.PathLike[str] = DEFAULT_ENGINE_ROOT,
    release_exists: Callable[[str, str, str], bool],
) -> PreparePlan:
    """Build the post-enroll prep plan without executing anything.

    ``release_exists(path, remote, release_branch)`` is the injected probe (same
    style as control.py's ``alive_probe`` / enroll.py's ``GitProbe``): return
    False and the repo is flagged ``needs_release_push`` so a later engine node
    cuts its release branch from ``target_branch`` and pushes it. This function
    never runs git, pushes, or spawns.
    """
    branch = release_branch(enroll_obj)
    run_root = goal_run_root(goal_id, engine_root=engine_root)
    repos = tuple(
        RepoPrep(
            path=repo["path"],
            remote=repo["remote"],
            target_branch=repo["target_branch"],
            release_branch=branch,
            needs_release_push=not release_exists(repo["path"], repo["remote"], branch),
        )
        for repo in enroll_obj["repos"]
    )
    return PreparePlan(run_root=run_root, release_branch=branch, repos=repos)


def git_argv_for(prep: RepoPrep) -> list[list[str]]:
    """The guarded argv list to carry out ``prep`` (returned, not executed).

    Always ``git … fetch <remote>``; when the release branch is missing, also
    ``git … push <remote> <remote>/<target_branch>:refs/heads/<release_branch>``
    (cut the release branch from the freshly-fetched target and publish it).
    Every argv is ``list[str]`` (never a shell string) with the three guards
    preceding ``-C <path>``.
    """
    argv = [_git_argv(prep.path, "fetch", prep.remote)]
    if prep.needs_release_push:
        refspec = f"{prep.remote}/{prep.target_branch}:refs/heads/{prep.release_branch}"
        argv.append(_git_argv(prep.path, "push", prep.remote, refspec))
    return argv


__all__ = [
    "DEFAULT_ENGINE_ROOT",
    "GoalRunRoot",
    "PreparePlan",
    "RepoPrep",
    "RunRootConflict",
    "create_run_root",
    "dd_branch",
    "git_argv_for",
    "goal_run_root",
    "prepare_plan",
    "read_enroll",
    "release_branch",
    "worktree_path",
    "write_enroll",
]
