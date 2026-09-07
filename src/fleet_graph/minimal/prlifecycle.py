"""The DD PR / worktree / dd-branch lifecycle: GO-35's mechanical teardown.

gitgate.py is the read side (GO-28/36 checks); this module is the write side
of one DD's life: ``open_pr`` after the GO-36 five-check pass, and on the
DD's end — merged or failed — close the PR, remove the worktree, delete the
remote dd branch, exactly GO-35's sequence. There is deliberately **no**
``merge_pr``: the engine never merges (GO-15 / design.md — every merge goes
through the Merge Agent). No events are written here (the caller records
them), no branches or worktrees are created (GO-36 gives that to the Goal
Agent), no acceptance commands are run.

Every command is a ``list[str]`` run ``shell=False`` through an injected
runner. Step failures are data (``StepResult``), never exceptions: a cleanup
failure must land in an event, not stall the DD. ``open_pr`` is the one
raising path — it returns a ``PrRef`` or raises ``PrError`` (an environment
error, the shape of gitgate's ``GitError``).

Remote / branch names are whitelisted (``^[A-Za-z0-9._/-]+$``, no leading
``-``) and PR numbers must be ``int > 0``, all validated **before** any argv
is built, so no token can be parsed as a flag. Every git argv carries the
three repo-config guards ahead of ``-C`` for the same measured reason as
gitgate (an agent-written worktree can carry a hostile repo-local config).
Only the runner/result *types* are imported from gitgate — no behavior
coupling; the guards and the argv builder are self-contained here so the
guarded call stays greppable in this file like gitgate's own.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

from fleet_graph.minimal.gitgate import CompletedResult, GitRunner

# The three repo-config guards, duplicated locally (same measured reason as
# gitgate._GUARDS / enroll._GIT_GUARDS: agent-written worktrees can carry a
# hostile repo-local .git/config — `core.fsmonitor` executes on index
# refresh, `core.hooksPath` runs every hook, `protocol.ext.allow=never`
# blocks `ext::` remotes from spawning a shell transport). Self-contained on
# purpose per this module's spec: the guarded argv builder must stay
# greppable here; gitgate contributes only the runner/result types.
_GUARDS: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
)


def _git(path: str, *args: str) -> list[str]:
    """The guarded argv for one git call against ``path`` (guards precede -C)."""

    return ["git", *_GUARDS, "-C", path, *args]


_TOKEN_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_PR_URL_RE = re.compile(r"https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/(\d+)")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_MERGEABLE_VALUES = ("MERGEABLE", "CONFLICTING", "UNKNOWN")
_CLOSED_STATES = ("CLOSED", "MERGED")
_OUTCOMES = ("merged", "failed")
_ALREADY_EXISTS_MARKER = "already exists"


def _check_token(kind: str, value: str) -> None:
    """Reject anything that could be parsed as a flag or split a token.

    The whitelist admits inner dashes (real names like
    ``dd/loopx-minimal/dd-12`` need them) but not a leading one, so a
    validated token can never impersonate a git/gh option. Violations raise
    before any argv is built: they are caller bugs, not runtime verdicts.
    """

    if not isinstance(value, str) or value.startswith("-") or _TOKEN_RE.match(value) is None:
        raise ValueError(
            f"invalid {kind} {value!r}: must match {_TOKEN_RE.pattern} and not start with '-'"
        )


def _check_pr_number(number: int) -> None:
    """PR numbers are ``int > 0`` — anything else is rejected before argv."""

    # bool is an int subclass; True would pass isinstance and the > 0 test
    # while being a caller bug, so it is refused explicitly.
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError(f"invalid PR number {number!r}: must be an int > 0")


@dataclass(frozen=True)
class PrRef:
    """The identity of one DD's PR: number plus html url."""

    number: int
    url: str


@dataclass(frozen=True)
class StepResult:
    """One teardown step's outcome: data for an event, not an exception."""

    step: str
    ok: bool
    argv: list[str]
    detail: str | None


@dataclass(frozen=True)
class CleanupResult:
    """GO-35 teardown verdict: the AND of all steps, plus every step."""

    ok: bool
    steps: list[StepResult]


class CleanupRepo(NamedTuple):
    """One repo's teardown record (plain 5-tuples are accepted too)."""

    repo_path: str
    worktree: str
    remote: str
    branch: str
    pr_number: int


class PrError(RuntimeError):
    """``open_pr`` failed on both the create and the already-exists fallback.

    An environment error (no gh, no auth, unparseable output), not a verdict:
    the caller cannot proceed without a PR identity, so unlike step failures
    this one raises. Carries the last argv tried and its stderr.
    """

    def __init__(self, argv: list[str], exit_code: int, stderr: str) -> None:
        super().__init__(f"gh {' '.join(argv)} failed (exit {exit_code}): {stderr.strip()}")
        self.argv = argv
        self.exit_code = exit_code
        self.stderr = stderr


def _failed_detail(res: CompletedResult) -> str:
    """A non-empty human line for a failed step, preferring stderr."""

    return res.stderr.strip() or res.stdout.strip() or f"exit {res.exit_code}"


def _pr_ref_from_json(stdout: str) -> PrRef | None:
    """A PrRef from ``gh pr view --json number,url`` stdout, or None."""

    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    number = payload.get("number")
    url = payload.get("url")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    if not isinstance(url, str) or not url:
        return None
    return PrRef(number=number, url=url)


def open_pr(
    worktree: str,
    *,
    head_branch: str,
    base_branch: str,
    title: str,
    body_file: str,
    runner: GitRunner,
) -> PrRef:
    """Open the DD's PR (head dd branch -> base release branch), replay-safe.

    ``gh pr create`` prints the PR url on stdout; the number is parsed from
    it. When gh answers that a PR ``already exists`` (a replayed open after a
    crash), the existing PR is looked up with ``gh pr view`` and reused —
    protocol.md's idempotence rule: the second run of an already-done
    side effect must succeed. Both paths failing raises ``PrError`` with the
    argv and stderr of the last attempt.
    """

    _check_token("head_branch", head_branch)
    _check_token("base_branch", base_branch)
    argv = [
        "gh",
        "pr",
        "create",
        "--head",
        head_branch,
        "--base",
        base_branch,
        "--title",
        title,
        "--body-file",
        body_file,
    ]
    res = runner.run(argv, cwd=worktree)
    match = _PR_URL_RE.search(res.stdout)
    if res.exit_code == 0 and match is not None:
        return PrRef(number=int(match.group(1)), url=match.group(0))
    if _ALREADY_EXISTS_MARKER in (res.stdout + "\n" + res.stderr).lower():
        view_argv = ["gh", "pr", "view", head_branch, "--json", "number,url"]
        view_res = runner.run(view_argv, cwd=worktree)
        ref = _pr_ref_from_json(view_res.stdout) if view_res.exit_code == 0 else None
        if ref is not None:
            return ref
        raise PrError(view_argv, view_res.exit_code, view_res.stderr)
    raise PrError(argv, res.exit_code, res.stderr)


def pr_mergeable(worktree: str, number: int, *, runner: GitRunner) -> str:
    """The platform's mergeable verdict: ``MERGEABLE | CONFLICTING | UNKNOWN``.

    context.md: after Goal approve the engine first checks platform
    mergeable — merge directly when MERGEABLE, hand to the Merge Agent on
    conflict. This is that mechanical input: anything unreadable (gh
    failure, bad JSON, an out-of-domain value) returns ``UNKNOWN`` — never
    guessed, never raised — so the caller routes uncertainty to the Merge
    Agent instead of merging on a coin flip.
    """

    _check_pr_number(number)
    argv = ["gh", "pr", "view", str(number), "--json", "mergeable"]
    res = runner.run(argv, cwd=worktree)
    if res.exit_code != 0:
        return "UNKNOWN"
    try:
        payload = json.loads(res.stdout)
    except ValueError:
        return "UNKNOWN"
    value = payload.get("mergeable") if isinstance(payload, dict) else None
    return value if value in _MERGEABLE_VALUES else "UNKNOWN"


def _pr_state(worktree: str, number: int, *, runner: GitRunner) -> str | None:
    """The PR's ``state`` via gh's JSON interface, or None when unreadable.

    Read from structured stdout rather than gh's human wording, so the
    already-closed verdict never depends on error-message phrasing or locale
    (the same discipline as gitgate's ls-remote parsing).
    """

    argv = ["gh", "pr", "view", str(number), "--json", "state"]
    res = runner.run(argv, cwd=worktree)
    if res.exit_code != 0:
        return None
    try:
        payload = json.loads(res.stdout)
    except ValueError:
        return None
    state = payload.get("state") if isinstance(payload, dict) else None
    return state if isinstance(state, str) else None


def close_pr(
    worktree: str, number: int, *, runner: GitRunner, comment: str | None = None
) -> StepResult:
    """Close PR ``number``; already closed / merged counts as success.

    gh versions disagree on whether closing a closed PR is an error, so a
    failing close is followed by exactly one state probe: ``CLOSED`` or
    ``MERGED`` means the goal is already reached (idempotent replay);
    anything else is a real failure, returned as data.
    """

    _check_pr_number(number)
    argv = ["gh", "pr", "close", str(number)]
    if comment is not None:
        argv = [*argv, "--comment", comment]
    res = runner.run(argv, cwd=worktree)
    if res.exit_code == 0:
        return StepResult(step="close_pr", ok=True, argv=argv, detail=None)
    state = _pr_state(worktree, number, runner=runner)
    if state in _CLOSED_STATES:
        return StepResult(
            step="close_pr",
            ok=True,
            argv=argv,
            detail=f"PR #{number} is already {state}; nothing to close",
        )
    return StepResult(step="close_pr", ok=False, argv=argv, detail=_failed_detail(res))


def _worktree_listed(repo_path: str, worktree: str, *, runner: GitRunner) -> bool:
    """Whether ``worktree`` still appears in ``git worktree list --porcelain``.

    Porcelain lines are a stable machine interface (``worktree <path>``), so
    the already-removed verdict never parses git's error wording. A failed
    listing returns True (present): absence cannot be proven, so the removal
    failure is reported honestly instead of being wished away.
    """

    argv = _git(repo_path, "worktree", "list", "--porcelain")
    res = runner.run(argv, cwd=repo_path)
    if res.exit_code != 0:
        return True
    return any(line == f"worktree {worktree}" for line in res.stdout.splitlines())


def remove_worktree(
    repo_path: str, worktree: str, *, runner: GitRunner, force: bool = False
) -> StepResult:
    """Remove ``worktree`` from its main repo, then prune stale metadata.

    A worktree no longer registered counts as success (a replay after a
    crash between remove and prune), verified via the porcelain listing
    rather than stderr wording. ``prune`` runs on that path too: it is
    exactly the command that cleans the leftover admin entries.
    """

    remove_argv = _git(repo_path, "worktree", "remove", *(["--force"] if force else []), worktree)
    res = runner.run(remove_argv, cwd=repo_path)
    if res.exit_code != 0:
        if _worktree_listed(repo_path, worktree, runner=runner):
            return StepResult(
                step="remove_worktree", ok=False, argv=remove_argv, detail=_failed_detail(res)
            )
        already_absent = True
    else:
        already_absent = False
    prune_argv = _git(repo_path, "worktree", "prune")
    prune_res = runner.run(prune_argv, cwd=repo_path)
    if prune_res.exit_code != 0:
        return StepResult(
            step="remove_worktree", ok=False, argv=prune_argv, detail=_failed_detail(prune_res)
        )
    detail = (
        f"worktree {worktree} is not registered under {repo_path}; already removed"
        if already_absent
        else None
    )
    return StepResult(step="remove_worktree", ok=True, argv=remove_argv, detail=detail)


def _remote_holds_branch(worktree: str, remote: str, branch: str, *, runner: GitRunner) -> bool:
    """Whether the remote still holds ``branch``, asked directly via ls-remote.

    Same discipline as gitgate.remote_tip: the verdict reads ls-remote
    stdout (any full sha means present), never git's error wording or exit
    codes. A failing probe returns True (present) so an unverifiable delete
    is reported failed rather than assumed done.
    """

    argv = _git(worktree, "ls-remote", remote, f"refs/heads/{branch}")
    res = runner.run(argv, cwd=worktree)
    if res.exit_code != 0:
        return True
    return any(
        _FULL_SHA_RE.match(token) for line in res.stdout.splitlines() for token in line.split()
    )


def delete_remote_branch(
    worktree: str, remote: str, branch: str, *, runner: GitRunner
) -> StepResult:
    """Delete ``branch`` on ``remote``; a branch already gone counts as success.

    A failed push is verified with one ls-remote: an empty answer proves the
    remote no longer holds the branch (idempotent replay); anything else is
    a real failure, returned as data. Any existing checkout of the repo can
    serve as ``worktree`` here — see cleanup_dd for how it picks one.
    """

    _check_token("remote", remote)
    _check_token("branch", branch)
    argv = _git(worktree, "push", remote, "--delete", branch)
    res = runner.run(argv, cwd=worktree)
    if res.exit_code == 0:
        return StepResult(step="delete_remote_branch", ok=True, argv=argv, detail=None)
    if not _remote_holds_branch(worktree, remote, branch, runner=runner):
        return StepResult(
            step="delete_remote_branch",
            ok=True,
            argv=argv,
            detail=f"branch {branch} is already absent on {remote}",
        )
    return StepResult(step="delete_remote_branch", ok=False, argv=argv, detail=_failed_detail(res))


def cleanup_dd(
    *,
    outcome: str,
    repos: Sequence[CleanupRepo | tuple[str, str, str, str, int]],
    runner: GitRunner,
    comment: str | None = None,
) -> CleanupResult:
    """GO-35's teardown: close PR, remove worktree, delete the remote dd branch.

    ``outcome`` only records which end the DD reached (``merged`` /
    ``failed``); the mechanical sequence is the same — merged and failed both
    leave a PR to close (a merged PR is already closed by the merge; closing
    again just takes the idempotent path) and a worktree plus dd branch to
    reclaim. Every step's failure is collected as data and the loop never
    stops early: a cleanup failure must land in an event, not stall the DD.
    All tokens are validated before the first command runs, so a malformed
    record fails fast instead of half-cleaning.
    """

    if outcome not in _OUTCOMES:
        raise ValueError(f"outcome must be one of {_OUTCOMES}, got {outcome!r}")
    records = list(repos)
    for _repo_path, _worktree, remote, branch, pr_number in records:
        _check_token("remote", remote)
        _check_token("branch", branch)
        _check_pr_number(pr_number)
    steps: list[StepResult] = []
    for repo_path, worktree, remote, branch, pr_number in records:
        steps.append(close_pr(worktree, pr_number, runner=runner, comment=comment))
        steps.append(remove_worktree(repo_path, worktree, runner=runner))
        # The push runs from the main repo checkout: a successful
        # remove_worktree has just deleted the worktree directory, and git
        # cannot even chdir into it anymore. delete_remote_branch accepts
        # any existing checkout of the repo, so GO-35's order stays intact.
        steps.append(delete_remote_branch(repo_path, remote, branch, runner=runner))
    return CleanupResult(ok=all(step.ok for step in steps), steps=steps)


__all__ = [
    "CleanupRepo",
    "CleanupResult",
    "PrError",
    "PrRef",
    "StepResult",
    "cleanup_dd",
    "close_pr",
    "delete_remote_branch",
    "open_pr",
    "pr_mergeable",
    "remove_worktree",
]
