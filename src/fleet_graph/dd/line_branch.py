"""The release/<line-id> branch model (D6, M5).

Three layers, one rule per layer:

- **main (the target branch)** is written only by the goal-level releaser,
  once per line at goal acceptance. Nothing on the dd path may push it.
- **``release/<line-id>`` (the line branch)** is where every gated DD of a
  line lands: one branch per repo the line touches. A dispatch freezes its
  base to this branch's head -- never to main's.
- **``dd/<dev-id>`` (the single branch/worktree)** stays what it was: one
  implementer's in-single chain.

**派单前 rebase 是 configure 段固定第一步.** :class:`LineRebase` is that
step: it rebases the line branch onto the target branch's current head and
then replays the attempt chain onto the rebased line head, so the single is
born on a base that already contains main. The rebase record it emits is the
*only* writer of the frozen post-rebase base -- nothing re-resolves main
after the fact to compensate (the negative criterion: deleting this step
must leave the recorded base without the new commit, visibly behind).

The historical counter-example this module exists to prevent is design §6.4's
dead branch: a line left **160 commits behind** main, stranding 54 of its
commits on a branch nobody could integrate. ``DEFAULT_RELEASE_BEHIND_THRESHOLD``
carries that number as the determination port's default threshold; the alert
*rule* that consumes it belongs to wf-6475fd and is deliberately not here --
this module only exposes the metric and the over-threshold verdict.

Every git call is guarded through :mod:`fleet_graph.dd.git` (an agent-writable
worktree must never execute repo-local config), and nothing here pushes main.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleet_graph.dd.git import run_git

#: The line branch namespace. One branch per (line, repo).
LINE_REF_PREFIX = "refs/heads/release/"

#: The target branch. Only the goal-level releaser writes it; the dd path
#: refuses it as a merge target outright.
MAIN_REF = "refs/heads/main"

#: Bare and fully-qualified spellings the main-ref guard recognises.
_MAIN_REF_SPELLINGS = frozenset({"main", "refs/heads/main", "refs/heads/master", "master"})

#: design §6.4: the line that fell 160 commits behind and stranded 54 of its
#: own. That accident is the default alarm threshold, kept next to the metric
#: it bounds. The consuming rule is wf-6475fd's, not this repo's.
DEFAULT_RELEASE_BEHIND_THRESHOLD = 160

#: A line id must be a single git ref component: no separators, no whitespace,
#: nothing that could walk out of the release/ namespace -- and never the
#: target branch's own short name, which release/<line-id> must not shadow.
_LINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_RESERVED_LINE_IDS = frozenset({"main", "master"})

#: A line id is one ref component; the cap keeps the derived branch name a
#: legal path component on every filesystem the fleet checks out onto.
_MAX_LINE_ID_LEN = 100

_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class LineBranchError(RuntimeError):
    """A mechanical line-branch operation failed. Never guessed around."""


class RebaseConflictError(LineBranchError):
    """The line branch or the attempt chain cannot be replayed cleanly.

    ``conflicts`` names the paths git reported (empty when git did not say);
    ``record`` carries the conflicted rebase record for the stage log. The
    spec's exit for this is the implement stage resolving, bounded rework,
    then ``failed(REBASE_CONFLICT)`` -- never a silent fallback to the old
    base.
    """

    def __init__(self, message: str, *, conflicts: list[str], record: RebaseRecord) -> None:
        super().__init__(message)
        self.conflicts = conflicts
        self.record = record


def is_valid_line_id(line_id: str) -> bool:
    """Whether ``line_id`` can name a release branch component."""
    if not line_id or line_id in _RESERVED_LINE_IDS:
        return False
    if len(line_id) > _MAX_LINE_ID_LEN:
        return False
    return _LINE_ID_RE.fullmatch(line_id) is not None


def line_ref_for(line_id: str) -> str:
    """``wf-8d9737`` -> ``refs/heads/release/wf-8d9737``. Refuse garbage."""
    if not is_valid_line_id(line_id):
        raise LineBranchError(f"invalid line id {line_id!r}: must match {_LINE_ID_RE.pattern}")
    return f"{LINE_REF_PREFIX}{line_id}"


def line_id_from_ref(ref: str) -> str | None:
    """The line id a release ref names, or None for any other ref."""
    if isinstance(ref, str) and ref.startswith(LINE_REF_PREFIX):
        tail = ref[len(LINE_REF_PREFIX) :]
        if is_valid_line_id(tail):
            return tail
    return None


def is_main_ref(ref: str) -> bool:
    """Whether this ref is the target branch in one of its spellings.

    The dd path's hard boundary: a merge stage asked to publish here is
    refused before any git runs.
    """
    return isinstance(ref, str) and ref.strip() in _MAIN_REF_SPELLINGS


@dataclass(frozen=True)
class RebaseRecord:
    """What the configure stage's first step did, in the configure log.

    ``status`` is closed vocabulary:

    - ``up_to_date``: the line branch already contained the target head --
      nothing was rewritten, nothing pushed.
    - ``rebased``: the line branch was replayed onto the target head and the
      attempt chain onto the rebased line head; the rebased line branch was
      published with its old head as the lease.
    - ``conflict``: the replay hit conflicts; the exit is REBASE_CONFLICT.
    - ``absent``: the line branch does not exist yet (first single for this
      repo); there is nothing to rebase and the merger will create it.
    """

    line_ref: str
    target_ref: str
    status: str
    before_line_head: str = ""
    target_head: str = ""
    after_line_head: str = ""
    attempt_head_before: str = ""
    attempt_head_after: str = ""
    conflicts: tuple[str, ...] = ()
    pushed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "line_ref": self.line_ref,
            "target_ref": self.target_ref,
            "status": self.status,
            "before_line_head": self.before_line_head,
            "target_head": self.target_head,
            "after_line_head": self.after_line_head,
            "attempt_head_before": self.attempt_head_before,
            "attempt_head_after": self.attempt_head_after,
            "conflicts": list(self.conflicts),
            "pushed": self.pushed,
        }


def _git(repo: Path, *args: str, check: bool = False) -> tuple[str, str, int]:
    proc = run_git(repo, *args, check=False)
    if check and proc.returncode != 0:
        raise LineBranchError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout, proc.stderr, proc.returncode


def resolve_remote_ref_head(repo: Path, remote_url: str, ref: str) -> str | None:
    """The remote head of one ref, or None when the ref does not exist there."""
    proc = run_git(repo, "ls-remote", remote_url, ref)
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        head, _, observed = line.partition("\t")
        if observed.strip() == ref and _HEX40.fullmatch(head.strip()):
            return head.strip()
    return None


def fetch_ref(repo: Path, remote_url: str, ref: str, local_ref: str) -> None:
    """Fetch one remote ref into an exact local ref (guarded, explicit refmap)."""
    _git(
        repo,
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--force",
        remote_url,
        f"+{ref}:{local_ref}",
        check=True,
    )


def rev_parse(repo: Path, ref: str) -> str | None:
    out, _err, code = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    commit = out.strip()
    if code != 0 or not _HEX40.fullmatch(commit):
        return None
    return commit


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    _out, _err, code = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    return code == 0


def _conflicted_paths(repo: Path) -> list[str]:
    out, _err, code = _git(repo, "diff", "--name-only", "--diff-filter=U")
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _abort_rebase(repo: Path) -> None:
    _git(repo, "rebase", "--abort")


class LineRebase:
    """Configure's fixed first step: rebase the line branch onto the target.

    Wired by the runner only when the development carries a ``line_ref``; a
    development without one skips the step entirely -- and then nothing else
    updates the frozen base, which is exactly the property the negative
    criterion demands (deleting the emission leaves the base behind).

    The mechanical shape, against the attempt worktree:

    1. fetch the target branch and the line branch from the remote;
    2. if the target head is already an ancestor of the line head, the line
       is up to date -- record and stop;
    3. otherwise replay the line-only commits onto the target head, then the
       attempt chain onto the replayed line head. Either replay hitting a
       conflict aborts both, records the conflicted paths, and raises
       :class:`RebaseConflictError` (the stage refuses with REBASE_CONFLICT);
    4. publish the rebased line branch with a lease on its old head, so a
       concurrent writer to the same line branch aborts the dispatch instead
       of being overwritten.
    """

    def __init__(
        self,
        repo: Path,
        *,
        remote_url: str,
        line_ref: str,
        target_ref: str = MAIN_REF,
        push: bool = True,
    ) -> None:
        if not line_ref.startswith(LINE_REF_PREFIX):
            raise LineBranchError(f"line_ref must live under {LINE_REF_PREFIX}*, got {line_ref!r}")
        if not target_ref.startswith("refs/heads/"):
            raise LineBranchError(f"target_ref must be a branch ref, got {target_ref!r}")
        self.repo = Path(repo)
        self.remote_url = remote_url
        self.line_ref = line_ref
        self.target_ref = target_ref
        self.push = push

    # Local scratch refs, kept out of the way of every real namespace.
    _TARGET_LOCAL = "refs/dd-rebase/target"
    _LINE_LOCAL = "refs/dd-rebase/line"

    def run(self) -> RebaseRecord:
        repo = self.repo
        head_out, _err, _head_code = _git(repo, "rev-parse", "HEAD", check=True)
        attempt_head = head_out.strip()

        target_head = resolve_remote_ref_head(repo, self.remote_url, self.target_ref)
        if target_head is None:
            raise LineBranchError(
                f"target branch {self.target_ref!r} does not exist on {self.remote_url!r}"
            )
        line_head = resolve_remote_ref_head(repo, self.remote_url, self.line_ref)
        if line_head is None:
            # First single for this (line, repo): the merger creates the
            # branch when it publishes the gated single. The frozen base
            # stays the target head admission already froze.
            return RebaseRecord(
                line_ref=self.line_ref,
                target_ref=self.target_ref,
                status="absent",
                target_head=target_head,
                attempt_head_before=attempt_head,
                attempt_head_after=attempt_head,
            )

        fetch_ref(repo, self.remote_url, self.target_ref, self._TARGET_LOCAL)
        fetch_ref(repo, self.remote_url, self.line_ref, self._LINE_LOCAL)
        target = rev_parse(repo, self._TARGET_LOCAL)
        line = rev_parse(repo, self._LINE_LOCAL)
        if target != target_head or line != line_head:
            raise LineBranchError("fetched refs disagree with the observed remote heads")

        if is_ancestor(repo, target_head, line_head):
            return RebaseRecord(
                line_ref=self.line_ref,
                target_ref=self.target_ref,
                status="up_to_date",
                before_line_head=line_head,
                target_head=target_head,
                after_line_head=line_head,
                attempt_head_before=attempt_head,
                attempt_head_after=attempt_head,
            )

        merge_base_out, _err, _code = _git(repo, "merge-base", line_head, target_head, check=True)
        merge_base = merge_base_out.strip()

        def _replay(onto: str, upstream: str, branch: str) -> str:
            proc = run_git(
                repo,
                "-c",
                "user.name=Dev Dispatch",
                "-c",
                "user.email=dev-dispatch@example.invalid",
                "rebase",
                "--onto",
                onto,
                upstream,
                branch,
            )
            if proc.returncode != 0:
                conflicts = _conflicted_paths(repo)
                _abort_rebase(repo)
                raise RebaseConflictError(
                    f"replaying {upstream}..{branch} onto {onto} hit conflicts "
                    f"({', '.join(conflicts) or 'paths not reported'})",
                    conflicts=conflicts,
                    record=RebaseRecord(
                        line_ref=self.line_ref,
                        target_ref=self.target_ref,
                        status="conflict",
                        before_line_head=line_head,
                        target_head=target_head,
                        after_line_head="",
                        attempt_head_before=attempt_head,
                        attempt_head_after="",
                        conflicts=tuple(conflicts),
                    ),
                )
            out, _e, c = _git(repo, "rev-parse", "HEAD", check=True)
            assert c == 0
            return out.strip()

        # 1) the line branch onto the target head, 2) the attempt chain onto
        # the rebased line head. Either replay hitting a conflict aborts both,
        # records the conflicted paths, restores the attempt chain, and raises
        # RebaseConflictError. (git's --abort restores the rebased tip, not
        # the pre-command HEAD, so the reset below is the restore contract.)
        try:
            rebased_line = _replay(target_head, merge_base, line_head)
            rebased_attempt = _replay(rebased_line, line_head, attempt_head)
        except RebaseConflictError:
            _git(repo, "reset", "--hard", attempt_head, check=True)
            raise

        # Move the local line ref so the repo agrees with what was rebased.
        _git(repo, "update-ref", self._LINE_LOCAL, rebased_line, check=True)

        pushed = False
        if self.push:
            proc = run_git(
                repo,
                "push",
                # Explicit lease: the push only lands if the remote line
                # branch is still exactly the head we rebased away from --
                # a concurrent writer to the same line branch aborts the
                # dispatch instead of being overwritten.
                f"--force-with-lease={self.line_ref}:{line_head}",
                self.remote_url,
                f"{rebased_line}:{self.line_ref}",
            )
            if proc.returncode != 0:
                _git(repo, "reset", "--hard", attempt_head, check=True)
                raise LineBranchError(
                    "publishing the rebased line branch failed (moved under us?): "
                    f"{(proc.stderr or proc.stdout).strip()[:300]}"
                )
            pushed = True

        return RebaseRecord(
            line_ref=self.line_ref,
            target_ref=self.target_ref,
            status="rebased",
            before_line_head=line_head,
            target_head=target_head,
            after_line_head=rebased_line,
            attempt_head_before=attempt_head,
            attempt_head_after=rebased_attempt,
            pushed=pushed,
        )


def release_behind_count(
    repo: Path,
    *,
    line_ref: str,
    target_ref: str = MAIN_REF,
    remote_url: str = "",
) -> int | None:
    """How many commits the line branch is behind the target branch.

    The ``release_behind`` metric's producer. Counts the commits the target
    branch has that the line branch does not (``rev-list --count line..target``);
    0 means the line contains the target head -- the post-rebase resting
    state. Returns None (unknown, never a fake 0) when either ref cannot be
    resolved locally or on the named remote.
    """
    line_id = line_id_from_ref(line_ref) if line_ref else None
    if line_id is None:
        return None
    line = rev_parse(repo, line_ref)
    target = rev_parse(repo, target_ref)
    if line is None or target is None:
        if not remote_url:
            return None
        line_remote = resolve_remote_ref_head(repo, remote_url, line_ref)
        target_remote = resolve_remote_ref_head(repo, remote_url, target_ref)
        if line_remote is None or target_remote is None:
            return None
        fetch_ref(repo, remote_url, line_ref, f"refs/dd-behind/{line_id}/line")
        fetch_ref(repo, remote_url, target_ref, f"refs/dd-behind/{line_id}/target")
        line = rev_parse(repo, f"refs/dd-behind/{line_id}/line")
        target = rev_parse(repo, f"refs/dd-behind/{line_id}/target")
        if line is None or target is None:
            return None
    out, _err, code = _git(repo, "rev-list", "--count", f"{line}..{target}")
    if code != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def release_behind_alarm(
    behind: int | None,
    *,
    threshold: int = DEFAULT_RELEASE_BEHIND_THRESHOLD,
) -> bool | None:
    """The over-threshold determination port (判定口).

    True = the line is behind by more than ``threshold`` and the wf-6475fd
    rule has a fact to consume. None = unknown (no line branch, unreadable
    repo) -- never collapsed into False, so "healthy" and "cannot tell" stay
    distinguishable. The alerting *rule* itself is wf-6475fd's; this port
    only answers the mechanical question.
    """
    if behind is None:
        return None
    return behind > threshold


def git_release_behind_reader(
    repo: Path,
    *,
    target_ref: str = MAIN_REF,
    remote_url: str = "",
) -> Callable[[str], int | None]:
    """A ``folder_id -> release_behind`` reader over one repo.

    This is the default-shaped producer for the :7494 read model's
    ``release_behind`` field (injected per deployment, like every other
    optional source there). Any failure is reported as None -- the read
    model degrades, it never 5xxs, and an unknown count is never dressed up
    as zero.
    """
    repo = Path(repo)

    def read(folder_id: str) -> int | None:
        try:
            if not is_valid_line_id(folder_id):
                return None
            return release_behind_count(
                repo,
                line_ref=line_ref_for(folder_id),
                target_ref=target_ref,
                remote_url=remote_url,
            )
        except Exception:
            return None

    return read


__all__ = [
    "DEFAULT_RELEASE_BEHIND_THRESHOLD",
    "LINE_REF_PREFIX",
    "MAIN_REF",
    "LineBranchError",
    "LineRebase",
    "RebaseConflictError",
    "RebaseRecord",
    "git_release_behind_reader",
    "is_main_ref",
    "is_valid_line_id",
    "line_id_from_ref",
    "line_ref_for",
    "release_behind_alarm",
    "release_behind_count",
    "resolve_remote_ref_head",
]
