"""Tests for the merge gate (GO-36 routing + protocol §0.10 Stop checks).

Everything runs against scripted fake runners in the style of
tests/test_minimal_gitgate.py and tests/test_minimal_prlifecycle.py: no real
git, no gh binary, no network. The scripts double as the contract: decide
never guesses unknown as mergeable, platform_merge never raises (failure is
data), verify_merge_output returns error strings (empty = pass), and only
violations of the token / sha whitelist raise ValueError before any argv
exists. The argv assertions are token-by-token on ``list[str]`` — never a
shell string, never ``shell=True``.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from fleet_graph.minimal import mergegate as mergegate_module
from fleet_graph.minimal.gitgate import CompletedResult
from fleet_graph.minimal.mergegate import (
    MergeDecision,
    MergePlan,
    MergeReason,
    MergeReasonCode,
    MergeRepo,
    MergeRoute,
    decide,
    final_merge_plan,
    platform_merge,
    verify_merge_output,
)
from fleet_graph.minimal.prlifecycle import PrRef

WORKTREE = "/data/repos/worktrees/dd-19"
REMOTE = "origin"
SOURCE_BRANCH = "dd/loopx-minimal/dd-19-mergegate-py-approve-mergeab"
TARGET_BRANCH = "release/loopx-minimal"
PR = PrRef(number=281, url="https://github.com/Dandi007/fleet-graph/pull/281")
SLUG = "Dandi007/fleet-graph"

SOURCE_HEAD = "5" * 40
TARGET_HEAD = "7" * 40
MERGED_COMMIT = "9" * 40
NEW_HEAD = "3" * 40
OTHER_TIP = "8" * 40

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


def _fail(stderr: str = "", exit_code: int = 1) -> CompletedResult:
    return CompletedResult(exit_code, "", stderr)


def _repo(**overrides: object) -> MergeRepo:
    fields: dict[str, object] = {
        "worktree": WORKTREE,
        "remote": REMOTE,
        "source_branch": SOURCE_BRANCH,
        "target_branch": TARGET_BRANCH,
        "source_head": SOURCE_HEAD,
        "target_head": TARGET_HEAD,
    }
    fields.update(overrides)
    return MergeRepo(**fields)  # type: ignore[arg-type]


class FakeGhRunner:
    """Answers each gh call from one-shot scripts keyed by argv fragments.

    Same contract as the prlifecycle fake: the first unconsumed entry whose
    fragment tokens all appear in the argv answers the call and is then
    consumed; an argv no script covers fails loudly.
    """

    def __init__(self, *scripted: tuple[list[str], CompletedResult]) -> None:
        self._scripted = list(scripted)
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> CompletedResult:
        self.calls.append(list(args))
        for index, (fragment, result) in enumerate(self._scripted):
            if all(part in args for part in fragment):
                self._scripted.pop(index)
                return result
        raise AssertionError(f"unscripted gh argv: {args!r}")


class FakeVerifyRunner:
    """Answers the git queries verify_merge_output makes, from a script.

    ``tips`` maps ``<remote>/<branch>`` to the sha ``rev-parse --verify``
    answers (missing = the ref does not resolve, exit 128); ``contains`` is
    the set of ``(ancestor, commit)`` pairs merge-base answers yes to;
    ``fetch_ok`` False scripts a failing fetch. Unrecognized argv fails
    loudly so tests cannot pass on accidental gaps.
    """

    def __init__(
        self,
        tips: dict[str, str],
        contains: set[tuple[str, str]] = frozenset(),
        fetch_ok: bool = True,
    ) -> None:
        self.tips = tips
        self.contains = contains
        self.fetch_ok = fetch_ok
        self.calls: list[tuple[list[str], str]] = []

    def run(self, args: list[str], *, cwd: str) -> CompletedResult:
        self.calls.append((list(args), cwd))
        if "fetch" in args:
            return _ok() if self.fetch_ok else _fail("fatal: could not read from remote")
        if "rev-parse" in args:
            ref = args[-1]
            sha = self.tips.get(ref)
            if sha is None:
                return CompletedResult(128, "", f"{ref}: not found")
            return _ok(sha + "\n")
        if "merge-base" in args:
            at = args.index("--is-ancestor")
            pair = (args[at + 1], args[at + 2])
            return _ok() if pair in self.contains else CompletedResult(1, "", "")
        raise AssertionError(f"unrecognized git argv: {args!r}")


def _merged_obj() -> dict:
    return {"schema": "merge/1", "stop": "merged", "merged_commit": MERGED_COMMIT}


def _rebased_obj() -> dict:
    return {"schema": "merge/1", "stop": "rebased", "new_head": NEW_HEAD}


def _failed_obj() -> dict:
    return {"schema": "merge/1", "stop": "failed", "detail": "conflict too tangled"}


# ---------------------------------------------------------------------------
# decide: the three-state routing (GO-36)
# ---------------------------------------------------------------------------


class TestDecide:
    def test_true_routes_to_platform_merge(self) -> None:
        decision = decide(PR, mergeable_fn=lambda pr: True)
        assert decision.route == MergeRoute.PLATFORM_MERGE
        assert decision.reason.code == MergeReasonCode.PLATFORM_MERGEABLE

    def test_mergeable_string_routes_to_platform_merge(self) -> None:
        decision = decide(PR, mergeable_fn=lambda pr: "MERGEABLE")
        assert decision.route == MergeRoute.PLATFORM_MERGE

    def test_mergeable_fn_receives_the_pr(self) -> None:
        seen: list[PrRef] = []
        decide(PR, mergeable_fn=lambda pr: seen.append(pr) or True)
        assert seen == [PR]

    def test_false_routes_to_merge_agent(self) -> None:
        decision = decide(PR, mergeable_fn=lambda pr: False)
        assert decision.route == MergeRoute.MERGE_AGENT
        assert decision.reason.code == MergeReasonCode.CONFLICTING

    def test_conflicting_string_routes_to_merge_agent(self) -> None:
        decision = decide(PR, mergeable_fn=lambda pr: "CONFLICTING")
        assert decision.route == MergeRoute.MERGE_AGENT
        assert decision.reason.code == MergeReasonCode.CONFLICTING

    def test_none_routes_to_merge_agent_and_unknown_is_not_mergeable(self) -> None:
        decision = decide(PR, mergeable_fn=lambda pr: None)
        assert decision.route == MergeRoute.MERGE_AGENT
        assert decision.reason.code == MergeReasonCode.UNKNOWN
        assert "unknown" in decision.reason.detail
        assert "never treated as mergeable" in decision.reason.detail

    def test_unknown_string_routes_to_merge_agent(self) -> None:
        decision = decide(PR, mergeable_fn=lambda pr: "UNKNOWN")
        assert decision.route == MergeRoute.MERGE_AGENT
        assert decision.reason.code == MergeReasonCode.UNKNOWN

    def test_out_of_domain_verdict_is_not_guessed_as_mergeable(self) -> None:
        decision = decide(PR, mergeable_fn=lambda pr: "MAYBE")
        assert decision.route == MergeRoute.MERGE_AGENT
        assert decision.reason.code == MergeReasonCode.UNKNOWN

    def test_decision_is_frozen(self) -> None:
        decision = decide(PR, mergeable_fn=lambda pr: True)
        with pytest.raises(FrozenInstanceError):
            decision.route = MergeRoute.MERGE_AGENT  # type: ignore[misc]

    def test_decision_shape(self) -> None:
        decision = decide(PR, mergeable_fn=lambda pr: True)
        assert decision == MergeDecision(
            route=MergeRoute.PLATFORM_MERGE,
            reason=MergeReason(
                code=MergeReasonCode.PLATFORM_MERGEABLE, detail=decision.reason.detail
            ),
        )


# ---------------------------------------------------------------------------
# platform_merge: failure is data, never an exception
# ---------------------------------------------------------------------------


class TestPlatformMerge:
    def test_success_returns_merged_with_commit(self) -> None:
        runner = FakeGhRunner(
            (["pr", "merge"], _ok("")),
            (["pr", "view"], _ok(json.dumps({"mergeCommit": {"oid": MERGED_COMMIT}}))),
        )
        stop, payload = platform_merge(PR, gh_runner=runner)
        assert stop == "merged"
        assert payload["merged_commit"] == MERGED_COMMIT
        assert payload["pr"] == 281
        assert payload["url"] == PR.url
        assert runner.calls == [
            ["gh", "pr", "merge", "281", "--repo", SLUG, "--merge"],
            ["gh", "pr", "view", "281", "--repo", SLUG, "--json", "mergeCommit"],
        ]

    def test_failure_returns_failed_and_does_not_raise(self) -> None:
        runner = FakeGhRunner(
            (["pr", "merge"], _fail("gh: Pull Request is not mergeable")),
            (["pr", "view"], _ok(json.dumps({"mergeCommit": None}))),
        )
        stop, payload = platform_merge(PR, gh_runner=runner)
        assert stop == "failed"
        assert "not mergeable" in payload["detail"]

    def test_zero_exit_with_unreadable_commit_is_failed_not_raised(self) -> None:
        runner = FakeGhRunner(
            (["pr", "merge"], _ok("")),
            (["pr", "view"], _ok("not json at all")),
        )
        stop, payload = platform_merge(PR, gh_runner=runner)
        assert stop == "failed"
        assert "could not be read back" in payload["detail"]

    def test_already_merged_pr_replay_still_verifies_merged(self) -> None:
        runner = FakeGhRunner(
            (["pr", "merge"], _fail("gh: Pull request was already merged")),
            (["pr", "view"], _ok(json.dumps({"mergeCommit": {"oid": MERGED_COMMIT}}))),
        )
        stop, payload = platform_merge(PR, gh_runner=runner)
        assert stop == "merged"
        assert payload["merged_commit"] == MERGED_COMMIT

    def test_non_sha_merge_commit_is_failed_not_raised(self) -> None:
        runner = FakeGhRunner(
            (["pr", "merge"], _ok("")),
            (["pr", "view"], _ok(json.dumps({"mergeCommit": {"oid": "abc"}}))),
        )
        stop, _payload = platform_merge(PR, gh_runner=runner)
        assert stop == "failed"

    @pytest.mark.parametrize(
        "pr",
        [
            PrRef(0, "https://github.com/Dandi007/fleet-graph/pull/281"),
            PrRef(-1, "https://github.com/Dandi007/fleet-graph/pull/281"),
            PrRef(True, "https://github.com/Dandi007/fleet-graph/pull/281"),  # type: ignore[arg-type]
            PrRef(281, "https://example.com/pr/281"),
            PrRef(281, "not a url"),
            PrRef(281, "https://github.com/Dandi007/fleet-graph/pull/272"),
        ],
    )
    def test_invalid_pr_ref_raises_value_error_before_any_argv(self, pr: PrRef) -> None:
        runner = FakeGhRunner()
        with pytest.raises(ValueError):
            platform_merge(pr, gh_runner=runner)
        assert runner.calls == []


# ---------------------------------------------------------------------------
# verify_merge_output: the §0.10 table, one pass + one fail per criterion
# ---------------------------------------------------------------------------


class TestVerifyMerged:
    def test_pass(self) -> None:
        runner = FakeVerifyRunner(
            tips={
                f"{REMOTE}/{SOURCE_BRANCH}": SOURCE_HEAD,
                f"{REMOTE}/{TARGET_BRANCH}": MERGED_COMMIT,
            },
            contains={(SOURCE_HEAD, MERGED_COMMIT)},
        )
        assert verify_merge_output(_merged_obj(), [_repo()], git_runner=runner) == []

    def test_merged_commit_not_the_target_tip_fails(self) -> None:
        runner = FakeVerifyRunner(
            tips={f"{REMOTE}/{TARGET_BRANCH}": OTHER_TIP},
            contains={(SOURCE_HEAD, MERGED_COMMIT)},
        )
        errors = verify_merge_output(_merged_obj(), [_repo()], git_runner=runner)
        assert len(errors) == 1
        assert "is not the tip" in errors[0]

    def test_merged_commit_without_source_head_fails(self) -> None:
        runner = FakeVerifyRunner(
            tips={f"{REMOTE}/{TARGET_BRANCH}": MERGED_COMMIT},
            contains=set(),
        )
        errors = verify_merge_output(_merged_obj(), [_repo()], git_runner=runner)
        assert len(errors) == 1
        assert "does not contain source_head" in errors[0]


class TestVerifyRebased:
    def test_pass(self) -> None:
        runner = FakeVerifyRunner(
            tips={
                f"{REMOTE}/{SOURCE_BRANCH}": NEW_HEAD,
                f"{REMOTE}/{TARGET_BRANCH}": TARGET_HEAD,
            },
        )
        assert verify_merge_output(_rebased_obj(), [_repo()], git_runner=runner) == []

    def test_target_tip_moved_fails(self) -> None:
        runner = FakeVerifyRunner(
            tips={
                f"{REMOTE}/{SOURCE_BRANCH}": NEW_HEAD,
                f"{REMOTE}/{TARGET_BRANCH}": OTHER_TIP,
            },
        )
        errors = verify_merge_output(_rebased_obj(), [_repo()], git_runner=runner)
        assert len(errors) == 1
        assert "target tip moved during a rebased stop" in errors[0]

    def test_new_head_not_the_source_tip_fails(self) -> None:
        runner = FakeVerifyRunner(
            tips={
                f"{REMOTE}/{SOURCE_BRANCH}": SOURCE_HEAD,
                f"{REMOTE}/{TARGET_BRANCH}": TARGET_HEAD,
            },
        )
        errors = verify_merge_output(_rebased_obj(), [_repo()], git_runner=runner)
        assert len(errors) == 1
        assert "is not the tip" in errors[0]


class TestVerifyFailed:
    def test_pass(self) -> None:
        runner = FakeVerifyRunner(
            tips={
                f"{REMOTE}/{SOURCE_BRANCH}": SOURCE_HEAD,
                f"{REMOTE}/{TARGET_BRANCH}": TARGET_HEAD,
            },
        )
        assert verify_merge_output(_failed_obj(), [_repo()], git_runner=runner) == []

    def test_source_tip_moved_fails(self) -> None:
        runner = FakeVerifyRunner(
            tips={
                f"{REMOTE}/{SOURCE_BRANCH}": NEW_HEAD,
                f"{REMOTE}/{TARGET_BRANCH}": TARGET_HEAD,
            },
        )
        errors = verify_merge_output(_failed_obj(), [_repo()], git_runner=runner)
        assert len(errors) == 1
        assert "source tip moved during a failed stop" in errors[0]

    def test_target_tip_moved_fails(self) -> None:
        runner = FakeVerifyRunner(
            tips={
                f"{REMOTE}/{SOURCE_BRANCH}": SOURCE_HEAD,
                f"{REMOTE}/{TARGET_BRANCH}": OTHER_TIP,
            },
        )
        errors = verify_merge_output(_failed_obj(), [_repo()], git_runner=runner)
        assert len(errors) == 1
        assert "target tip moved during a failed stop" in errors[0]


class TestVerifyOutputShape:
    def test_non_object_is_an_error_not_an_exception(self) -> None:
        errors = verify_merge_output("merged", [_repo()], git_runner=FakeVerifyRunner({}))
        assert errors == ["merge output must be a JSON object"]

    def test_unknown_stop_is_an_error(self) -> None:
        runner = FakeVerifyRunner({})
        errors = verify_merge_output({"stop": "yolo"}, [_repo()], git_runner=runner)
        assert errors == [
            "merge output stop must be one of ('merged', 'rebased', 'failed'), got 'yolo'"
        ]

    def test_missing_merged_commit_is_an_error_without_git_calls(self) -> None:
        runner = FakeVerifyRunner({})
        errors = verify_merge_output({"stop": "merged"}, [_repo()], git_runner=runner)
        assert len(errors) == 1
        assert "merged_commit" in errors[0]
        assert runner.calls == []

    def test_non_sha_new_head_is_an_error(self) -> None:
        errors = verify_merge_output(
            {"stop": "rebased", "new_head": "abc"}, [_repo()], git_runner=FakeVerifyRunner({})
        )
        assert len(errors) == 1
        assert "new_head" in errors[0]


class TestVerifyRepos:
    def test_two_repos_are_checked_independently(self) -> None:
        good = MergeRepo(
            worktree="/wt/good",
            remote=REMOTE,
            source_branch=SOURCE_BRANCH,
            target_branch=TARGET_BRANCH,
            source_head=SOURCE_HEAD,
            target_head=TARGET_HEAD,
            label="repo-good",
        )
        bad = _repo(label="repo-bad", worktree="/wt/bad", target_branch="main")
        runner = FakeVerifyRunner(
            tips={
                f"{REMOTE}/{SOURCE_BRANCH}": NEW_HEAD,  # moved: fails for both
                f"{REMOTE}/{TARGET_BRANCH}": TARGET_HEAD,
                f"{REMOTE}/main": OTHER_TIP,  # moved target: fails for repo-bad
            },
        )
        errors = verify_merge_output(_failed_obj(), [good, bad], git_runner=runner)
        assert len(errors) == 3
        assert all(error.startswith(("repo-good", "repo-bad")) for error in errors)
        assert sum(error.startswith("repo-bad") for error in errors) == 2

    def test_plain_tuples_are_accepted(self) -> None:
        runner = FakeVerifyRunner(
            tips={
                f"{REMOTE}/{SOURCE_BRANCH}": SOURCE_HEAD,
                f"{REMOTE}/{TARGET_BRANCH}": TARGET_HEAD,
            },
        )
        record = (WORKTREE, REMOTE, SOURCE_BRANCH, TARGET_BRANCH, SOURCE_HEAD, TARGET_HEAD)
        assert verify_merge_output(_failed_obj(), [record], git_runner=runner) == []

    def test_failing_fetch_is_an_error_not_an_exception(self) -> None:
        runner = FakeVerifyRunner({}, fetch_ok=False)
        errors = verify_merge_output(_failed_obj(), [_repo()], git_runner=runner)
        assert len(errors) == 1
        assert "fetch of origin failed" in errors[0]

    def test_the_fetch_argv_is_guarded_and_pinned(self) -> None:
        runner = FakeVerifyRunner(
            tips={f"{REMOTE}/{TARGET_BRANCH}": MERGED_COMMIT},
            contains={(SOURCE_HEAD, MERGED_COMMIT)},
        )
        verify_merge_output(_merged_obj(), [_repo()], git_runner=runner)
        fetch_argv = [args for args, _cwd in runner.calls if "fetch" in args]
        assert fetch_argv == [_git(WORKTREE, "fetch", REMOTE)]

    @pytest.mark.parametrize(
        "overrides",
        [
            {"remote": "-evil"},
            {"source_branch": "--upload-pack=x"},
            {"target_branch": "bad branch"},
        ],
    )
    def test_bad_branch_tokens_raise_before_any_argv(self, overrides: dict) -> None:
        runner = FakeVerifyRunner({})
        with pytest.raises(ValueError):
            verify_merge_output(_failed_obj(), [_repo(**overrides)], git_runner=runner)
        assert runner.calls == []

    @pytest.mark.parametrize(
        "overrides", [{"source_head": "abc"}, {"target_head": 40 * "g"}, {"target_head": None}]
    )
    def test_bad_record_heads_raise_before_any_argv(self, overrides: dict) -> None:
        runner = FakeVerifyRunner({})
        with pytest.raises(ValueError):
            verify_merge_output(_failed_obj(), [_repo(**overrides)], git_runner=runner)
        assert runner.calls == []


# ---------------------------------------------------------------------------
# final_merge_plan: release → each repo's own target (GO-31)
# ---------------------------------------------------------------------------

RELEASE = "release/loopx-minimal"

ENROLL = {
    "schema": "goal.enroll/2",
    "goal_id": "g-7f3a2c",
    "work_folder": None,
    "title": "minimal line",
    "goal_text": "…",
    "source_branch": RELEASE,
    "repos": [
        {
            "path": "/data/repos/fleet-graph",
            "remote": "origin",
            "target_branch": "main",
            "acceptance": ["make verify"],
        },
        {
            "path": "/data/repos/other",
            "remote": "upstream",
            "target_branch": "develop",
            "acceptance": ["make test"],
        },
    ],
}


class TestFinalMergePlan:
    def test_two_repos_each_with_their_own_target(self) -> None:
        plans = final_merge_plan(ENROLL, release_branch=RELEASE)
        assert plans == [
            MergePlan(
                repo_id="/data/repos/fleet-graph",
                source=RELEASE,
                target="main",
                remote="origin",
            ),
            MergePlan(
                repo_id="/data/repos/other",
                source=RELEASE,
                target="develop",
                remote="upstream",
            ),
        ]

    def test_disagreeing_release_branch_raises(self) -> None:
        with pytest.raises(ValueError, match="disagrees"):
            final_merge_plan(ENROLL, release_branch="release/somewhere-else")

    def test_malformed_repo_raises(self) -> None:
        broken = {
            **ENROLL,
            "repos": [{"path": "/data/repos/x", "remote": "", "target_branch": "main"}],
        }
        with pytest.raises(ValueError, match=r"repos\[0\]\.remote"):
            final_merge_plan(broken, release_branch=RELEASE)

    def test_missing_repos_raises(self) -> None:
        with pytest.raises(ValueError, match="repos"):
            final_merge_plan({"source_branch": RELEASE}, release_branch=RELEASE)


# ---------------------------------------------------------------------------
# runner contract: argv lists, never shell strings
# ---------------------------------------------------------------------------


class TestRunnerContract:
    def _git_runner(self) -> FakeVerifyRunner:
        runner = FakeVerifyRunner(
            tips={
                f"{REMOTE}/{SOURCE_BRANCH}": SOURCE_HEAD,
                f"{REMOTE}/{TARGET_BRANCH}": MERGED_COMMIT,
            },
            contains={(SOURCE_HEAD, MERGED_COMMIT)},
        )
        verify_merge_output(_merged_obj(), [_repo()], git_runner=runner)
        return runner

    def test_every_git_argv_is_a_str_list(self) -> None:
        runner = self._git_runner()
        assert runner.calls, "the check made no git calls at all"
        for args, _cwd in runner.calls:
            assert isinstance(args, list)
            assert all(isinstance(part, str) for part in args)
            for sep in (";", "|", "&&", "$(", "`"):
                assert not any(sep in part for part in args), f"shell-ish {sep!r} in {args!r}"

    def test_every_git_argv_carries_the_config_guards(self) -> None:
        """Agent-written worktrees can carry a hostile repo-local config; every
        git argv must include the three guards that neutralize it, directly
        following the git token (mirrored from tests/test_minimal_gitgate.py)."""
        runner = self._git_runner()
        for args, _cwd in runner.calls:
            assert args[0] == "git"
            assert args[1:3] == ["-c", "core.fsmonitor=false"]
            assert "core.hooksPath=/dev/null" in args
            assert "protocol.ext.allow=never" in args

    def test_every_gh_argv_is_a_str_list(self) -> None:
        runner = FakeGhRunner(
            (["pr", "merge"], _ok("")),
            (["pr", "view"], _ok(json.dumps({"mergeCommit": {"oid": MERGED_COMMIT}}))),
        )
        platform_merge(PR, gh_runner=runner)
        assert runner.calls
        for args in runner.calls:
            assert isinstance(args, list)
            assert all(isinstance(part, str) for part in args)
            assert args[0] == "gh"
            for sep in (";", "|", "&&", "$(", "`"):
                assert not any(sep in part for part in args), f"shell-ish {sep!r} in {args!r}"

    def test_module_source_never_uses_shell_true(self) -> None:
        source = Path(mergegate_module.__file__).read_text(encoding="utf-8")
        assert "shell=True" not in source

    def test_module_has_no_global_subprocess(self) -> None:
        # a module-level `import subprocess` would bind the attribute; the
        # only subprocess use is function-local inside SubprocessGhRunner.run
        assert not hasattr(mergegate_module, "subprocess")
