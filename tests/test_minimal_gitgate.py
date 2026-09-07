"""Tests for the minimal git gate (GO-28 handoff / GO-36 DD readiness).

All gate logic runs against a fake GitRunner that answers by argv pattern, so
no real git, no network, and the assertions double as the contract that every
call is a plain argv list executed in an explicit cwd (never a shell string).
"""

from __future__ import annotations

from typing import Any

import pytest

from fleet_graph.minimal.gitgate import (
    CompletedResult,
    DDRepoRef,
    FailureCode,
    RepoRef,
    check_dd_ready,
    check_handoff,
    file_exists_at,
    remote_tip,
)

# Deterministic 40-char shas so remote tips and local HEADs can diverge on cue.
SHA_A1 = "a" * 40
SHA_B1 = "b" * 40
SHA_OLD = "c" * 40
SHA_NEW = "d" * 40

# argv fragments that identify which git query a call is.
_FRAGMENTS = {
    "branch": ["rev-parse", "--abbrev-ref", "HEAD"],
    "head": ["rev-parse", "HEAD"],
    "status": ["status", "--porcelain"],
    "ls_remote": ["ls-remote"],
    "tip": ["rev-parse", "--verify"],
    "cat_file": ["cat-file", "-e"],
    "merge_base": ["merge-base", "--is-ancestor"],
}


def _matches(args: list[str], fragment: list[str]) -> bool:
    return all(part in args for part in fragment)


class FakeGitRunner:
    """Answers each recognized git query from a per-repo script.

    A repo's script maps a query kind to its canned result: ``branch`` is the
    ref name stdout (``"HEAD"`` meaning detached), ``head`` the HEAD sha,
    ``status`` the porcelain output, ``ls_remote`` the ls-remote stdout
    (``None`` meaning the remote holds no such ref), ``tip`` the remote tip
    sha resolved from local refs (``None`` meaning the ref does not resolve),
    ``spec`` the ``cat-file -e`` exit code, ``merge_base`` the
    ``merge-base --is-ancestor`` exit code. Unscripted queries get the green
    defaults; unrecognized argv fails loudly so tests cannot pass on
    accidental gaps.
    """

    def __init__(self, repos: dict[str, dict[str, Any]]) -> None:
        self._repos = repos
        self.calls: list[tuple[list[str], str]] = []

    def run(self, args: list[str], *, cwd: str) -> CompletedResult:
        self.calls.append((list(args), cwd))
        script = self._repos.get(cwd)
        if script is None:
            raise AssertionError(f"unexpected cwd {cwd!r}; scripted: {sorted(self._repos)}")
        if _matches(args, _FRAGMENTS["ls_remote"]):
            listed = script.get("ls_remote", SHA_A1)
            if listed is None:
                return CompletedResult(0, "", "")
            return CompletedResult(0, listed + "\trefs/remotes/origin/feature-x\n", "")
        if _matches(args, _FRAGMENTS["branch"]):
            return CompletedResult(0, script.get("branch", "feature-x") + "\n", "")
        if _matches(args, _FRAGMENTS["head"]):
            return CompletedResult(0, script.get("head", SHA_A1) + "\n", "")
        if _matches(args, _FRAGMENTS["status"]):
            return CompletedResult(0, script.get("status", ""), "")
        if _matches(args, _FRAGMENTS["tip"]):
            tip = script.get("tip", SHA_A1)
            if tip is None:
                return CompletedResult(128, "", "refs/remotes/origin/feature-x not found")
            return CompletedResult(0, tip + "\n", "")
        if _matches(args, _FRAGMENTS["cat_file"]):
            return CompletedResult(script.get("spec", 0), "", "")
        if _matches(args, _FRAGMENTS["merge_base"]):
            return CompletedResult(script.get("merge_base", 1), "", "")
        raise AssertionError(f"unrecognized git argv: {args!r}")


def _repo(worktree: str, **script: Any) -> RepoRef:
    return RepoRef(worktree=worktree, remote="origin", branch="feature-x", label=f"repo-{worktree}")


def _runner(*repo_scripts: tuple[str, dict[str, Any]]) -> FakeGitRunner:
    return FakeGitRunner(dict(repo_scripts))


# ---------------------------------------------------------------------------
# check_handoff
# ---------------------------------------------------------------------------


class TestCheckHandoff:
    def test_two_green_repos_pass(self) -> None:
        result = check_handoff(
            [_repo("/wt/one"), _repo("/wt/two")],
            runner=_runner(("/wt/one", {}), ("/wt/two", {})),
        )
        assert result.ok is True
        assert result.failures == []

    def test_dirty_worktree_fails(self) -> None:
        result = check_handoff(
            [_repo("/wt/dirty")],
            runner=_runner(("/wt/dirty", {"status": " M src/foo.py\n"})),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.DIRTY_WORKTREE]
        assert result.failures[0].repo == "repo-/wt/dirty"

    def test_untracked_files_also_count_as_dirty(self) -> None:
        result = check_handoff(
            [_repo("/wt/untracked")],
            runner=_runner(("/wt/untracked", {"status": "?? notes.txt\n"})),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.DIRTY_WORKTREE]

    def test_local_ahead_of_remote_is_not_pushed(self) -> None:
        result = check_handoff(
            [_repo("/wt/ahead")],
            runner=_runner(
                (
                    "/wt/ahead",
                    {"head": SHA_NEW, "tip": SHA_OLD, "merge_base": 1},
                )
            ),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.NOT_PUSHED]

    def test_local_behind_remote_is_head_behind_remote(self) -> None:
        result = check_handoff(
            [_repo("/wt/behind")],
            runner=_runner(
                (
                    "/wt/behind",
                    {"head": SHA_OLD, "tip": SHA_NEW, "merge_base": 0},
                )
            ),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.HEAD_BEHIND_REMOTE]

    def test_branch_missing_on_remote(self) -> None:
        result = check_handoff(
            [_repo("/wt/no-branch")],
            runner=_runner(("/wt/no-branch", {"ls_remote": None})),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.BRANCH_MISSING_ON_REMOTE]

    def test_detached_head(self) -> None:
        result = check_handoff(
            [_repo("/wt/detached")],
            runner=_runner(("/wt/detached", {"branch": "HEAD"})),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.DETACHED_HEAD]

    def test_wrong_branch_is_reported_even_when_head_differs_from_tip(self) -> None:
        # A worktree parked on another branch whose sha happens to differ from
        # the target branch's remote tip: the verdict must be wrong_branch, not
        # not_pushed -- the fix is to switch back, not to push.
        result = check_handoff(
            [_repo("/wt/wrong")],
            runner=_runner(
                (
                    "/wt/wrong",
                    {
                        "branch": "release/loopx-minimal",
                        "head": SHA_NEW,
                        "tip": SHA_OLD,
                        "merge_base": 1,
                    },
                )
            ),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.WRONG_BRANCH]
        assert result.failures[0].repo == "repo-/wt/wrong"

    def test_one_bad_repo_out_of_two_is_reported_once(self) -> None:
        result = check_handoff(
            [_repo("/wt/good"), _repo("/wt/bad")],
            runner=_runner(
                ("/wt/good", {}),
                ("/wt/bad", {"status": " M src/foo.py\n"}),
            ),
        )
        assert result.ok is False
        assert len(result.failures) == 1
        assert result.failures[0].repo == "repo-/wt/bad"
        assert result.failures[0].code == FailureCode.DIRTY_WORKTREE

    def test_failures_are_data_not_exceptions(self) -> None:
        result = check_handoff(
            [_repo("/wt/multi")],
            runner=_runner(
                (
                    "/wt/multi",
                    {
                        "status": " M a.py\n?? b.txt\n",
                        "head": SHA_NEW,
                        "tip": SHA_OLD,
                        "merge_base": 1,
                    },
                )
            ),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [
            FailureCode.NOT_PUSHED,
            FailureCode.DIRTY_WORKTREE,
        ]
        for failure in result.failures:
            assert failure.detail


# ---------------------------------------------------------------------------
# check_dd_ready
# ---------------------------------------------------------------------------


def _dd_repo(worktree: str, spec_path: str = "docs/specs/101-foo.md") -> DDRepoRef:
    return DDRepoRef(
        worktree=worktree,
        remote="origin",
        branch="feature-x",
        label=f"repo-{worktree}",
        spec_path=spec_path,
    )


class TestDDRepoRefValidation:
    def test_empty_spec_path_is_rejected(self) -> None:
        # ``git cat-file -e '<sha>:'`` exits 0, so an empty spec_path would
        # silently skip GO-36 check 5; it must be refused at construction.
        with pytest.raises(ValueError, match="spec_path"):
            _dd_repo("/wt/dd-empty", spec_path="")


class TestCheckDDReady:
    def test_all_five_checks_pass(self) -> None:
        result = check_dd_ready([_dd_repo("/wt/dd-ok")], runner=_runner(("/wt/dd-ok", {})))
        assert result.ok is True
        assert result.failures == []

    def test_wrong_branch_with_matching_tip_still_fails(self) -> None:
        # The silent slip-through: the worktree sits on the release branch while
        # the dd branch was just cut from the same commit and pushed, so HEAD
        # equals the remote tip and the tree is clean. Anything short of
        # wrong_branch here would send the engine opening a PR off the wrong
        # branch.
        result = check_dd_ready(
            [_dd_repo("/wt/dd-wrongbranch")],
            runner=_runner(
                (
                    "/wt/dd-wrongbranch",
                    {"branch": "release/loopx-minimal", "head": SHA_A1, "tip": SHA_A1},
                )
            ),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.WRONG_BRANCH]
        assert "release/loopx-minimal" in result.failures[0].detail
        assert "feature-x" in result.failures[0].detail

    def test_spec_missing_in_head_commit(self) -> None:
        result = check_dd_ready(
            [_dd_repo("/wt/dd-nospec")],
            runner=_runner(("/wt/dd-nospec", {"spec": 1})),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.SPEC_MISSING]

    def test_handoff_failure_short_circuits_spec_check(self) -> None:
        result = check_dd_ready(
            [_dd_repo("/wt/dd-dirty")],
            runner=_runner(("/wt/dd-dirty", {"status": " M x.py\n", "spec": 1})),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.DIRTY_WORKTREE]

    def test_branch_missing_on_remote_fails_before_spec(self) -> None:
        result = check_dd_ready(
            [_dd_repo("/wt/dd-nobranch")],
            runner=_runner(("/wt/dd-nobranch", {"ls_remote": None, "spec": 1})),
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.BRANCH_MISSING_ON_REMOTE]

    def test_two_repos_only_one_missing_spec(self) -> None:
        result = check_dd_ready(
            [_dd_repo("/wt/dd-a"), _dd_repo("/wt/dd-b")],
            runner=_runner(("/wt/dd-a", {}), ("/wt/dd-b", {"spec": 128})),
        )
        assert result.ok is False
        assert len(result.failures) == 1
        assert result.failures[0].repo == "repo-/wt/dd-b"
        assert result.failures[0].code == FailureCode.SPEC_MISSING


# ---------------------------------------------------------------------------
# file_exists_at
# ---------------------------------------------------------------------------


class TestFileExistsAt:
    def test_nonzero_cat_file_exit_is_false_not_an_exception(self) -> None:
        runner = _runner(("/wt/x", {"spec": 128}))
        assert file_exists_at("/wt/x", SHA_A1, "docs/specs/nope.md", runner=runner) is False

    def test_zero_cat_file_exit_is_true(self) -> None:
        runner = _runner(("/wt/x", {"spec": 0}))
        assert file_exists_at("/wt/x", SHA_A1, "docs/specs/101-foo.md", runner=runner) is True


# ---------------------------------------------------------------------------
# remote_tip: the remote is asked directly, absence is a verdict not an error
# ---------------------------------------------------------------------------


class TestRemoteTip:
    def test_branch_absent_on_remote_is_none_not_an_exception(self) -> None:
        # Exit 0 with empty stdout is how ls-remote answers "no such ref";
        # the absence must not depend on stderr wording or the exit code.
        runner = _runner(("/wt/tip", {"ls_remote": None}))
        assert remote_tip("/wt/tip", "origin", "feature-x", runner=runner) is None

    def test_ls_remote_stdout_sha_is_returned(self) -> None:
        runner = _runner(("/wt/tip", {"ls_remote": SHA_NEW}))
        assert remote_tip("/wt/tip", "origin", "feature-x", runner=runner) == SHA_NEW

    def test_fetch_false_resolves_local_tracking_ref(self) -> None:
        runner = _runner(("/wt/tip", {"tip": SHA_B1}))
        assert remote_tip("/wt/tip", "origin", "feature-x", runner=runner, fetch=False) == SHA_B1

    def test_fetch_false_missing_tracking_ref_is_none(self) -> None:
        runner = _runner(("/wt/tip", {"tip": None}))
        assert remote_tip("/wt/tip", "origin", "feature-x", runner=runner, fetch=False) is None

    def test_ls_remote_failure_raises_git_error(self) -> None:
        from fleet_graph.minimal.gitgate import GitError

        class FailingRunner:
            def run(self, args: list[str], *, cwd: str) -> CompletedResult:
                if _matches(args, _FRAGMENTS["ls_remote"]):
                    return CompletedResult(128, "", "remote origin does not exist")
                raise AssertionError(f"unexpected argv: {args!r}")

        with pytest.raises(GitError):
            remote_tip("/wt/tip", "origin", "feature-x", runner=FailingRunner())


# ---------------------------------------------------------------------------
# runner contract: argv lists + explicit cwd, never shell strings
# ---------------------------------------------------------------------------


class TestRunnerContract:
    def _exercised_runner(self) -> FakeGitRunner:
        runner = _runner(("/wt/contract", {}))
        check_dd_ready([_dd_repo("/wt/contract")], runner=runner)
        return runner

    def test_every_call_passes_an_argv_list(self) -> None:
        runner = self._exercised_runner()
        assert runner.calls, "the gate made no git calls at all"
        for args, _cwd in runner.calls:
            assert isinstance(args, list)
            assert all(isinstance(part, str) for part in args)
            assert args[0] == "git", "the argv starts with the literal git command"
            for sep in (";", "|", "&&", "$(", "`"):
                assert not any(sep in part for part in args), f"shell-ish {sep!r} in {args!r}"

    def test_every_call_carries_the_config_guards(self) -> None:
        """Agent-written worktrees can carry a hostile repo-local config; every
        argv must include the three guards that neutralize it (mirrored from
        tests/test_dd_git.py's exploit regression)."""
        runner = self._exercised_runner()
        assert runner.calls
        for args, _cwd in runner.calls:
            assert args[1:2] == ["-c"] and args[2] == "core.fsmonitor=false"
            assert "core.hooksPath=/dev/null" in args
            assert "protocol.ext.allow=never" in args

    def test_every_call_carries_the_worktree_cwd(self) -> None:
        runner = self._exercised_runner()
        for _args, cwd in runner.calls:
            assert cwd == "/wt/contract"

    def test_worktree_pinned_via_dash_c_argv(self) -> None:
        """The fake sees the argv the real runner would exec verbatim; the cwd
        is pinned with a list-form ``-C <worktree>``, never a shell string."""
        runner = self._exercised_runner()
        for args, _cwd in runner.calls:
            at_c = args.index("-C")
            assert args[at_c + 1] == "/wt/contract"

    def test_remote_is_queried_before_tip_resolution(self) -> None:
        """The gate asks the remote directly (ls-remote) rather than trusting
        stale local refs; the query precedes any local ref resolution."""
        runner = self._exercised_runner()
        remote_at = next(
            i
            for i, (args, _cwd) in enumerate(runner.calls)
            if _matches(args, _FRAGMENTS["ls_remote"])
        )
        local_tips = [
            i for i, (args, _cwd) in enumerate(runner.calls) if _matches(args, _FRAGMENTS["tip"])
        ]
        assert not local_tips or remote_at < min(local_tips)


# ---------------------------------------------------------------------------
# failure codes are exported constants
# ---------------------------------------------------------------------------


class TestFailureCodes:
    def test_codes_are_stable_strings(self) -> None:
        assert FailureCode.NOT_PUSHED == "not_pushed"
        assert FailureCode.HEAD_BEHIND_REMOTE == "head_behind_remote"
        assert FailureCode.DIRTY_WORKTREE == "dirty_worktree"
        assert FailureCode.BRANCH_MISSING_ON_REMOTE == "branch_missing_on_remote"
        assert FailureCode.DETACHED_HEAD == "detached_head"
        assert FailureCode.WRONG_BRANCH == "wrong_branch"
        assert FailureCode.SPEC_MISSING == "spec_missing"
