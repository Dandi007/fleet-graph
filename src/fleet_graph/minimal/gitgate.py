"""The minimal git gate: mechanical checks for agent handoffs (GO-25/28/36).

Two gates live here, both pure read-side logic over one isolated git runner:

- ``check_handoff`` (GO-28): every inter-agent handoff must be pushed to the
  remote with local HEAD == remote branch tip and a clean worktree.
- ``check_dd_ready`` (GO-36): before the engine opens a DD, additionally the
  branch must exist on the remote and the spec file must exist in the HEAD
  commit itself.

Failures are normal return values (``GateResult``), never exceptions: they are
data to be written into events and bounced back to the agent. No write
operation (commit / push / branch / worktree / merge) is performed here.

Every argv this module builds carries the three repo-config guards
(``core.fsmonitor=false`` / ``core.hooksPath=/dev/null`` / ``protocol.ext.allow=never``).
The measured reason is in ``fleet_graph/dd/git.py``: a worktree written by an
agent can carry a hostile repo-local config, and ``git status`` -- which the
gate runs -- executes ``core.fsmonitor`` on index refresh. The guards are
duplicated here rather than imported because this package must not depend on
the old ``fleet_graph.dd`` modules (per-DD isolation). The source-wide
invariant in ``tests/test_dd_git.py`` whitelists this module's guarded argv
shape (the guards must directly follow the ``git`` token), so the literal
command token appears in the argv builder, greppable like any other call.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TIMEOUT_SECONDS = 60.0


def _sha_from_ls_remote(stdout: str) -> str | None:
    """The first full sha in ``ls-remote`` output, or None if it is empty.

    ``git ls-remote <remote> <pattern>`` prints nothing (exit 0) when the
    remote holds no matching ref, and one ``<sha>\t<ref>`` line per match
    otherwise; matching lines for ``refs/heads/<branch>`` carry that branch's
    tip. Reading the sha from stdout -- not the exit code, not stderr -- keeps
    the branch-absent verdict independent of git's error wording or locale.
    """

    for line in stdout.splitlines():
        for token in line.split():
            if _FULL_SHA_RE.match(token):
                return token
    return None


# The three repo-config guards, mirrored from fleet_graph/dd/git.py GUARDS:
# `core.fsmonitor` is a command git runs on index refresh, `core.hooksPath`
# runs every hook, `protocol.ext.allow=never` blocks `ext::` remotes from
# executing a shell transport. Agent-written worktrees can set all three in
# repo-local .git/config, which global/system config isolation does not cover.
_GUARDS: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
)


def _git(worktree: str, *args: str) -> list[str]:
    """The guarded argv for one git call against ``worktree``.

    Shape-mirrors ``fleet_graph/dd/git.py::git_argv``: the three ``-c`` config
    guards precede ``-C <worktree>``, so a hostile repo-local config cannot
    turn a status query into command execution.
    """
    return ["git", *_GUARDS, "-C", worktree, *args]


class FailureCode:
    """Machine-readable reasons a gate can fail.

    These are the constants downstream DDs reference in event payloads and
    bounce-back messages; they must not be hand-typed as string literals.
    """

    NOT_PUSHED = "not_pushed"
    HEAD_BEHIND_REMOTE = "head_behind_remote"
    DIRTY_WORKTREE = "dirty_worktree"
    BRANCH_MISSING_ON_REMOTE = "branch_missing_on_remote"
    DETACHED_HEAD = "detached_head"
    WRONG_BRANCH = "wrong_branch"
    SPEC_MISSING = "spec_missing"


class GitError(RuntimeError):
    """A git invocation failed unexpectedly (bad worktree path, no repo...).

    This is an environment error, not a gate verdict: gates only return
    failures, they never raise them. Callers may let this propagate.
    """

    def __init__(self, argv: list[str], exit_code: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(argv)} failed (exit {exit_code}): {stderr.strip()}")
        self.argv = argv
        self.exit_code = exit_code
        self.stderr = stderr


@dataclass(frozen=True)
class CompletedResult:
    """The outcome of one git invocation: exit code plus raw output."""

    exit_code: int
    stdout: str
    stderr: str


class GitRunner(Protocol):
    """Every git call funnels through this, argv-list only, never a shell."""

    def run(self, args: list[str], *, cwd: str) -> CompletedResult:
        """Run ``args`` in ``cwd`` and return exit code / stdout / stderr."""
        ...  # pragma: no cover - protocol body


class SubprocessGitRunner:
    """Default ``GitRunner``: list-argv ``subprocess.run`` with a timeout.

    No ``shell=True`` is ever used (the argv is a list, so no string ever
    reaches a shell). The environment is stripped of caller-controlled
    ``GIT_*`` settings; the repo-config guards live in the argv itself
    (``_GUARDS``), because the worktree is agent-written and its repo-local
    config is not covered by env isolation.
    """

    def __init__(self, timeout: float = _TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def run(self, args: list[str], *, cwd: str) -> CompletedResult:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                env=env,
                timeout=self.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(args, -1, f"timed out after {self.timeout}s") from exc
        return CompletedResult(proc.returncode, proc.stdout, proc.stderr)


@dataclass(frozen=True)
class WorktreeStatus:
    """A snapshot of one worktree, from the read-only status queries."""

    branch: str | None
    head: str | None
    clean: bool
    untracked: bool


@dataclass(frozen=True)
class RepoRef:
    """One repo to check: the worktree path plus its remote-tracking ref."""

    worktree: str
    remote: str
    branch: str
    label: str | None = None

    @property
    def repo_id(self) -> str:
        return self.label if self.label is not None else self.worktree


@dataclass(frozen=True)
class DDRepoRef(RepoRef):
    """A ``RepoRef`` carrying the spec path required by the DD gate.

    ``spec_path`` must be a non-empty repo-relative path: an empty one would
    silently pass the GO-36 spec check because ``git cat-file -e '<sha>:'``
    exits 0 for the empty path, so emptiness is rejected at construction
    instead of being discovered at the git layer.
    """

    spec_path: str = ""

    def __post_init__(self) -> None:
        if not self.spec_path:
            raise ValueError(
                "DDRepoRef.spec_path must be a non-empty repo-relative path "
                "(GO-36 check 5 would otherwise be silently skipped)"
            )


@dataclass(frozen=True)
class GateFailure:
    """One failed check: which repo, which criterion, human-readable detail."""

    repo: str
    code: str
    detail: str


@dataclass(frozen=True)
class GateResult:
    """The aggregated verdict of a gate over a group of repos."""

    ok: bool
    failures: list[GateFailure]


def _run(runner: GitRunner, args: list[str], cwd: str) -> CompletedResult:
    return runner.run(args, cwd=cwd)


def _git(worktree: str, *args: str) -> list[str]:
    """The guarded argv for one git call against ``worktree``.

    Shape-mirrors ``fleet_graph/dd/git.py::git_argv``: the three ``-c`` config
    guards precede ``-C <worktree>``, so a hostile repo-local config cannot
    turn a status query into command execution.
    """
    return ["git", *_GUARDS, "-C", worktree, *args]


def worktree_status(worktree: str, *, runner: GitRunner) -> WorktreeStatus:
    """Read branch / head / clean / untracked for ``worktree``.

    ``branch`` is ``None`` on a detached HEAD; ``head`` is the 40-char sha of
    HEAD (``None`` only when git itself cannot name it); ``clean`` is True when
    ``git status --porcelain`` is empty; ``untracked`` reports untracked files
    separately so failure details can be more precise.
    """
    branch_argv = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    branch_res = _run(runner, branch_argv, worktree)
    if branch_res.exit_code != 0:
        raise GitError(branch_argv, branch_res.exit_code, branch_res.stderr)
    branch = branch_res.stdout.strip()
    branch = None if branch == "HEAD" else branch

    head_argv = _git(worktree, "rev-parse", "HEAD")
    head_res = _run(runner, head_argv, worktree)
    if head_res.exit_code != 0:
        raise GitError(head_argv, head_res.exit_code, head_res.stderr)
    head = head_res.stdout.strip()
    head = head if _FULL_SHA_RE.match(head) else None

    status_argv = _git(worktree, "status", "--porcelain")
    status_res = _run(runner, status_argv, worktree)
    if status_res.exit_code != 0:
        raise GitError(status_argv, status_res.exit_code, status_res.stderr)

    lines = [line for line in status_res.stdout.splitlines() if line.strip()]
    untracked = any(line.startswith("??") for line in lines)
    return WorktreeStatus(
        branch=branch,
        head=head,
        clean=not lines,
        untracked=untracked,
    )


def remote_tip(
    worktree: str,
    remote: str,
    branch: str,
    *,
    runner: GitRunner,
    fetch: bool = True,
) -> str | None:
    """Resolve the tip of ``branch`` on ``remote``.

    Asks the remote directly via ``git ls-remote <remote> refs/heads/<branch>``
    when ``fetch=True`` (the default): the answer is what the remote actually
    holds right now, with no dependence on git's stderr wording, exit codes,
    or translation of a failed ``git fetch``. ``fetch=False`` (tests, or
    callers that just fetched) resolves ``<remote>/<branch>`` from the local
    refs instead. Returns the 40-char sha, or ``None`` when the branch does
    not exist on the remote (the GO-36 ① verdict, not an error). A fetch
    failure for any other reason is an environment error and raises
    ``GitError``.
    """

    if fetch:
        argv = _git(worktree, "ls-remote", remote, f"refs/heads/{branch}")
        res = _run(runner, argv, worktree)
        if res.exit_code != 0:
            raise GitError(argv, res.exit_code, res.stderr)
        tip = _sha_from_ls_remote(res.stdout)
        return tip

    argv = _git(worktree, "rev-parse", "--verify", f"{remote}/{branch}")
    res = _run(runner, argv, worktree)
    if res.exit_code != 0:
        return None
    tip = res.stdout.strip()
    return tip if _FULL_SHA_RE.match(tip) else None


def file_exists_at(worktree: str, commit: str, relpath: str, *, runner: GitRunner) -> bool:
    """Whether ``relpath`` exists in the tree of ``commit`` (not the worktree)."""
    argv = _git(worktree, "cat-file", "-e", f"{commit}:{relpath}")
    return _run(runner, argv, worktree).exit_code == 0


def revision_reachable(
    worktree: str,
    remote: str,
    branch: str,
    sha: str,
    *,
    runner: GitRunner,
) -> bool:
    """Whether ``sha`` is still part of ``branch``'s history on ``remote``.

    protocol §11's recovery state-mismatch gate: the engine records a
    ``release_head`` / ``head_commit`` sha in events and, before resuming,
    verifies that sha is still reachable from the branch tip. The tip may have
    advanced since (a merge moving the branch forward keeps the old head as an
    ancestor), but a rewritten / force-pushed branch loses it. The tip is read
    via :func:`remote_tip`; a branch that no longer exists, a git failure, or a
    non-ancestor sha all answer ``False`` — the caller blocks rather than guess
    on possibly-rewritten state. A verbatim tip match answers ``True`` without
    an extra ``merge-base`` round-trip.
    """
    try:
        tip = remote_tip(worktree, remote, branch, runner=runner)
    except GitError:
        return False
    if tip is None:
        return False
    if tip == sha:
        return True
    argv = _git(worktree, "merge-base", "--is-ancestor", sha, tip)
    return _run(runner, argv, worktree).exit_code == 0


def check_handoff(repos: list[RepoRef], *, runner: GitRunner) -> GateResult:
    """GO-28's handoff gate: pushed, HEAD == remote tip, clean worktree.

    Each entry is checked independently; a branch missing on the remote fails
    ``BRANCH_MISSING_ON_REMOTE`` (it cannot be pushed). The verdict is a normal
    return value, never an exception, so it can be recorded as an event and
    bounced back to the agent.
    """
    failures: list[GateFailure] = []
    for repo in repos:
        status = worktree_status(repo.worktree, runner=runner)
        failures.extend(_handoff_failures(repo, status, runner=runner))
    return GateResult(ok=not failures, failures=failures)


def check_dd_ready(repos: list[DDRepoRef], *, runner: GitRunner) -> GateResult:
    """GO-36's open-a-DD gate: handoff checks plus remote branch and spec.

    On top of the three handoff criteria, each repo must have its branch on
    the remote and the spec file present in the HEAD commit itself. A repo
    already failing the handoff layer is recorded once, with its earliest
    precise code; only repos that pass it get the extra spec check (a green
    handoff there implies HEAD resolved to a full 40-char sha).
    """
    failures: list[GateFailure] = []
    for repo in repos:
        status = worktree_status(repo.worktree, runner=runner)
        repo_failures = _handoff_failures(repo, status, runner=runner)
        if repo_failures:
            failures.extend(repo_failures)
            continue
        assert status.head is not None  # a green handoff implies HEAD is a full sha
        if not file_exists_at(repo.worktree, status.head, repo.spec_path, runner=runner):
            failures.append(
                GateFailure(
                    repo=repo.repo_id,
                    code=FailureCode.SPEC_MISSING,
                    detail=(
                        f"spec file {repo.spec_path} does not exist in commit {status.head}; "
                        "it must be committed and pushed with the branch"
                    ),
                )
            )
    return GateResult(ok=not failures, failures=failures)


def _handoff_failures(
    repo: RepoRef, status: WorktreeStatus, *, runner: GitRunner
) -> list[GateFailure]:
    """The three GO-28 criteria for one repo, most fundamental first."""

    if status.branch is None:
        return [
            GateFailure(
                repo=repo.repo_id,
                code=FailureCode.DETACHED_HEAD,
                detail=f"worktree {repo.worktree} is on a detached HEAD",
            )
        ]

    # GO-36 ②: the worktree must be on the expected branch at all, checked
    # before any head/tip comparison so a switched worktree is never
    # misreported as not_pushed / head_behind_remote (which would point the
    # agent at the wrong fix). A green-looking coincidence -- HEAD equal to
    # the remote tip because the dd branch was just cut from it -- is still a
    # wrong_branch failure.
    if status.branch != repo.branch:
        return [
            GateFailure(
                repo=repo.repo_id,
                code=FailureCode.WRONG_BRANCH,
                detail=(
                    f"worktree {repo.worktree} is on branch {status.branch}, expected {repo.branch}"
                ),
            )
        ]

    tip = remote_tip(repo.worktree, repo.remote, repo.branch, runner=runner)
    if tip is None:
        return [
            GateFailure(
                repo=repo.repo_id,
                code=FailureCode.BRANCH_MISSING_ON_REMOTE,
                detail=(
                    f"branch {repo.branch} does not exist on remote {repo.remote}; "
                    "cannot verify the handoff was pushed"
                ),
            )
        ]

    failures: list[GateFailure] = []
    head = status.head
    if head is None:
        failures.append(
            GateFailure(
                repo=repo.repo_id,
                code=FailureCode.NOT_PUSHED,
                detail=f"HEAD of {repo.worktree} could not be resolved to a commit",
            )
        )
    elif head != tip:
        code = (
            FailureCode.HEAD_BEHIND_REMOTE
            if _is_ancestor(head, tip, repo, runner)
            else FailureCode.NOT_PUSHED
        )
        relation = "behind" if code == FailureCode.HEAD_BEHIND_REMOTE else "not pushed to"
        failures.append(
            GateFailure(
                repo=repo.repo_id,
                code=code,
                detail=(
                    f"local HEAD {head} of branch {repo.branch} {relation} "
                    f"remote {repo.remote}/{repo.branch} tip {tip}"
                ),
            )
        )

    if not status.clean:
        failures.append(
            GateFailure(
                repo=repo.repo_id,
                code=FailureCode.DIRTY_WORKTREE,
                detail=(
                    f"worktree {repo.worktree} has uncommitted changes"
                    + (" (including untracked files)" if status.untracked else "")
                ),
            )
        )

    return failures


def _is_ancestor(head: str, tip: str, repo: RepoRef, runner: GitRunner) -> bool:
    """Whether local HEAD is an ancestor of the remote tip (i.e. merely stale)."""
    argv = _git(repo.worktree, "merge-base", "--is-ancestor", head, tip)
    return _run(runner, argv, repo.worktree).exit_code == 0


__all__ = [
    "CompletedResult",
    "DDRepoRef",
    "FailureCode",
    "GateFailure",
    "GateResult",
    "GitError",
    "GitRunner",
    "RepoRef",
    "SubprocessGitRunner",
    "WorktreeStatus",
    "check_dd_ready",
    "check_handoff",
    "file_exists_at",
    "remote_tip",
    "revision_reachable",
    "worktree_status",
]
