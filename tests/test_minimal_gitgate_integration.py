"""Optional integration test for the git gate against real local git.

Everything stays on-disk and offline: a bare repo plays the remote, a normal
clone plays the worktree. Guarded by an env skip so sandboxes without git
still run the fake-runner suite above.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from fleet_graph.minimal.gitgate import (
    DDRepoRef,
    FailureCode,
    SubprocessGitRunner,
    check_dd_ready,
    check_handoff,
    remote_tip,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def pair(tmp_path: Path) -> dict[str, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    (seed / "README.md").write_text("x\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "main")
    _git(seed, "fetch", "origin")

    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(work)],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_GLOBAL": os.devnull},
    )
    _git(work, "checkout", "-q", "-b", "feature-x")
    return {"remote": remote, "work": work}


class TestRealGitIntegration:
    def test_pushed_clean_handoff_passes(self, pair: dict[str, Path]) -> None:
        work = pair["work"]
        _git(work, "push", "-q", "-u", "origin", "feature-x")
        result = check_handoff(
            [DDRepoRef(worktree=str(work), remote="origin", branch="feature-x")],
            runner=SubprocessGitRunner(),
        )
        assert result.ok is True
        assert result.failures == []

    def test_unpushed_commit_is_not_pushed(self, pair: dict[str, Path]) -> None:
        work = pair["work"]
        _git(work, "push", "-q", "-u", "origin", "feature-x")
        (work / "new.txt").write_text("x\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "local only")
        result = check_handoff(
            [DDRepoRef(worktree=str(work), remote="origin", branch="feature-x")],
            runner=SubprocessGitRunner(),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.NOT_PUSHED]

    def test_never_pushed_branch_is_missing_on_remote(self, pair: dict[str, Path]) -> None:
        work = pair["work"]
        result = check_handoff(
            [DDRepoRef(worktree=str(work), remote="origin", branch="feature-x")],
            runner=SubprocessGitRunner(),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.BRANCH_MISSING_ON_REMOTE]

    def test_dirty_worktree(self, pair: dict[str, Path]) -> None:
        work = pair["work"]
        _git(work, "push", "-q", "-u", "origin", "feature-x")
        (work / "new.txt").write_text("x\n", encoding="utf-8")
        result = check_handoff(
            [DDRepoRef(worktree=str(work), remote="origin", branch="feature-x")],
            runner=SubprocessGitRunner(),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.DIRTY_WORKTREE]

    def test_remote_tip_and_dd_ready(self, pair: dict[str, Path]) -> None:
        work = pair["work"]
        _git(work, "push", "-q", "-u", "origin", "feature-x")
        tip = remote_tip(str(work), "origin", "feature-x", runner=SubprocessGitRunner())
        assert tip is not None and len(tip) == 40

        spec = work / "docs" / "specs" / "101-foo.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("# spec\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "add spec")
        _git(work, "push", "-q", "origin", "feature-x")

        ref = DDRepoRef(
            worktree=str(work),
            remote="origin",
            branch="feature-x",
            spec_path="docs/specs/101-foo.md",
        )
        assert check_dd_ready([ref], runner=SubprocessGitRunner()).ok is True

        spec_missing = DDRepoRef(
            worktree=str(work),
            remote="origin",
            branch="feature-x",
            spec_path="docs/specs/nope.md",
        )
        result = check_dd_ready([spec_missing], runner=SubprocessGitRunner())
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.SPEC_MISSING]
