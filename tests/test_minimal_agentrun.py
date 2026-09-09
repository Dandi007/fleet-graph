"""Tests for the minimal agent-run adapter (DD-05): session policy, argv, parsing.

Every run here goes through a fake runner -- no real ``agent-run`` subprocess is
ever spawned -- so the assertions double as the contract that each invocation is
a plain argv list executed in an explicit cwd, never a shell string, and that
every failure is a data value (``AgentResult.failure_code``) rather than an
exception.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from fleet_graph.minimal.agentrun import (
    DEFAULT_SESSION_POLICIES,
    ROLES,
    AgentCall,
    AgentRunTimeout,
    Completed,
    FailureCode,
    SessionPolicy,
    build_argv,
    parse_stop,
    resolve_session_policy,
    resume_args,
    run_agent,
    schema_for,
)
from fleet_graph.minimal.protocol import (
    SCHEMA_GOAL_REVIEW,
    SCHEMA_GOAL_TURN,
    SCHEMA_IMPL,
    SCHEMA_MERGE,
    SCHEMA_REVIEW,
    SCHEMA_SCRIBE,
)

SHA = "0" * 40


def _call(**overrides: object) -> AgentCall:
    defaults: dict[str, object] = {
        "role": "impl",
        "call_kind": None,
        "run_id": "run-1",
        "cwd": "/ws/dd-01",
        "session_root": "/root/sessions",
        "timeout_s": 300,
        "output_schema_json": json.dumps({"schema": SCHEMA_IMPL}),
    }
    defaults.update(overrides)
    return AgentCall(**defaults)


# ---------------------------------------------------------------------------
# schema_for
# ---------------------------------------------------------------------------


class TestRoles:
    def test_roles_are_the_six(self) -> None:
        assert ROLES == ("goal", "impl", "cr", "fr", "merge", "scribe")


class TestSchemaFor:
    def test_goal_turn(self) -> None:
        assert schema_for("goal", "turn") == SCHEMA_GOAL_TURN

    def test_goal_review(self) -> None:
        assert schema_for("goal", "review") == SCHEMA_GOAL_REVIEW

    def test_impl(self) -> None:
        assert schema_for("impl") == SCHEMA_IMPL

    def test_cr_and_fr_share_review_schema(self) -> None:
        assert schema_for("cr") == SCHEMA_REVIEW
        assert schema_for("fr") == SCHEMA_REVIEW

    def test_merge(self) -> None:
        assert schema_for("merge") == SCHEMA_MERGE

    def test_scribe(self) -> None:
        assert schema_for("scribe") == SCHEMA_SCRIBE

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(ValueError):
            schema_for("boss")

    def test_unknown_goal_call_kind_raises(self) -> None:
        with pytest.raises(ValueError):
            schema_for("goal", "dispatch")

    def test_goal_without_call_kind_raises(self) -> None:
        with pytest.raises(ValueError):
            schema_for("goal")


# ---------------------------------------------------------------------------
# session policy
# ---------------------------------------------------------------------------


class TestDefaultSessionPolicies:
    def test_defaults_match_protocol_table(self) -> None:
        assert {
            "goal": SessionPolicy("resume", 0.7),
            "impl": SessionPolicy("resume", 0.7),
            "cr": SessionPolicy("resume", 0.8),
            "fr": SessionPolicy("resume", 0.8),
            "merge": SessionPolicy("fresh", None),
            "scribe": SessionPolicy("resume", 0.6),
        } == DEFAULT_SESSION_POLICIES

    def test_policy_is_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            SessionPolicy("resume", 0.5).mode = "fresh"  # type: ignore[misc]


class TestResolveSessionPolicy:
    def test_returns_default_without_override(self) -> None:
        assert resolve_session_policy("impl", {}) == SessionPolicy("resume", 0.7)

    def test_none_overrides_treated_as_empty(self) -> None:
        assert resolve_session_policy("merge", None) == SessionPolicy("fresh", None)

    def test_partial_override_of_compact_at(self) -> None:
        assert resolve_session_policy("impl", {"impl": {"compact_at": 0.5}}) == SessionPolicy(
            "resume", 0.5
        )

    def test_partial_override_of_compact_at_upper_bound(self) -> None:
        assert resolve_session_policy("impl", {"impl": {"compact_at": 1.0}}) == SessionPolicy(
            "resume", 1.0
        )

    def test_override_only_touches_the_named_role(self) -> None:
        overrides = {"goal": {"mode": "fresh", "compact_at": None}}
        assert resolve_session_policy("impl", overrides) == SessionPolicy("resume", 0.7)

    def test_switching_to_fresh_clears_compact_at(self) -> None:
        overrides = {"cr": {"mode": "fresh", "compact_at": None}}
        assert resolve_session_policy("cr", overrides) == SessionPolicy("fresh", None)

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(ValueError):
            resolve_session_policy("boss", {})

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError):
            resolve_session_policy("impl", {"impl": {"mode": "sometimes"}})

    def test_fresh_inheriting_compact_at_raises(self) -> None:
        # cr defaults to compact_at 0.8; overriding only the mode to fresh
        # leaves that threshold in place, which a fresh session cannot carry.
        with pytest.raises(ValueError):
            resolve_session_policy("cr", {"cr": {"mode": "fresh"}})

    def test_fresh_with_explicit_compact_at_raises(self) -> None:
        with pytest.raises(ValueError):
            resolve_session_policy("merge", {"merge": {"compact_at": 0.5}})

    @pytest.mark.parametrize("bad", [1.5, 0.0, -0.1])
    def test_compact_at_out_of_range_raises(self, bad: float) -> None:
        with pytest.raises(ValueError):
            resolve_session_policy("impl", {"impl": {"compact_at": bad}})


class TestResumeArgs:
    def test_no_last_run_is_fresh(self) -> None:
        assert resume_args(SessionPolicy("resume", 0.7), None, session_root="/sessions") == (
            None,
            None,
        )

    def test_fresh_policy_makes_no_resume(self) -> None:
        assert resume_args(SessionPolicy("fresh", None), "run-0", session_root="/sessions") == (
            None,
            None,
        )

    def test_resume_joins_session_root_and_id(self) -> None:
        assert resume_args(SessionPolicy("resume", 0.7), "run-0", session_root="/sessions") == (
            "/sessions/run-0",
            0.7,
        )

    def test_empty_session_root_uses_id_alone(self) -> None:
        assert resume_args(SessionPolicy("resume", 0.8), "run-1", session_root="") == (
            "run-1",
            0.8,
        )


# ---------------------------------------------------------------------------
# build_argv
# ---------------------------------------------------------------------------


class TestBuildArgv:
    def test_fresh_shape_exactly(self) -> None:
        call = _call(
            role="merge",
            output_schema_json=json.dumps({"schema": SCHEMA_MERGE}),
        )
        assert build_argv(call) == [
            "agent-run",
            "--role",
            "merge",
            "--harness",
            "merge",
            "--session-root",
            "/root/sessions",
            "--run-id",
            "run-1",
            "--output-schema",
            json.dumps({"schema": SCHEMA_MERGE}),
            "--isolation",
            "full",
            "--timeout",
            "300",
            "--cwd",
            "/ws/dd-01",
        ]

    def test_resume_shape_exactly(self) -> None:
        call = _call(resume_dir="/root/sessions/run-0", model="claude-opus-5")
        assert build_argv(call) == [
            "agent-run",
            "--role",
            "impl",
            "--harness",
            "impl",
            "--session-root",
            "/root/sessions",
            "--run-id",
            "run-1",
            "--output-schema",
            json.dumps({"schema": SCHEMA_IMPL}),
            "--isolation",
            "full",
            "--timeout",
            "300",
            "--cwd",
            "/ws/dd-01",
            "--resume",
            "/root/sessions/run-0",
            "--model",
            "claude-opus-5",
        ]

    def test_harness_defaults_to_role(self) -> None:
        assert _call().harness == "impl"

    def test_explicit_harness_is_used(self) -> None:
        call = _call(harness="impl-code")
        argv = build_argv(call)
        assert argv[argv.index("--harness") + 1] == "impl-code"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("role", "impl; rm -rf /"),
            ("harness", "impl$(whoami)"),
            ("run_id", "run 1"),
        ],
    )
    def test_injection_characters_rejected(self, field: str, value: str) -> None:
        call = _call(**{field: value})
        with pytest.raises(ValueError):
            build_argv(call)

    def test_whitelisted_tokens_are_accepted(self) -> None:
        call = _call(role="aimpl.1_x-2", run_id="run.1_x-2")
        assert build_argv(call)[2] == "aimpl.1_x-2"

    @pytest.mark.parametrize("bad", [0, -5])
    def test_non_positive_timeout_raises(self, bad: int) -> None:
        with pytest.raises(ValueError):
            build_argv(_call(timeout_s=bad))

    def test_non_integer_timeout_raises(self) -> None:
        with pytest.raises(ValueError):
            build_argv(_call(timeout_s="300"))  # type: ignore[arg-type]

    def test_argv_is_a_plain_list_without_shell_metacharacters(self) -> None:
        argv = build_argv(_call(resume_dir="/root/r 0"))
        assert isinstance(argv, list)
        for token in argv:
            assert isinstance(token, str)
            assert token != "agent-run" or token == "agent-run"
        assert argv[0] == "agent-run"
        for sep in (";", "|", "&&", "$(", "`"):
            assert not any(sep in token for token in argv), f"shell-ish {sep!r} in {argv!r}"


class TestBuildArgvCompactAt:
    def test_compact_at_sits_between_resume_and_model(self) -> None:
        call = _call(resume_dir="/root/sessions/run-0", compact_at=0.7, model="claude-opus-5")
        argv = build_argv(call)
        assert argv.index("--compact-at") == argv.index("--resume") + 2
        assert argv.index("--compact-at") < argv.index("--model")

    def test_compact_at_token_shape(self) -> None:
        call = _call(resume_dir="/root/sessions/run-0", compact_at=0.7)
        argv = build_argv(call)
        assert argv[argv.index("--resume") + 1] == "/root/sessions/run-0"
        assert argv[argv.index("--compact-at") + 1] == "0.7"
        assert "--model" not in argv

    def test_ratio_is_a_short_form(self) -> None:
        call = _call(resume_dir="/root/sessions/run-0", compact_at=0.75)
        argv = build_argv(call)
        assert argv[argv.index("--compact-at") + 1] == "0.75"

    def test_compact_at_without_resume_dir_raises(self) -> None:
        with pytest.raises(ValueError):
            build_argv(_call(compact_at=0.7))

    def test_compact_at_none_omits_the_token(self) -> None:
        argv = build_argv(_call(resume_dir="/root/sessions/run-0"))
        assert "--compact-at" not in argv


# ---------------------------------------------------------------------------
# parse_stop
# ---------------------------------------------------------------------------


class TestParseStop:
    def test_ok(self) -> None:
        obj = {"schema": SCHEMA_IMPL, "stop": "committed", "commit": SHA, "summary": "改了"}
        result = parse_stop(json.dumps(obj), SCHEMA_IMPL, 0)
        assert result.ok is True
        assert result.stop == "committed"
        assert result.obj == obj
        assert result.failure_code is None
        assert result.exit_code == 0

    def test_nonzero_without_runtime_error(self) -> None:
        result = parse_stop("", SCHEMA_IMPL, 3)
        assert result.ok is False
        assert result.failure_code == FailureCode.NONZERO_EXIT
        assert result.exit_code == 3

    def test_nonzero_with_runtime_error_invalid_output(self) -> None:
        stdout = json.dumps(
            {"schema": "runtime.error/1", "stop": "invalid_output", "detail": "boom"}
        )
        result = parse_stop(stdout, SCHEMA_IMPL, 1)
        assert result.ok is False
        assert result.failure_code == FailureCode.INVALID_OUTPUT
        assert result.detail == "boom"
        assert result.exit_code == 1

    def test_nonzero_with_runtime_error_timeout(self) -> None:
        stdout = json.dumps({"schema": "runtime.error/1", "stop": "timeout", "detail": "慢"})
        result = parse_stop(stdout, SCHEMA_IMPL, 1)
        assert result.failure_code == FailureCode.TIMEOUT
        assert result.detail == "慢"

    def test_prose_and_multiple_objects_picks_last(self) -> None:
        first = {"schema": SCHEMA_IMPL, "stop": "banana"}
        last = {"schema": SCHEMA_IMPL, "stop": "committed", "commit": "1" * 40, "summary": "last"}
        stdout = "prose\n" + json.dumps(first) + "\nmore prose\n" + json.dumps(last) + "\nend"
        result = parse_stop(stdout, SCHEMA_IMPL, 0)
        assert result.ok is True
        assert result.obj is not None and result.obj["summary"] == "last"

    def test_schema_name_mismatch(self) -> None:
        obj = {"schema": SCHEMA_MERGE, "stop": "merged", "merged_commit": "a" * 40}
        result = parse_stop(json.dumps(obj), SCHEMA_IMPL, 0)
        assert result.ok is False
        assert result.failure_code == FailureCode.SCHEMA_MISMATCH

    def test_no_object(self) -> None:
        result = parse_stop("just prose, nothing balanced", SCHEMA_IMPL, 0)
        assert result.ok is False
        assert result.failure_code == FailureCode.NO_OBJECT

    def test_review_fail_without_blocker_or_major_is_invalid_output(self) -> None:
        obj = {
            "schema": SCHEMA_REVIEW,
            "stop": "fail",
            "role": "cr",
            "findings": [{"severity": "note", "detail": "看起来还行"}],
        }
        result = parse_stop(json.dumps(obj), SCHEMA_REVIEW, 0)
        assert result.ok is False
        assert result.failure_code == FailureCode.INVALID_OUTPUT
        assert "blocker, major" in (result.detail or "")
        assert result.obj == obj

    def test_stop_out_of_enum_is_invalid_output(self) -> None:
        obj = {"schema": SCHEMA_IMPL, "stop": "banana"}
        result = parse_stop(json.dumps(obj), SCHEMA_IMPL, 0)
        assert result.failure_code == FailureCode.INVALID_OUTPUT


# ---------------------------------------------------------------------------
# run_agent
# ---------------------------------------------------------------------------


class FakeRunner:
    def __init__(self, completed: Completed | None = None, *, timeout: bool = False) -> None:
        self.completed = completed
        self.timeout = timeout
        self.calls: list[tuple[list[str], str, int]] = []

    def run(self, argv: list[str], cwd: str, timeout_s: int) -> Completed:
        self.calls.append((list(argv), cwd, timeout_s))
        if self.timeout:
            raise AgentRunTimeout(timeout_s, argv, cwd)
        assert self.completed is not None
        return self.completed


class TestRunAgent:
    def test_success_roundtrip(self) -> None:
        obj = {"schema": SCHEMA_IMPL, "stop": "committed", "commit": SHA, "summary": "x"}
        runner = FakeRunner(Completed(0, json.dumps(obj), ""))
        call = _call()
        result = run_agent(call, runner=runner)
        assert result.ok is True
        assert result.stop == "committed"
        assert result.argv == build_argv(call)

    def test_timeout_maps_to_timeout_failure_code(self) -> None:
        runner = FakeRunner(timeout=True)
        result = run_agent(_call(), runner=runner)
        assert result.ok is False
        assert result.failure_code == FailureCode.TIMEOUT

    def test_forwards_argv_cwd_and_timeout(self) -> None:
        obj = {"schema": SCHEMA_IMPL, "stop": "failed", "detail": "x"}
        runner = FakeRunner(Completed(0, json.dumps(obj), ""))
        call = _call(timeout_s=42)
        run_agent(call, runner=runner)
        assert len(runner.calls) == 1
        argv, cwd, timeout_s = runner.calls[0]
        assert argv == build_argv(call)
        assert cwd == "/ws/dd-01"
        assert timeout_s == 42

    def test_goal_turn_uses_goal_schema(self) -> None:
        obj = {"schema": SCHEMA_GOAL_TURN, "stop": "done", "summary": "完成了"}
        runner = FakeRunner(Completed(0, json.dumps(obj), ""))
        call = _call(role="goal", call_kind="turn")
        assert run_agent(call, runner=runner).ok is True

    def test_goal_review_uses_goal_review_schema(self) -> None:
        obj = {"schema": SCHEMA_GOAL_REVIEW, "stop": "approve", "summary": "过"}
        runner = FakeRunner(Completed(0, json.dumps(obj), ""))
        call = _call(role="goal", call_kind="review")
        assert run_agent(call, runner=runner).ok is True


# ---------------------------------------------------------------------------
# failure codes are exported constants
# ---------------------------------------------------------------------------


class TestFailureCodes:
    def test_codes_are_stable_strings(self) -> None:
        assert FailureCode.NONZERO_EXIT == "nonzero_exit"
        assert FailureCode.INVALID_OUTPUT == "invalid_output"
        assert FailureCode.NO_OBJECT == "no_object"
        assert FailureCode.SCHEMA_MISMATCH == "schema_mismatch"
        assert FailureCode.TIMEOUT == "timeout"
