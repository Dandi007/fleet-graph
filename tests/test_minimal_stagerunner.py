"""Tests for stagerunner.py (DD-13): one agent stage end-to-end, zero IO.

Everything here pins the fixed gate -> prompt -> agent -> validate -> gate
order from protocol §0.10 with a fake ``GitRunner`` (argv-pattern answers, like
test_minimal_gitgate.py) and a fake ``AgentInvoker`` that records every call,
so the assertions double as the contract that the agent is invoked at most once
(and never on a pre-gate failure) and produces the exact pending ``events``
list the caller later writes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import fleet_graph.minimal as minimal_pkg
from fleet_graph.minimal import agentrun, gitgate, prompts, protocol
from fleet_graph.minimal.stagerunner import (
    REVIEW_CHANGED_CODE,
    STAGES,
    StageOutcome,
    StageRequest,
    run_stage,
)

SHA_A1 = "a" * 40
SHA_B1 = "b" * 40
SHA_NEW = "d" * 40

_FRAG = {
    "ls_remote": ["ls-remote"],
    "branch": ["rev-parse", "--abbrev-ref", "HEAD"],
    "head": ["rev-parse", "HEAD"],
    "status": ["status", "--porcelain"],
    "merge_base": ["merge-base", "--is-ancestor"],
}


def _matches(args: list[str], fragment: list[str]) -> bool:
    return all(part in args for part in fragment)


class FakeGitRunner:
    """Answers git queries from a mutable per-cwd script (green by default)."""

    def __init__(self) -> None:
        self.repos: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[list[str], str]] = []

    def script_for(self, cwd: str) -> dict[str, Any]:
        return self.repos.setdefault(
            cwd,
            {
                "branch": "feature-x",
                "head": SHA_A1,
                "status": "",
                "ls_remote": SHA_A1,
                "merge_base": 1,
            },
        )

    def run(self, args: list[str], *, cwd: str) -> gitgate.CompletedResult:
        self.calls.append((list(args), cwd))
        s = self.script_for(cwd)
        if _matches(args, _FRAG["ls_remote"]):
            listed = s["ls_remote"]
            if listed is None:
                return gitgate.CompletedResult(0, "", "")
            return gitgate.CompletedResult(0, listed + "\trefs/remotes/origin/feature-x\n", "")
        if _matches(args, _FRAG["branch"]):
            return gitgate.CompletedResult(0, s["branch"] + "\n", "")
        if _matches(args, _FRAG["head"]):
            return gitgate.CompletedResult(0, s["head"] + "\n", "")
        if _matches(args, _FRAG["status"]):
            return gitgate.CompletedResult(0, s["status"], "")
        if _matches(args, _FRAG["merge_base"]):
            return gitgate.CompletedResult(s["merge_base"], "", "")
        raise AssertionError(f"unrecognized git argv: {args!r}")


class FakeInvoker:
    """Records each call (argv + both prompts) and returns a canned result."""

    def __init__(self, exit_code: int = 0, stdout: str = "", *, on_call=None) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.on_call = on_call
        self.calls: list[tuple[list[str], str | None, str]] = []

    def __call__(
        self, argv: list[str], *, system_prompt: str | None, user_prompt: str
    ) -> tuple[int, str]:
        self.calls.append((list(argv), system_prompt, user_prompt))
        if self.on_call is not None:
            self.on_call()
        return self.exit_code, self.stdout


def _repo(worktree: str = "/wt/one", branch: str = "feature-x") -> gitgate.RepoRef:
    return gitgate.RepoRef(worktree=worktree, remote="origin", branch=branch, label="repo-one")


def _impl_in() -> dict[str, Any]:
    return prompts.build_impl_in(
        dd_id="dd-03",
        round=1,
        workspace="/wt/one",
        branch="feature-x",
        base_commit=SHA_A1,
        spec_text="做 X",
        acceptance=["make test"],
        feedback=None,
        history=prompts.history_handle(goal_run_root="/runs/g", dd_id="dd-03"),
    )


def _request(**overrides: Any) -> StageRequest:
    defaults: dict[str, Any] = {
        "stage": "impl",
        "run_id": "run-1",
        "in_obj": _impl_in(),
        "repos": [_repo()],
        "expected_schema": protocol.SCHEMA_IMPL,
        "policy": agentrun.SessionPolicy("resume", 0.7),
        "cwd": "/wt/one",
        "is_first_call": True,
        "session_root": "/root/sessions",
    }
    defaults.update(overrides)
    return StageRequest(**defaults)


def _committed_stdout() -> str:
    return json.dumps(
        {"schema": protocol.SCHEMA_IMPL, "stop": "committed", "commit": SHA_B1, "summary": "改了"}
    )


# ---------------------------------------------------------------------------
# the pre gate: failure must not invoke the agent at all
# ---------------------------------------------------------------------------


class TestPreGate:
    def test_failure_does_not_invoke_agent(self) -> None:
        runner = FakeGitRunner()
        runner.script_for("/wt/one")["status"] = " M src/foo.py\n"
        invoker = FakeInvoker(0, _committed_stdout())

        outcome = run_stage(_request(), git_runner=runner, agent_invoker=invoker)

        assert outcome.ok is False
        assert outcome.invalid_reason == "pre_gate"
        assert [f.code for f in outcome.gate_failures] == [gitgate.FailureCode.DIRTY_WORKTREE]
        assert invoker.calls == []
        assert outcome.events == [
            (
                "agent.invalid_output",
                {
                    "stage": "impl",
                    "run_id": "run-1",
                    "phase": "pre",
                    "failures": [
                        {
                            "repo": "repo-one",
                            "code": gitgate.FailureCode.DIRTY_WORKTREE,
                            "detail": "worktree /wt/one has uncommitted changes",
                        },
                    ],
                },
            )
        ]

    def test_two_bad_repos_are_all_reported(self) -> None:
        runner = FakeGitRunner()
        runner.repos["/wt/a"] = dict(
            branch="feature-x", head=SHA_A1, status="", ls_remote=None, merge_base=1
        )
        runner.repos["/wt/b"] = dict(
            branch="feature-x", head=SHA_A1, status=" M y.py\n", ls_remote=SHA_A1, merge_base=1
        )
        req = _request(
            repos=[
                gitgate.RepoRef(worktree="/wt/a", remote="origin", branch="feature-x", label="ra"),
                gitgate.RepoRef(worktree="/wt/b", remote="origin", branch="feature-x", label="rb"),
            ]
        )
        outcome = run_stage(req, git_runner=runner, agent_invoker=FakeInvoker(0, ""))

        assert [f.code for f in outcome.gate_failures] == [
            gitgate.FailureCode.BRANCH_MISSING_ON_REMOTE,
            gitgate.FailureCode.DIRTY_WORKTREE,
        ]


# ---------------------------------------------------------------------------
# the happy path (impl committed): prompts, one call, agent.exited + finish
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_success_end_to_end(self) -> None:
        runner = FakeGitRunner()
        invoker = FakeInvoker(0, _committed_stdout())

        outcome = run_stage(_request(), git_runner=runner, agent_invoker=invoker)

        assert outcome.ok is True
        assert outcome.stop == "committed"
        assert outcome.obj is not None and outcome.obj["commit"] == SHA_B1
        assert outcome.invalid_reason is None
        assert outcome.gate_failures == []

        assert len(invoker.calls) == 1
        argv, system_prompt, user_prompt = invoker.calls[0]
        assert argv[0] == "agent-run"
        assert argv[argv.index("--role") + 1] == "impl"
        assert argv[argv.index("--cwd") + 1] == "/wt/one"
        assert argv[argv.index("--run-id") + 1] == "run-1"
        assert argv[argv.index("--session-root") + 1] == "/root/sessions"
        assert argv[argv.index("--output-schema") + 1] == json.dumps(
            protocol.describe_schema(protocol.SCHEMA_IMPL)
        )
        assert system_prompt is not None
        assert "恰好一个 JSON 对象" in system_prompt
        assert user_prompt == prompts.render_user_prompt(_impl_in())

        assert outcome.events == [
            (
                "agent.exited",
                {"stage": "impl", "run_id": "run-1", "exit_code": 0, "stop": "committed"},
            ),
            (
                "dd.stage.finished",
                {
                    "stage": "impl",
                    "stop": "committed",
                    "run_id": "run-1",
                    "commit": SHA_B1,
                    "summary": "改了",
                },
            ),
        ]

    def test_argv_is_built_by_agentrun(self) -> None:
        invoker = FakeInvoker(0, _committed_stdout())
        run_stage(_request(), git_runner=FakeGitRunner(), agent_invoker=invoker)
        argv = invoker.calls[0][0]
        for token in ("impl", "run-1", "/wt/one"):
            assert token in argv
        for sep in (";", "|", "&&", "$(", "`"):
            assert not any(sep in part for part in argv)

    def test_resume_non_first_call_skips_system_prompt(self) -> None:
        req = _request(is_first_call=False)
        invoker = FakeInvoker(0, _committed_stdout())
        run_stage(req, git_runner=FakeGitRunner(), agent_invoker=invoker)
        assert invoker.calls[0][1] is None

    def test_fresh_policy_always_sends_system_prompt(self) -> None:
        req = _request(policy=agentrun.SessionPolicy("fresh", None), is_first_call=False)
        invoker = FakeInvoker(0, _committed_stdout())
        run_stage(req, git_runner=FakeGitRunner(), agent_invoker=invoker)
        assert invoker.calls[0][1] is not None


def _review_stdout(role: str = "cr") -> str:
    return json.dumps(
        {"schema": protocol.SCHEMA_REVIEW, "role": role, "stop": "pass", "summary": "过"}
    )


# ---------------------------------------------------------------------------
# non-zero exit -> agent.failed, exactly one call, no re-run
# ---------------------------------------------------------------------------


class TestNonZeroExit:
    def test_plain_nonzero_exit(self) -> None:
        invoker = FakeInvoker(3, "")
        outcome = run_stage(_request(), git_runner=FakeGitRunner(), agent_invoker=invoker)

        assert outcome.ok is False
        assert outcome.invalid_reason == "non_zero_exit"
        assert len(invoker.calls) == 1
        assert outcome.events == [
            (
                "agent.failed",
                {
                    "stage": "impl",
                    "run_id": "run-1",
                    "exit_code": 3,
                    "detail": "agent exited with non-zero exit code 3",
                },
            )
        ]

    def test_runtime_error_detail_is_used(self) -> None:
        stdout = json.dumps(
            {"schema": "runtime.error/1", "stop": "invalid_output", "detail": "boom"}
        )
        invoker = FakeInvoker(1, stdout)
        outcome = run_stage(_request(), git_runner=FakeGitRunner(), agent_invoker=invoker)

        assert outcome.ok is False
        assert outcome.events[0][1]["detail"] == "boom"


# ---------------------------------------------------------------------------
# extract + validate -> agent.invalid_output
# ---------------------------------------------------------------------------


class TestInvalidOutput:
    def test_no_object(self) -> None:
        invoker = FakeInvoker(0, "just prose, no balanced object")
        outcome = run_stage(_request(), git_runner=FakeGitRunner(), agent_invoker=invoker)

        assert outcome.ok is False
        assert outcome.invalid_reason == "no_object"
        assert outcome.events[0][0] == "agent.invalid_output"
        assert "impl/1" in outcome.events[0][1]["detail"]

    def test_validation_failure(self) -> None:
        bad = json.dumps({"schema": protocol.SCHEMA_IMPL, "stop": "failed"})
        invoker = FakeInvoker(0, bad)
        outcome = run_stage(_request(), git_runner=FakeGitRunner(), agent_invoker=invoker)

        assert outcome.ok is False
        assert outcome.invalid_reason == "invalid_output"
        assert outcome.stop == "failed"
        assert "detail" in outcome.events[0][1]["detail"]


# ---------------------------------------------------------------------------
# the post gate: agent committed but did not push / left the tree dirty
# ---------------------------------------------------------------------------


class TestPostGate:
    def test_dirty_after_agent_fails_post_gate(self) -> None:
        runner = FakeGitRunner()

        def dirty_it() -> None:
            runner.repos["/wt/one"]["status"] = " M src/foo.py\n"

        invoker = FakeInvoker(0, _committed_stdout(), on_call=dirty_it)
        outcome = run_stage(_request(), git_runner=runner, agent_invoker=invoker)

        assert outcome.ok is False
        assert outcome.invalid_reason == "post_gate"
        assert [f.code for f in outcome.gate_failures] == [gitgate.FailureCode.DIRTY_WORKTREE]
        kind, payload = outcome.events[0]
        assert kind == "agent.invalid_output"
        assert payload["phase"] == "post"


# ---------------------------------------------------------------------------
# review (cr/fr): a reviewer that edits code is invalid (GO-25 / §0.10)
# ---------------------------------------------------------------------------


class TestReviewUnchanged:
    def test_clean_review_passes(self) -> None:
        invoker = FakeInvoker(0, _review_stdout())
        req = _request(stage="cr", expected_schema=protocol.SCHEMA_REVIEW)
        outcome = run_stage(req, git_runner=FakeGitRunner(), agent_invoker=invoker)

        assert outcome.ok is True
        assert outcome.stop == "pass"
        assert outcome.events == [
            ("agent.exited", {"stage": "cr", "run_id": "run-1", "exit_code": 0, "stop": "pass"}),
            (
                "dd.stage.finished",
                {"stage": "cr", "stop": "pass", "run_id": "run-1", "role": "cr", "summary": "过"},
            ),
        ]

    def test_reviewer_who_committed_new_code_is_invalid(self) -> None:
        runner = FakeGitRunner()

        def commit_it() -> None:
            runner.repos["/wt/one"].update(head=SHA_NEW, ls_remote=SHA_NEW)

        invoker = FakeInvoker(0, _review_stdout(), on_call=commit_it)
        req = _request(stage="cr", expected_schema=protocol.SCHEMA_REVIEW)
        outcome = run_stage(req, git_runner=runner, agent_invoker=invoker)

        assert outcome.ok is False
        assert outcome.invalid_reason == REVIEW_CHANGED_CODE
        assert [f.code for f in outcome.gate_failures] == [REVIEW_CHANGED_CODE]

    def test_reviewer_who_committed_without_pushing_fails_post_gate(self) -> None:
        runner = FakeGitRunner()

        def commit_only() -> None:
            runner.script_for("/wt/one")["head"] = SHA_NEW

        invoker = FakeInvoker(0, _review_stdout(), on_call=commit_only)
        req = _request(stage="fr", expected_schema=protocol.SCHEMA_REVIEW)
        outcome = run_stage(req, git_runner=runner, agent_invoker=invoker)

        assert outcome.ok is False
        assert outcome.invalid_reason == "post_gate"
        assert [f.code for f in outcome.gate_failures] == [gitgate.FailureCode.NOT_PUSHED]


# ---------------------------------------------------------------------------
# stage -> role / finished-kind mapping
# ---------------------------------------------------------------------------


class TestStageMapping:
    @pytest.mark.parametrize("stage", ["impl", "cr", "fr", "goal_review", "merge"])
    def test_dd_internal_stages_finish_with_dd_stage(self, stage: str) -> None:
        schema, stop, extra = {
            "impl": (protocol.SCHEMA_IMPL, "committed", {"commit": SHA_B1, "summary": "x"}),
            "cr": (protocol.SCHEMA_REVIEW, "pass", {"role": "cr", "summary": "x"}),
            "fr": (protocol.SCHEMA_REVIEW, "pass", {"role": "fr", "summary": "x"}),
            "goal_review": (protocol.SCHEMA_GOAL_REVIEW, "approve", {"summary": "x"}),
            "merge": (protocol.SCHEMA_MERGE, "merged", {"merged_commit": SHA_B1}),
        }[stage]
        stdout = json.dumps({"schema": schema, "stop": stop, **extra})
        req = _request(stage=stage, expected_schema=schema)
        outcome = run_stage(req, git_runner=FakeGitRunner(), agent_invoker=FakeInvoker(0, stdout))

        assert outcome.ok is True, outcome.invalid_reason
        assert outcome.events[1][0] == "dd.stage.finished"
        assert outcome.events[1][1]["stage"] == stage

    def test_goal_turn_finishes_with_goal_turn(self) -> None:
        stdout = json.dumps(
            {"schema": protocol.SCHEMA_GOAL_TURN, "stop": "done", "summary": "完成了"}
        )
        req = _request(
            stage="goal_turn",
            expected_schema=protocol.SCHEMA_GOAL_TURN,
            in_obj=prompts.build_goal_turn_in(
                goal={},
                goal_version=1,
                steer_diff=[],
                turn_no=1,
                release_branch="release/g",
                release_head=SHA_A1,
                dd_summary="",
                last_dd=None,
                last_stop=None,
                messages=[],
                warnings=[],
                history=prompts.history_handle(goal_run_root="/runs/g"),
            ),
        )
        outcome = run_stage(req, git_runner=FakeGitRunner(), agent_invoker=FakeInvoker(0, stdout))

        assert outcome.ok is True
        assert outcome.events[1][0] == "goal.turn.finished"
        assert outcome.events[1][1]["run_id"] == "run-1"
        assert outcome.events[1][1]["stop"] == "done"

    def test_scribe_finishes_with_agent_exited_only(self) -> None:
        stdout = json.dumps(
            {"schema": protocol.SCHEMA_SCRIBE, "stop": "observed", "observations": []}
        )
        req = _request(
            stage="scribe",
            expected_schema=protocol.SCHEMA_SCRIBE,
            in_obj=prompts.build_scribe_in(
                goal_id="g-000001",
                goal_version=1,
                trigger="goal.turn.finished",
                since_seq=1,
                until_seq=1,
                new_runs=[],
                prior_observations="/o.jsonl",
                history=prompts.history_handle(goal_run_root="/runs/g"),
            ),
        )
        outcome = run_stage(req, git_runner=FakeGitRunner(), agent_invoker=FakeInvoker(0, stdout))

        assert outcome.ok is True
        assert outcome.events == [
            (
                "agent.exited",
                {"stage": "scribe", "run_id": "run-1", "exit_code": 0, "stop": "observed"},
            ),
        ]

    def test_unknown_stage_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown stage"):
            _request(stage="boss")


# ---------------------------------------------------------------------------
# package surface
# ---------------------------------------------------------------------------


class TestPackageSurface:
    def test_stages_tuple(self) -> None:
        assert STAGES == ("impl", "cr", "fr", "goal_turn", "goal_review", "merge", "scribe")

    def test_init_lists_stagerunner_but_does_not_import_it(self) -> None:
        assert minimal_pkg.__doc__ is not None
        assert "``stagerunner``" in minimal_pkg.__doc__
        assert not hasattr(minimal_pkg, "run_stage")
        assert not hasattr(minimal_pkg, "StageRequest")

    def test_outcome_is_a_frozen_value(self) -> None:
        from dataclasses import FrozenInstanceError

        outcome = StageOutcome(
            ok=True, stop="x", obj=None, invalid_reason=None, gate_failures=[], events=[]
        )
        with pytest.raises(FrozenInstanceError):
            outcome.ok = False  # type: ignore[misc]
