"""Every git invocation this repo makes on an agent-produced worktree.

`safe_git_environment()` strips `GIT_*` and disables the global and system
config, and that is **not enough on its own**: repo-local `.git/config` is
still read, and `core.fsmonitor` there is a command git executes on an index
refresh. The worktree we run these commands in is written by an agent, which
can write `.git/config` like any other file -- so a bare `git add -A` on it is
arbitrary command execution in the orchestrator's context.

Measured, not reasoned about: with the env guards alone, a repo-local
`core.fsmonitor` fired on `git add -A`; with the three `-c` guards below it did
not. `tests/test_dd_git.py` keeps that exploit as a regression test.

The vendored `git_ops._safe_git` already carries these three (findings §18);
this is the same protection for the calls this repo makes itself, rather than
a second opinion about what is safe.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fleet_graph.dd.vendor import git_ops

# `core.fsmonitor`: a command git runs on index refresh.
# `core.hooksPath`: every hook in the repo, likewise.
# `protocol.ext.allow`: `ext::` remotes run a shell command as a transport.
GUARDS: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
)


def git_argv(repo: Path | str, *args: str) -> list[str]:
    """The argv for one guarded git call against `repo`."""
    return ["git", *GUARDS, "-C", str(repo), *args]


def run_git(
    repo: Path | str,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        git_argv(repo, *args),
        capture_output=True,
        text=True,
        env=env if env is not None else git_ops.safe_git_environment(),
        check=check,
    )


__all__ = ["GUARDS", "git_argv", "run_git"]
