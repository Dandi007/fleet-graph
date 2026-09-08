"""The merge gate: GO-36's approve-then-mergeable routing + the §0.10 checks.

GO-15 made the Merge Agent the only merger; GO-36's reply (context.md's 已定
段末句, protocol.md §0.10 末条) narrowed that: after Goal approve the engine
first asks the platform whether the PR is mergeable — merge it directly when
it is, hand it to the Merge Agent only on conflict. This module is that
decision plus the mechanical merge bookkeeping around it:

- ``decide``: route one PR to ``platform_merge`` or ``merge_agent`` from an
  injected mergeable verdict. Unknown is never guessed as mergeable — it
  routes to the agent with a reason that says so explicitly.
- ``platform_merge``: perform the merge through gh (argv ``list[str]``,
  ``shell=False``, every interpolated token whitelisted before any argv
  exists). Failure is data, never an exception (agentrun.AgentResult's
  discipline): a merge whose commit cannot be read back is a failure, and an
  already-merged PR (a replay after a crash between merge and record) still
  verifies as merged.
- ``verify_merge_output``: protocol §0.10's Merge Agent Stop-check — the
  ``merged`` / ``rebased`` / ``failed`` git states, read from the remote,
  per repo, returned as a list of error strings (empty = pass). Shape
  problems in the agent's output object are error entries; only violations
  of the engine-side token/sha whitelist raise ``ValueError`` before any
  argv is built (caller bugs, prlifecycle's discipline).
- ``final_merge_plan``: the line-done closing merges — release branch → each
  repo's own ``target_branch`` (GO-31: target is per repo), pure data.

Every function is pure over injected IO (``mergeable_fn`` / ``gh_runner`` /
``git_runner``); the module has no global subprocess — the one concrete gh
runner imports it lazily inside ``run`` (enroll.SubprocessProbe's pattern).
Git argv carries the three repo-config guards ahead of ``-C`` (same measured
reason as gitgate; the builder is local so the guarded call stays greppable,
and tests/test_dd_git.py whitelists this file accordingly). Only gitgate's
``remote_tip`` and result types plus prlifecycle's ``PrRef`` are imported —
no behavior coupling, no graph, no prompt rendering (the Merge Agent call
itself is the ``merge_fn`` seam owned by other DDs).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import NamedTuple, Protocol

from fleet_graph.minimal.gitgate import CompletedResult, GitRunner, remote_tip
from fleet_graph.minimal.prlifecycle import PrRef

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
# A PrRef url as open_pr records it; the owner/name groups are the ``--repo``
# slug gh is pinned with, and the pull number must agree with ``PrRef.number``.
_GH_PR_URL_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/([0-9]+)$"
)

_STOPS = ("merged", "rebased", "failed")
_REQUIRED_FIELD = {"merged": "merged_commit", "rebased": "new_head", "failed": None}

# The three repo-config guards, duplicated locally (same measured reason as
# gitgate._GUARDS / enroll._GIT_GUARDS / prlifecycle._GUARDS: agent-written
# worktrees can carry a hostile repo-local .git/config — `core.fsmonitor`
# executes on index refresh, `core.hooksPath` runs every hook,
# `protocol.ext.allow=never` blocks `ext::` remotes from spawning a shell
# transport). Local on purpose so the guarded argv builder stays greppable in
# this file, per tests/test_dd_git.py's source-level whitelist.
_GUARDS: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
)


def _git(worktree: str, *args: str) -> list[str]:
    """The guarded argv for one git call against ``worktree`` (guards precede -C)."""

    return ["git", *_GUARDS, "-C", worktree, *args]


def _check_token(kind: str, value: str) -> None:
    """Reject anything that could be parsed as a flag or split a token.

    Same whitelist as prlifecycle._check_token: inner dashes are admitted
    (real branch names need them) but a leading one is not, so a validated
    token can never impersonate a git option. Violations raise before any
    argv is built — they are caller bugs, not runtime verdicts.
    """

    if not isinstance(value, str) or value.startswith("-") or _TOKEN_RE.match(value) is None:
        raise ValueError(
            f"invalid {kind} {value!r}: must match {_TOKEN_RE.pattern} and not start with '-'"
        )


def _check_sha(kind: str, value: str) -> None:
    """Engine-side heads must be full shas before they enter any argv."""

    if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {kind} {value!r}: must be a 40-hex sha")


def _failed_detail(res: CompletedResult) -> str:
    """A non-empty human line for a failed gh call, preferring stderr."""

    return res.stderr.strip() or res.stdout.strip() or f"exit {res.exit_code}"


# ---------------------------------------------------------------------------
# Routing (GO-36: mergeable → platform, conflict/unknown → Merge Agent)
# ---------------------------------------------------------------------------


class MergeRoute:
    """Where an approved PR's merge is executed (referenced, never retyped)."""

    PLATFORM_MERGE = "platform_merge"
    MERGE_AGENT = "merge_agent"


class MergeReasonCode:
    """Machine-readable routing reasons for events and bounce-backs."""

    PLATFORM_MERGEABLE = "platform_mergeable"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown_not_mergeable"


@dataclass(frozen=True)
class MergeReason:
    """Why a route was taken: a short code plus a human-readable detail."""

    code: str
    detail: str


@dataclass(frozen=True)
class MergeDecision:
    """The routing verdict for one approved PR."""

    route: str
    reason: MergeReason


def decide(pr: PrRef, *, mergeable_fn: Callable[[PrRef], object]) -> MergeDecision:
    """Route ``pr`` from an injected platform mergeable verdict.

    ``mergeable_fn(pr)`` returns ``True`` / ``"MERGEABLE"`` → platform merge;
    ``False`` / ``"CONFLICTING"`` → Merge Agent (conflict); anything else —
    ``None``, ``"UNKNOWN"`` (gh has not finished computing, the query failed,
    see prlifecycle.pr_mergeable), or an out-of-domain value — → Merge Agent
    with the unknown reason. Unknown is **never** treated as mergeable: the
    engine does not merge on a coin flip, it hands the uncertainty to the
    agent (GO-36 / context.md's 已定 段末句).
    """

    verdict = mergeable_fn(pr)
    if verdict is True or verdict == "MERGEABLE":
        return MergeDecision(
            route=MergeRoute.PLATFORM_MERGE,
            reason=MergeReason(
                code=MergeReasonCode.PLATFORM_MERGEABLE,
                detail=f"PR #{pr.number}: platform reports MERGEABLE; GO-36 merges directly",
            ),
        )
    if verdict is False or verdict == "CONFLICTING":
        return MergeDecision(
            route=MergeRoute.MERGE_AGENT,
            reason=MergeReason(
                code=MergeReasonCode.CONFLICTING,
                detail=f"PR #{pr.number}: platform reports CONFLICTING; Merge Agent handles it",
            ),
        )
    return MergeDecision(
        route=MergeRoute.MERGE_AGENT,
        reason=MergeReason(
            code=MergeReasonCode.UNKNOWN,
            detail=(
                f"PR #{pr.number}: platform mergeable verdict is {verdict!r} (not yet computed "
                "or query failed); unknown is never treated as mergeable, so the merge goes "
                "to the Merge Agent"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Platform merge (gh; failure is data, never an exception)
# ---------------------------------------------------------------------------


class GhRunner(Protocol):
    """Every gh call funnels through this, argv-list only, never a shell."""

    def run(self, args: list[str]) -> CompletedResult:
        """Run ``args`` and return exit code / stdout / stderr."""
        ...  # pragma: no cover - protocol body


class GhError(RuntimeError):
    """A gh invocation failed at the runner level (no binary, timeout).

    An environment error, not a merge verdict: ``platform_merge`` itself
    never raises — only the injected runner can, mirroring how
    SubprocessGitRunner's timeouts surface as gitgate.GitError.
    """


class SubprocessGhRunner:
    """Default ``GhRunner``: list-argv ``subprocess.run`` with a timeout.

    The subprocess import is function-local (enroll.SubprocessProbe's
    pattern) so the module carries no global subprocess. ``shell=False`` is
    explicit and the argv is always a list, so no string ever reaches a
    shell.
    """

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout

    def run(self, args: list[str]) -> CompletedResult:
        import subprocess

        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GhError(f"gh {' '.join(args)} timed out after {self.timeout}s") from exc
        except OSError as exc:
            raise GhError(f"gh {' '.join(args)} could not run: {exc}") from exc
        return CompletedResult(proc.returncode, proc.stdout, proc.stderr)


def _repo_slug(pr: PrRef) -> str:
    """The ``owner/name`` slug of ``pr``'s url, whitelisted before any argv.

    The slug pins gh to the PR's own repository regardless of the process's
    working directory (the caller passes a ``PrRef``, not a worktree). The
    regex admits inner dashes but the leading-dash check still runs: a slug
    half like ``--upload-pack`` would match the character class, so it is
    rejected explicitly — a validated slug can never impersonate an option.
    """

    match = _GH_PR_URL_RE.fullmatch(pr.url) if isinstance(pr.url, str) else None
    if match is None:
        raise ValueError(f"PR url {pr.url!r} is not a github pull url")
    owner, name, url_number = match.group(1), match.group(2), int(match.group(3))
    if owner.startswith("-") or name.startswith("-"):
        raise ValueError(f"PR url {pr.url!r} carries an option-like repo slug")
    if url_number != pr.number:
        raise ValueError(f"PR number {pr.number} disagrees with its url {pr.url!r}")
    return f"{owner}/{name}"


def _merge_commit_of(pr: PrRef, slug: str, *, gh_runner: GhRunner) -> str | None:
    """The PR's merge commit sha via ``gh pr view --json mergeCommit``, or None.

    Read from structured stdout (never gh's wording): the oid must be a full
    40-hex sha; an unmerged PR answers ``"mergeCommit": null`` and any
    unreadable answer is None, so callers treat it as "not merged", not as a
    crash.
    """

    argv = ["gh", "pr", "view", str(pr.number), "--repo", slug, "--json", "mergeCommit"]
    res = gh_runner.run(argv)
    if res.exit_code != 0:
        return None
    try:
        payload = json.loads(res.stdout)
    except ValueError:
        return None
    commit = payload.get("mergeCommit") if isinstance(payload, dict) else None
    oid = commit.get("oid") if isinstance(commit, dict) else None
    if not isinstance(oid, str) or _FULL_SHA_RE.fullmatch(oid) is None:
        return None
    return oid


def platform_merge(pr: PrRef, *, gh_runner: GhRunner) -> tuple[str, dict]:
    """Merge ``pr`` on the platform; the outcome is data, never an exception.

    ``gh pr merge --merge`` (a true merge commit, the shape protocol §0.10's
    ``merged`` check assumes: the target's new tip contains the source head);
    the commit is then read back with ``gh pr view``. Returns
    ``("merged", {"merged_commit": ..., "pr": ..., "url": ...})`` or
    ``("failed", {"detail": ...})``. A failing merge still probes the view
    once — an already-merged PR (a replay after a crash between the merge and
    its record) must succeed, protocol's idempotence rule; only when the read
    back finds no merge commit is the failure returned.
    """

    if isinstance(pr.number, bool) or not isinstance(pr.number, int) or pr.number <= 0:
        raise ValueError(f"invalid PR number {pr.number!r}: must be an int > 0")
    slug = _repo_slug(pr)
    merge_argv = ["gh", "pr", "merge", str(pr.number), "--repo", slug, "--merge"]
    res = gh_runner.run(merge_argv)
    merged_commit = _merge_commit_of(pr, slug, gh_runner=gh_runner)
    if res.exit_code != 0 and merged_commit is None:
        return ("failed", {"detail": _failed_detail(res), "pr": pr.number})
    if merged_commit is None:
        return (
            "failed",
            {
                "detail": (
                    "gh pr merge exited 0 but the merge commit could not be read back "
                    "from gh pr view"
                ),
                "pr": pr.number,
            },
        )
    return ("merged", {"merged_commit": merged_commit, "pr": pr.number, "url": pr.url})


# ---------------------------------------------------------------------------
# The §0.10 Merge Agent Stop-check
# ---------------------------------------------------------------------------


class MergeRepo(NamedTuple):
    """One repo's merge context: the branch pair the engine filled in.

    ``source_head`` / ``target_head`` are the tips the engine recorded when
    building the merge input (protocol §6: engine-filled, never agent-quoted);
    the Stop-check compares the remote against them. ``label`` names the repo
    in error strings; plain 6-/7-tuples are accepted too, like
    prlifecycle.CleanupRepo.
    """

    worktree: str
    remote: str
    source_branch: str
    target_branch: str
    source_head: str
    target_head: str
    label: str | None = None

    @property
    def repo_id(self) -> str:
        return self.label if self.label is not None else self.worktree


def _contains(repo: MergeRepo, ancestor: str, commit: str, *, git_runner: GitRunner) -> bool:
    """Whether ``commit`` contains ``ancestor`` (merge-base --is-ancestor)."""

    argv = _git(repo.worktree, "merge-base", "--is-ancestor", ancestor, commit)
    return git_runner.run(argv, cwd=repo.worktree).exit_code == 0


def _verify_one(
    stop: str, value: str | None, repo: MergeRepo, *, git_runner: GitRunner
) -> list[str]:
    """The §0.10 Stop-check for one repo; every failure is an error string.

    The remote is fetched first so the containment check sees the objects the
    Merge Agent pushed, then both tips are resolved from the freshly updated
    remote-tracking refs (gitgate.remote_tip with ``fetch=False`` — the
    verdict reads refs, never git's error wording). A failed fetch is an
    error entry, not an exception: an unverifiable stop cannot pass.
    """

    fetch_argv = _git(repo.worktree, "fetch", repo.remote)
    res = git_runner.run(fetch_argv, cwd=repo.worktree)
    if res.exit_code != 0:
        return [f"{repo.repo_id}: fetch of {repo.remote} failed: {_failed_detail(res)}"]
    source_tip = remote_tip(
        repo.worktree, repo.remote, repo.source_branch, runner=git_runner, fetch=False
    )
    target_tip = remote_tip(
        repo.worktree, repo.remote, repo.target_branch, runner=git_runner, fetch=False
    )
    errors: list[str] = []
    if stop == "merged":
        assert value is not None  # the shape check guarantees the field exists
        if value != target_tip:
            errors.append(
                f"{repo.repo_id}: merged_commit {value} is not the tip of "
                f"{repo.remote}/{repo.target_branch} (tip is {target_tip})"
            )
        if not _contains(repo, repo.source_head, value, git_runner=git_runner):
            errors.append(
                f"{repo.repo_id}: merged_commit {value} does not contain "
                f"source_head {repo.source_head}"
            )
    elif stop == "rebased":
        assert value is not None
        if value != source_tip:
            errors.append(
                f"{repo.repo_id}: new_head {value} is not the tip of "
                f"{repo.remote}/{repo.source_branch} (tip is {source_tip})"
            )
        if target_tip != repo.target_head:
            errors.append(
                f"{repo.repo_id}: target tip moved during a rebased stop: "
                f"{repo.remote}/{repo.target_branch} is {target_tip}, "
                f"expected {repo.target_head}"
            )
    else:
        if source_tip != repo.source_head:
            errors.append(
                f"{repo.repo_id}: source tip moved during a failed stop: "
                f"{repo.remote}/{repo.source_branch} is {source_tip}, "
                f"expected {repo.source_head}"
            )
        if target_tip != repo.target_head:
            errors.append(
                f"{repo.repo_id}: target tip moved during a failed stop: "
                f"{repo.remote}/{repo.target_branch} is {target_tip}, "
                f"expected {repo.target_head}"
            )
    return errors


def verify_merge_output(
    obj: object,
    repos: Sequence[MergeRepo | tuple[str, ...]],
    *,
    git_runner: GitRunner,
) -> list[str]:
    """Protocol §0.10's Merge Agent Stop-check; empty list = pass.

    Shape problems in ``obj`` (the agent's ``merge/1`` output: unknown
    ``stop``, a missing or non-sha ``merged_commit`` / ``new_head``) are
    error entries returned without any git query — agent output is data, so
    an unparseable object is an invalid-output verdict, not an exception.
    The engine-side context (branch names, recorded heads) is whitelisted
    first and raises ``ValueError`` before any argv exists, prlifecycle's
    caller-bug discipline. The three verdicts, per protocol §0.10's table:

    - ``merged``: ``merged_commit`` is the target branch tip and contains
      ``source_head``;
    - ``rebased``: ``new_head`` is the source branch tip and the target tip
      is unchanged;
    - ``failed``: both branch tips are unchanged.
    """

    if not isinstance(obj, dict):
        return ["merge output must be a JSON object"]
    stop = obj.get("stop")
    if stop not in _STOPS:
        return [f"merge output stop must be one of {_STOPS}, got {stop!r}"]
    field = _REQUIRED_FIELD[stop]
    value: str | None = None
    if field is not None:
        value = obj.get(field)
        if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
            return [f"stop={stop} requires {field!r} to be a 40-hex sha, got {value!r}"]
    records: list[MergeRepo] = [
        repo if isinstance(repo, MergeRepo) else MergeRepo(*repo) for repo in repos
    ]
    for repo in records:
        _check_token("remote", repo.remote)
        _check_token("source_branch", repo.source_branch)
        _check_token("target_branch", repo.target_branch)
        _check_sha("source_head", repo.source_head)
        _check_sha("target_head", repo.target_head)
    errors: list[str] = []
    for repo in records:
        errors.extend(_verify_one(stop, value, repo, git_runner=git_runner))
    return errors


# ---------------------------------------------------------------------------
# The line-done closing merges (release → each repo's own target, GO-31)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergePlan:
    """One repo's closing merge: release branch → its own target branch."""

    repo_id: str
    source: str
    target: str
    remote: str


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and value != ""


def final_merge_plan(enroll: dict, *, release_branch: str) -> list[MergePlan]:
    """The line-done merge plan: release branch → each repo's ``target_branch``.

    GO-31: the target is per repo, so one plan per enroll ``repos`` entry,
    each carrying that repo's own ``target_branch`` and ``remote``; the
    source is the goal-level release branch, identical for every repo. The
    ``release_branch`` argument must agree with the enroll's recorded
    ``source_branch`` (GO-30: written once on enroll) — a disagreement is a
    caller bug and raises. Pure data: nothing is fetched, pushed, or merged
    here (the execution is the ``merge_fn`` seam owned by other DDs).
    """

    if not _nonempty(release_branch):
        raise ValueError("release_branch must be a non-empty string")
    if not isinstance(enroll, dict):
        raise ValueError("enroll must be the enroll object (a JSON object)")
    recorded = enroll.get("source_branch")
    if _nonempty(recorded) and recorded != release_branch:
        raise ValueError(
            f"release_branch {release_branch!r} disagrees with the enroll "
            f"source_branch {recorded!r}"
        )
    repos = enroll.get("repos")
    if not isinstance(repos, list):
        raise ValueError("enroll object is missing the repos array")
    plans: list[MergePlan] = []
    for index, repo in enumerate(repos):
        if not isinstance(repo, dict):
            raise ValueError(f"repos[{index}] must be an object")
        for key in ("path", "remote", "target_branch"):
            if not _nonempty(repo.get(key)):
                raise ValueError(f"repos[{index}].{key} must be a non-empty string")
        plans.append(
            MergePlan(
                repo_id=repo["path"],
                source=release_branch,
                target=repo["target_branch"],
                remote=repo["remote"],
            )
        )
    return plans


__all__ = [
    "GhError",
    "GhRunner",
    "MergeDecision",
    "MergePlan",
    "MergeReason",
    "MergeReasonCode",
    "MergeRepo",
    "MergeRoute",
    "SubprocessGhRunner",
    "decide",
    "final_merge_plan",
    "platform_merge",
    "verify_merge_output",
]
