"""The mechanical acceptance step: declared argv in, facts out, no verdicts."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from fleet_graph.acceptance import (
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    STATUS_NOT_DECLARED,
    STATUS_RAN,
    STATUS_SKIPPED_NO_CWD,
    TAIL,
    AcceptanceRunner,
    AcceptanceSpec,
    acceptance_environment,
    run_acceptance,
)


class TestSpecJsonRoundTrip:
    """The declaration crosses the systemd-run boundary as one JSON argument;
    what the scheduler serialises must be exactly what the line parses."""

    def test_round_trips_argvs_cwd_and_timeout(self) -> None:
        spec = AcceptanceSpec(
            argvs=(("systemctl", "--user", "is-active", "loop-engine-jobd"),),
            cwd="/tmp",
            timeout_seconds=120,
        )
        assert AcceptanceSpec.from_cli_json(spec.to_cli_json()) == spec

    def test_round_trips_a_missing_cwd(self) -> None:
        spec = AcceptanceSpec(argvs=(("true",),), cwd=None)
        assert AcceptanceSpec.from_cli_json(spec.to_cli_json()).cwd is None

    def test_a_non_object_is_refused(self) -> None:
        with pytest.raises(ValueError):
            AcceptanceSpec.from_cli_json(json.dumps(["not", "an", "object"]))


class TestAbsenceIsStated:
    """ "No acceptance was declared" and "acceptance passed" must never be
    confusable -- the NOT-RUN rounds this step exists to end were silent."""

    def test_no_spec_is_not_declared(self) -> None:
        assert run_acceptance(None) == {"status": STATUS_NOT_DECLARED}

    def test_an_empty_command_list_is_not_declared(self) -> None:
        assert run_acceptance(AcceptanceSpec(argvs=(), cwd="/tmp")) == {
            "status": STATUS_NOT_DECLARED
        }

    def test_commands_without_a_cwd_are_refused_out_loud(self) -> None:
        """Where a command runs is part of the reviewed declaration; the step
        must never inherit the engine's own working directory."""
        facts = run_acceptance(AcceptanceSpec(argvs=(("true",), ("false",)), cwd=None))
        assert facts["status"] == STATUS_SKIPPED_NO_CWD
        assert facts["commands"] == 2


class TestExecution:
    def test_green_and_red_both_report(self, tmp_path: Any) -> None:
        facts = run_acceptance(
            AcceptanceSpec(
                argvs=(
                    ("sh", "-c", "echo all good"),
                    ("sh", "-c", "echo went wrong >&2; exit 3"),
                ),
                cwd=str(tmp_path),
            )
        )
        assert facts["status"] == STATUS_RAN
        first, second = facts["results"]
        assert first["exit_code"] == 0
        assert "all good" in first["tail"]["stdout"]
        assert second["exit_code"] == 3
        assert "went wrong" in second["tail"]["stderr"]
        assert all(r["duration_s"] >= 0 for r in facts["results"])

    def test_a_red_command_does_not_swallow_the_next(self, tmp_path: Any) -> None:
        facts = run_acceptance(
            AcceptanceSpec(
                argvs=(("sh", "-c", "exit 1"), ("sh", "-c", "exit 0")), cwd=str(tmp_path)
            )
        )
        assert [r["exit_code"] for r in facts["results"]] == [1, 0]

    def test_tails_are_truncated_per_stream(self, tmp_path: Any) -> None:
        facts = run_acceptance(
            AcceptanceSpec(
                argvs=(("python3", "-c", "print('x' * 5000)"),),
                cwd=str(tmp_path),
            )
        )
        assert len(facts["results"][0]["tail"]["stdout"]) <= TAIL

    def test_command_not_found_gets_the_synthetic_127(self, tmp_path: Any) -> None:
        facts = run_acceptance(
            AcceptanceSpec(argvs=(("no-such-command-r0d-xyz",),), cwd=str(tmp_path))
        )
        result = facts["results"][0]
        assert result["exit_code"] == EXIT_NOT_FOUND
        assert "not found" in result["tail"]["stderr"]

    def test_timeout_gets_the_synthetic_124(self) -> None:
        def slow_run(*args: Any, **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

        facts = run_acceptance(
            AcceptanceSpec(argvs=(("sleep", "3600"),), cwd="/tmp", timeout_seconds=5),
            run=slow_run,
        )
        result = facts["results"][0]
        assert result["exit_code"] == EXIT_TIMEOUT
        assert "timed out after 5s" in result["tail"]["stderr"]

    def test_timeout_is_per_command_and_reaches_subprocess(self) -> None:
        seen: list[dict[str, Any]] = []

        def spy_run(argv: Any, **kwargs: Any) -> Any:
            seen.append(kwargs)

            class Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            return Proc()

        run_acceptance(
            AcceptanceSpec(argvs=(("a",), ("b",)), cwd="/tmp", timeout_seconds=42), run=spy_run
        )
        assert [k["timeout"] for k in seen] == [42, 42]
        assert [k["cwd"] for k in seen] == ["/tmp", "/tmp"]


class TestEnvironmentIsolation:
    """The scheduler's own environment (bus tokens, gateway credentials) must
    never leak into a command an agent's work is graded by."""

    def test_only_the_whitelist_survives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FLEET_GRAPH_SECRET_TOKEN", "leaky")
        env = acceptance_environment()
        assert "FLEET_GRAPH_SECRET_TOKEN" not in env
        assert set(env) <= {"PATH", "HOME"}

    def test_the_subprocess_sees_the_stripped_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("LEAKY_VAR", "should-not-appear")
        facts = run_acceptance(
            AcceptanceSpec(argvs=(("sh", "-c", "echo ${LEAKY_VAR:-absent}"),), cwd=str(tmp_path))
        )
        assert "absent" in facts["results"][0]["tail"]["stdout"]


class TestRunnerPort:
    def test_the_runner_wraps_its_spec(self) -> None:
        assert AcceptanceRunner(None).run() == {"status": STATUS_NOT_DECLARED}
