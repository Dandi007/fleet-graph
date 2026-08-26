"""Guarding the git calls this repo makes on an agent-produced worktree.

The exploit below is measured, not hypothetical: it was run against the
pre-fix sealer and it fired.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import git, head
from fleet_graph.dd.bootstrap import committed_target_base
from fleet_graph.dd.git import GUARDS, git_argv, run_git
from fleet_graph.graphs.dd_scripts import WorkspaceSealer


def arm_fsmonitor(repo: Path, marker: Path) -> None:
    """What an agent could write into the worktree it was given.

    `core.fsmonitor` is a command git runs on an index refresh. It lives in
    repo-local config, which `GIT_CONFIG_GLOBAL=/dev/null` and
    `GIT_CONFIG_NOSYSTEM=1` do **not** cover.
    """
    git(repo, "config", "core.fsmonitor", f"/bin/sh -c 'touch {marker}; echo'")


class TestTheExploitTheGuardsExistFor:
    def test_the_env_guards_alone_do_not_stop_it(self, repo: Path, tmp_path: Path) -> None:
        """The premise. If this ever stops firing, the rest of this file is
        testing nothing and should be re-derived rather than deleted."""
        marker = tmp_path / "fired-without-guards"
        arm_fsmonitor(repo, marker)
        (repo / "new.txt").write_text("x\n", encoding="utf-8")

        subprocess.run(
            ["git", "-C", str(repo), "add", "-A"],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        assert marker.exists(), "the exploit no longer reproduces; re-derive this test"

    def test_the_sealer_does_not_run_it(self, repo: Path, tmp_path: Path) -> None:
        from fleet_graph.dd.lifecycle import Lifecycle

        marker = tmp_path / "fired-through-the-sealer"
        arm_fsmonitor(repo, marker)
        (repo / "written-by-a-stage.txt").write_text("x\n", encoding="utf-8")
        before = head(repo)

        sealed = WorkspaceSealer(repo=repo).materialize(
            Lifecycle.load().stages["configure"],
            {"input_commit": before, "attempt_started_at": ""},
            _outcome(),
        )

        assert not marker.exists(), "a repo-local fsmonitor executed through the sealer"
        # And it still did its job: guarding must not cost the commit.
        assert sealed.commit != before
        assert "written-by-a-stage.txt" in git(repo, "show", "--name-only", "--format=", "HEAD")

    def test_reading_the_committed_identity_is_guarded_too(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """Honest about what this proves: `git show` does not refresh the
        index, so fsmonitor would not fire here even unguarded (measured:
        `add` and `status` fire, `show` and `rev-parse` do not). What is
        asserted is that the call carries the guards anyway -- `hooksPath` and
        `protocol.ext` are not index-bound, and the next person to change this
        function should not have to rediscover which subcommands are safe.
        """
        marker = tmp_path / "not-fired-through-the-read"
        arm_fsmonitor(repo, marker)

        assert committed_target_base(repo) is None
        assert not marker.exists()
        assert GUARDS[1] in git_argv(repo, "show", "HEAD:x")


class TestNoUnguardedGitIsLeftInTheSource:
    def test_every_git_call_goes_through_the_helper(self) -> None:
        """A source-level invariant, because the next raw
        `subprocess.run(["git", ...])` would reopen this silently.

        Deliberately *not* using `executable_source`: it drops string tokens,
        and the thing being searched for is a string literal. That version of
        this test passed against code that still had the raw call.
        """
        import fleet_graph

        root = Path(fleet_graph.__file__).parent
        # How an argv is built, not how git is mentioned: a comment saying
        # "git" must not trip this, and a list starting with it must.
        shapes = ('["git"', "['git'", '("git"', "('git'")
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if "vendor" in path.parts or path.name == "git.py":
                continue
            text = path.read_text(encoding="utf-8")
            if any(shape in text for shape in shapes):
                offenders.append(str(path.relative_to(root)))
        assert offenders == [], f"unguarded git invocation in {offenders}"

    def test_the_guards_are_the_three_that_matter(self) -> None:
        argv = git_argv("/tmp/x", "status")
        for guard in (
            "core.fsmonitor=false",
            "core.hooksPath=/dev/null",
            "protocol.ext.allow=never",
        ):
            assert guard in argv, guard
        assert argv[:2] == ["git", "-c"], "the guards precede the subcommand"
        assert GUARDS


class TestTheHelperStillBehavesLikeGit:
    def test_it_returns_output(self, repo: Path) -> None:
        assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == head(repo)

    def test_check_raises_on_failure(self, repo: Path) -> None:
        import pytest

        with pytest.raises(subprocess.CalledProcessError):
            run_git(repo, "cat-file", "-e", "deadbeef" * 5, check=True)


def _outcome() -> object:
    from fleet_graph.graphs.dd_pipeline import StageOutcome

    return StageOutcome()
