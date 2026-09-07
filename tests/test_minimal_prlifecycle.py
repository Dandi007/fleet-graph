"""Tests for the DD PR / worktree lifecycle (GO-35/36 write side).

Everything runs against a scripted fake runner in the style of
tests/test_minimal_gitgate.py: no real git, no gh binary, no network. The
assertions are token-by-token on the argv (never a shell string), and the
scripts double as the contract that step failures are data (``StepResult``),
not exceptions — only ``open_pr`` raises (``PrError``), and only violations
of the token / number whitelist raise ``ValueError`` before any argv exists.
"""

from __future__ import annotations

import pytest

from fleet_graph.minimal.gitgate import CompletedResult
from fleet_graph.minimal.prlifecycle import (
    CleanupRepo,
    PrError,
    PrRef,
    cleanup_dd,
    close_pr,
    delete_remote_branch,
    open_pr,
    pr_mergeable,
    remove_worktree,
)

WORKTREE = "/data/repos/worktrees/dd-12"
REPO = "/data/repos/fleet-graph"
REMOTE = "origin"
BRANCH = "dd/loopx-minimal/dd-12"
BASE = "release/loopx-minimal"
PR_NUMBER = 272
PR_URL = f"https://github.com/Dandi007/fleet-graph/pull/{PR_NUMBER}"
SHA = "a" * 40

GUARDS = [
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
]


def _git(path: str, *args: str) -> list[str]:
    """The exact guarded argv the module must build (mirrored for assertions)."""

    return ["git", *GUARDS, "-C", path, *args]


def _ok(stdout: str = "") -> CompletedResult:
    return CompletedResult(0, stdout, "")


def _fail(stderr: str = "", stdout: str = "", exit_code: int = 1) -> CompletedResult:
    return CompletedResult(exit_code, stdout, stderr)


class FakeRunner:
    """Answers each call from one-shot scripts keyed by argv fragments.

    A script entry is ``(fragment, result)``: the first unconsumed entry
    whose fragment tokens all appear in the argv answers the call and is
    then consumed, so ordering within one kind is preserved. An argv no
    script covers fails loudly — tests cannot pass on accidental gaps.
    """

    def __init__(self, *scripted: tuple[list[str], CompletedResult]) -> None:
        self._scripted = list(scripted)
        self.calls: list[tuple[list[str], str]] = []

    def run(self, args: list[str], *, cwd: str) -> CompletedResult:
        self.calls.append((list(args), cwd))
        for index, (fragment, result) in enumerate(self._scripted):
            if all(part in args for part in fragment):
                self._scripted.pop(index)
                return result
        raise AssertionError(f"unscripted argv: {args!r}")


def _record() -> CleanupRepo:
    return CleanupRepo(
        repo_path=REPO, worktree=WORKTREE, remote=REMOTE, branch=BRANCH, pr_number=PR_NUMBER
    )


# ---------------------------------------------------------------------------
# open_pr
# ---------------------------------------------------------------------------


class TestOpenPr:
    def test_creates_the_pr_and_parses_number_and_url(self) -> None:
        runner = FakeRunner((["pr", "create"], _ok(PR_URL + "\n")))
        ref = open_pr(
            WORKTREE,
            head_branch=BRANCH,
            base_branch=BASE,
            title="prlifecycle: GO-35",
            body_file="/tmp/body.md",
            runner=runner,
        )
        assert ref == PrRef(number=PR_NUMBER, url=PR_URL)
        assert runner.calls == [
            (
                [
                    "gh",
                    "pr",
                    "create",
                    "--head",
                    BRANCH,
                    "--base",
                    BASE,
                    "--title",
                    "prlifecycle: GO-35",
                    "--body-file",
                    "/tmp/body.md",
                ],
                WORKTREE,
            )
        ]

    def test_already_exists_falls_back_to_view_and_reuses(self) -> None:
        existing = "https://github.com/Dandi007/fleet-graph/pull/271"
        view_stdout = f'{{"number": 271, "url": "{existing}"}}'
        runner = FakeRunner(
            (
                ["pr", "create"],
                _fail(f'gh: a pull request for branch "{BRANCH}" already exists'),
            ),
            (["pr", "view", "--json", "number,url"], _ok(view_stdout)),
        )
        ref = open_pr(
            WORKTREE,
            head_branch=BRANCH,
            base_branch=BASE,
            title="t",
            body_file="b",
            runner=runner,
        )
        assert ref == PrRef(number=271, url=existing)
        assert runner.calls[1] == (["gh", "pr", "view", BRANCH, "--json", "number,url"], WORKTREE)

    def test_create_failure_without_fallback_raises_pr_error(self) -> None:
        runner = FakeRunner((["pr", "create"], _fail("gh: graphql error")))
        with pytest.raises(PrError) as excinfo:
            open_pr(
                WORKTREE,
                head_branch=BRANCH,
                base_branch=BASE,
                title="t",
                body_file="b",
                runner=runner,
            )
        assert excinfo.value.argv[:4] == ["gh", "pr", "create", "--head"]
        assert "graphql error" in excinfo.value.stderr

    def test_fallback_view_failure_raises_pr_error_with_the_view_argv(self) -> None:
        runner = FakeRunner(
            (["pr", "create"], _fail("a pull request for branch already exists")),
            (["pr", "view", "--json", "number,url"], _fail("gh: no pull request found")),
        )
        with pytest.raises(PrError) as excinfo:
            open_pr(
                WORKTREE,
                head_branch=BRANCH,
                base_branch=BASE,
                title="t",
                body_file="b",
                runner=runner,
            )
        assert excinfo.value.argv == ["gh", "pr", "view", BRANCH, "--json", "number,url"]


# ---------------------------------------------------------------------------
# pr_mergeable
# ---------------------------------------------------------------------------


class TestPrMergeable:
    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [
            ('{"mergeable": "MERGEABLE"}', "MERGEABLE"),
            ('{"mergeable": "CONFLICTING"}', "CONFLICTING"),
            ('{"mergeable": "UNKNOWN"}', "UNKNOWN"),
            ('{"mergeable": null}', "UNKNOWN"),
            ("not json at all", "UNKNOWN"),
            ("", "UNKNOWN"),
        ],
    )
    def test_maps_the_platform_verdict(self, stdout: str, expected: str) -> None:
        runner = FakeRunner((["pr", "view", "--json", "mergeable"], _ok(stdout)))
        assert pr_mergeable(WORKTREE, PR_NUMBER, runner=runner) == expected

    def test_gh_failure_is_unknown_not_an_exception(self) -> None:
        runner = FakeRunner(
            (["pr", "view", "--json", "mergeable"], _fail("gh: connection refused"))
        )
        assert pr_mergeable(WORKTREE, PR_NUMBER, runner=runner) == "UNKNOWN"

    def test_argv_is_token_exact(self) -> None:
        runner = FakeRunner(
            (["pr", "view", "--json", "mergeable"], _ok('{"mergeable": "MERGEABLE"}'))
        )
        pr_mergeable(WORKTREE, PR_NUMBER, runner=runner)
        assert runner.calls == [
            (
                ["gh", "pr", "view", str(PR_NUMBER), "--json", "mergeable"],
                WORKTREE,
            )
        ]


# ---------------------------------------------------------------------------
# close_pr
# ---------------------------------------------------------------------------


class TestClosePr:
    def test_closes_with_exact_argv(self) -> None:
        runner = FakeRunner((["pr", "close"], _ok()))
        step = close_pr(WORKTREE, PR_NUMBER, runner=runner)
        assert step.step == "close_pr"
        assert step.ok is True
        assert step.detail is None
        assert runner.calls == [(["gh", "pr", "close", "272"], WORKTREE)]

    def test_comment_is_appended_as_two_tokens(self) -> None:
        runner = FakeRunner((["pr", "close"], _ok()))
        close_pr(WORKTREE, PR_NUMBER, runner=runner, comment="superseded by dd-13")
        assert runner.calls[0][0] == [
            "gh",
            "pr",
            "close",
            "272",
            "--comment",
            "superseded by dd-13",
        ]

    def test_already_closed_is_idempotent_success(self) -> None:
        runner = FakeRunner(
            (["pr", "close"], _fail("Pull request #272 is already closed")),
            (["pr", "view", "--json", "state"], _ok('{"state": "CLOSED"}')),
        )
        step = close_pr(WORKTREE, PR_NUMBER, runner=runner)
        assert step.ok is True
        assert "already CLOSED" in (step.detail or "")

    def test_already_merged_is_idempotent_success(self) -> None:
        runner = FakeRunner(
            (["pr", "close"], _fail("cannot close a merged pull request")),
            (["pr", "view", "--json", "state"], _ok('{"state": "MERGED"}')),
        )
        step = close_pr(WORKTREE, PR_NUMBER, runner=runner)
        assert step.ok is True

    def test_real_failure_is_data(self) -> None:
        runner = FakeRunner(
            (["pr", "close"], _fail("gh: forbidden")),
            (["pr", "view", "--json", "state"], _ok('{"state": "OPEN"}')),
        )
        step = close_pr(WORKTREE, PR_NUMBER, runner=runner)
        assert step.ok is False
        assert "forbidden" in (step.detail or "")
        assert step.argv == ["gh", "pr", "close", "272"]


# ---------------------------------------------------------------------------
# remove_worktree
# ---------------------------------------------------------------------------


class TestRemoveWorktree:
    def test_removes_then_prunes_with_exact_argv(self) -> None:
        runner = FakeRunner(
            (["worktree", "remove"], _ok()),
            (["worktree", "prune"], _ok()),
        )
        step = remove_worktree(REPO, WORKTREE, runner=runner)
        assert step.step == "remove_worktree"
        assert step.ok is True
        assert step.detail is None
        assert runner.calls == [
            (_git(REPO, "worktree", "remove", WORKTREE), REPO),
            (_git(REPO, "worktree", "prune"), REPO),
        ]

    def test_force_inserts_the_flag_before_the_path(self) -> None:
        runner = FakeRunner(
            (["worktree", "remove"], _ok()),
            (["worktree", "prune"], _ok()),
        )
        remove_worktree(REPO, WORKTREE, runner=runner, force=True)
        assert runner.calls[0][0] == _git(REPO, "worktree", "remove", "--force", WORKTREE)

    def test_absent_worktree_is_idempotent_success_and_still_prunes(self) -> None:
        runner = FakeRunner(
            (["worktree", "remove"], _fail("fatal: not a working tree", exit_code=128)),
            (["worktree", "list"], _ok(f"worktree {REPO}\n")),
            (["worktree", "prune"], _ok()),
        )
        step = remove_worktree(REPO, WORKTREE, runner=runner)
        assert step.ok is True
        assert "already removed" in (step.detail or "")
        assert runner.calls[1] == (
            _git(REPO, "worktree", "list", "--porcelain"),
            REPO,
        )
        assert runner.calls[2] == (_git(REPO, "worktree", "prune"), REPO)

    def test_registered_worktree_failure_is_data_and_skips_prune(self) -> None:
        runner = FakeRunner(
            (["worktree", "remove"], _fail("fatal: working tree contains unstaged changes")),
            (["worktree", "list"], _ok(f"worktree {REPO}\nworktree {WORKTREE}\n")),
        )
        step = remove_worktree(REPO, WORKTREE, runner=runner)
        assert step.ok is False
        assert "unstaged changes" in (step.detail or "")
        assert len(runner.calls) == 2  # no prune after a genuine removal failure

    def test_prune_failure_is_reported_with_the_prune_argv(self) -> None:
        runner = FakeRunner(
            (["worktree", "remove"], _ok()),
            (["worktree", "prune"], _fail("fatal: unable to prune", exit_code=128)),
        )
        step = remove_worktree(REPO, WORKTREE, runner=runner)
        assert step.ok is False
        assert step.argv == _git(REPO, "worktree", "prune")
        assert "unable to prune" in (step.detail or "")


# ---------------------------------------------------------------------------
# delete_remote_branch
# ---------------------------------------------------------------------------


class TestDeleteRemoteBranch:
    def test_deletes_with_exact_argv(self) -> None:
        runner = FakeRunner((["push"], _ok()))
        step = delete_remote_branch(WORKTREE, REMOTE, BRANCH, runner=runner)
        assert step.step == "delete_remote_branch"
        assert step.ok is True
        assert step.detail is None
        assert runner.calls == [(_git(WORKTREE, "push", REMOTE, "--delete", BRANCH), WORKTREE)]

    def test_branch_already_absent_is_idempotent_success(self) -> None:
        runner = FakeRunner(
            (["push"], _fail("error: unable to delete: remote ref does not exist")),
            (["ls-remote"], _ok("")),
        )
        step = delete_remote_branch(WORKTREE, REMOTE, BRANCH, runner=runner)
        assert step.ok is True
        assert "already absent" in (step.detail or "")
        assert runner.calls[1] == (
            _git(WORKTREE, "ls-remote", REMOTE, f"refs/heads/{BRANCH}"),
            WORKTREE,
        )

    def test_branch_still_present_is_failure_data(self) -> None:
        runner = FakeRunner(
            (["push"], _fail("error: failed to push some refs")),
            (["ls-remote"], _ok(f"{SHA}\trefs/heads/{BRANCH}\n")),
        )
        step = delete_remote_branch(WORKTREE, REMOTE, BRANCH, runner=runner)
        assert step.ok is False
        assert "failed to push" in (step.detail or "")
        assert step.argv == _git(WORKTREE, "push", REMOTE, "--delete", BRANCH)


# ---------------------------------------------------------------------------
# cleanup_dd: GO-35's sequence, failures collected, never early-exit
# ---------------------------------------------------------------------------


class TestCleanupDd:
    def _happy_scripts(self) -> FakeRunner:
        return FakeRunner(
            (["pr", "close"], _ok()),
            (["worktree", "remove"], _ok()),
            (["worktree", "prune"], _ok()),
            (["push"], _ok()),
        )

    def test_merged_happy_path_runs_three_steps_in_go35_order(self) -> None:
        runner = self._happy_scripts()
        result = cleanup_dd(outcome="merged", repos=[_record()], runner=runner)
        assert result.ok is True
        assert [step.step for step in result.steps] == [
            "close_pr",
            "remove_worktree",
            "delete_remote_branch",
        ]
        # The push runs from the main repo checkout: the worktree directory
        # is gone after a successful removal, and git cannot chdir into it.
        assert runner.calls[3] == (_git(REPO, "push", REMOTE, "--delete", BRANCH), REPO)

    def test_failed_outcome_comment_flows_into_the_close(self) -> None:
        runner = self._happy_scripts()
        result = cleanup_dd(
            outcome="failed", repos=[_record()], runner=runner, comment="dd failed at acceptance"
        )
        assert result.ok is True
        assert runner.calls[0][0][-2:] == ["--comment", "dd failed at acceptance"]

    def test_mid_step_failure_does_not_stop_the_sequence(self) -> None:
        runner = FakeRunner(
            (["pr", "close"], _ok()),
            (["worktree", "remove"], _fail("fatal: contains unstaged changes")),
            (["worktree", "list"], _ok(f"worktree {REPO}\nworktree {WORKTREE}\n")),
            (["push"], _ok()),
        )
        result = cleanup_dd(outcome="failed", repos=[_record()], runner=runner)
        assert result.ok is False
        assert [step.step for step in result.steps] == [
            "close_pr",
            "remove_worktree",
            "delete_remote_branch",
        ]
        assert [step.ok for step in result.steps] == [True, False, True]
        # the branch deletion after the failed removal still ran
        assert any("push" in argv for argv, _ in runner.calls)

    def test_plain_tuple_records_are_accepted(self) -> None:
        runner = self._happy_scripts()
        result = cleanup_dd(
            outcome="merged",
            repos=[(REPO, WORKTREE, REMOTE, BRANCH, PR_NUMBER)],
            runner=runner,
        )
        assert result.ok is True
        assert len(result.steps) == 3

    def test_two_repos_six_steps_in_per_repo_order(self) -> None:
        other = CleanupRepo(
            repo_path="/data/repos/other",
            worktree="/data/repos/worktrees/dd-12-other",
            remote=REMOTE,
            branch="dd/loopx-minimal/other",
            pr_number=273,
        )
        runner = FakeRunner(
            (["pr", "close"], _ok()),
            (["pr", "close"], _ok()),
            (["worktree", "remove"], _ok()),
            (["worktree", "remove"], _ok()),
            (["worktree", "prune"], _ok()),
            (["worktree", "prune"], _ok()),
            (["push"], _ok()),
            (["push"], _ok()),
        )
        result = cleanup_dd(outcome="merged", repos=[_record(), other], runner=runner)
        assert result.ok is True
        assert len(result.steps) == 6
        # per repo: close, remove(+prune), push — the first repo finishes
        # before the second starts (GO-35's order is per repo, not per step)
        first_repo_argv = [argv for argv, _ in runner.calls[:4]]
        assert first_repo_argv[0][:4] == ["gh", "pr", "close", "272"]
        assert first_repo_argv[3][:9] == ["git", *GUARDS, "-C", REPO]

    def test_invalid_outcome_raises_before_any_command(self) -> None:
        runner = FakeRunner()
        with pytest.raises(ValueError, match="outcome"):
            cleanup_dd(outcome="merged-ish", repos=[_record()], runner=runner)
        assert runner.calls == []

    def test_invalid_token_in_record_raises_before_any_command(self) -> None:
        runner = FakeRunner()
        bad = CleanupRepo(REPO, WORKTREE, REMOTE, "-evil", PR_NUMBER)
        with pytest.raises(ValueError, match="branch"):
            cleanup_dd(outcome="failed", repos=[bad], runner=runner)
        assert runner.calls == []


# ---------------------------------------------------------------------------
# the whitelist: ValueError before any argv exists
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize("number", [0, -1, "272", 2.5, True, None])
    def test_bad_pr_numbers_raise_before_any_argv(self, number: object) -> None:
        runner = FakeRunner()
        with pytest.raises(ValueError, match="PR number"):
            close_pr(WORKTREE, number, runner=runner)  # type: ignore[arg-type]
        assert runner.calls == []

    @pytest.mark.parametrize("token", ["-evil", "has space", "semi;colon", "", "dd|x", "a(b)"])
    def test_bad_branch_tokens_raise_before_any_argv(self, token: str) -> None:
        runner = FakeRunner()
        with pytest.raises(ValueError, match="branch"):
            delete_remote_branch(WORKTREE, REMOTE, token, runner=runner)
        assert runner.calls == []

    def test_bad_remote_token_raises_before_any_argv(self) -> None:
        runner = FakeRunner()
        with pytest.raises(ValueError, match="remote"):
            delete_remote_branch(WORKTREE, "origin --force", BRANCH, runner=runner)
        assert runner.calls == []

    def test_open_pr_validates_head_and_base(self) -> None:
        runner = FakeRunner()
        with pytest.raises(ValueError, match="head_branch"):
            open_pr(
                WORKTREE,
                head_branch="-x",
                base_branch=BASE,
                title="t",
                body_file="b",
                runner=runner,
            )
        assert runner.calls == []

    def test_inner_dashes_are_legal(self) -> None:
        # dd/loopx-minimal/dd-12 carries inner dashes; only a LEADING dash
        # would impersonate a flag.
        runner = FakeRunner((["push"], _ok()))
        assert delete_remote_branch(WORKTREE, REMOTE, BRANCH, runner=runner).ok is True


# ---------------------------------------------------------------------------
# runner contract: guards precede -C on every git argv
# ---------------------------------------------------------------------------


class TestGuardOrdering:
    def _exercised(self) -> FakeRunner:
        runner = FakeRunner(
            (["pr", "close"], _ok()),
            (["worktree", "remove"], _ok()),
            (["worktree", "prune"], _ok()),
            (["push"], _ok()),
        )
        close_pr(WORKTREE, PR_NUMBER, runner=runner)
        remove_worktree(REPO, WORKTREE, runner=runner)
        delete_remote_branch(REPO, REMOTE, BRANCH, runner=runner)
        return runner

    def test_guards_directly_follow_git_and_precede_dash_c(self) -> None:
        runner = self._exercised()
        git_calls = [argv for argv, _ in runner.calls if argv[0] == "git"]
        assert len(git_calls) == 3
        for argv in git_calls:
            assert argv[1:8] == [*GUARDS, "-C"], argv
            assert argv.index("-C") == 7

    def test_gh_calls_carry_no_git_flags(self) -> None:
        runner = self._exercised()
        gh_calls = [argv for argv, _ in runner.calls if argv[0] == "gh"]
        assert gh_calls
        for argv in gh_calls:
            assert "-C" not in argv
            for sep in (";", "|", "&&", "$(", "`"):
                assert not any(sep in part for part in argv), f"shell-ish {sep!r} in {argv!r}"
