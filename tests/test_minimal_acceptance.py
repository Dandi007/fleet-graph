"""Tests for the minimal acceptance runner (fail-fast batch + projections).

All batch logic runs against an injected fake Runner that records calls, so
the fail-fast contract is asserted from the call log itself (later commands
must never run). One class at the end exercises the real BashRunner,
including the whole-process-group kill on timeout.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from fleet_graph.minimal.acceptance import (
    TAIL_LIMIT,
    AcceptanceRun,
    BashRunner,
    CommandResult,
    Completed,
    acceptance_event_payload,
    acceptance_results,
    combine_acceptance,
    run_acceptance,
    tail_of,
)


class RecordingRunner:
    """Fake Runner answering from a per-command script and recording calls.

    Each script entry maps a command to a ``(exit_code, output, timed_out)``
    tuple. Unscripted commands fail loudly so a test cannot pass because a
    typo silently ran the green default.
    """

    def __init__(self, script: dict[str, tuple[int, str, bool]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[dict[str, Any]] = []

    def run(self, cmd: str, cwd: str, timeout_s: int, env: dict[str, str] | None) -> Completed:
        self.calls.append({"cmd": cmd, "cwd": cwd, "timeout_s": timeout_s, "env": env})
        if cmd not in self.script:
            raise AssertionError(f"unscripted command {cmd!r}; scripted: {sorted(self.script)}")
        exit_code, output, timed_out = self.script[cmd]
        return Completed(exit_code=exit_code, output=output, timed_out=timed_out)


def _green(cmd: str = "make test", output: str = "ok\n") -> dict[str, tuple[int, str, bool]]:
    return {cmd: (0, output, False)}


# ---------------------------------------------------------------------------
# combine_acceptance
# ---------------------------------------------------------------------------


class TestCombineAcceptance:
    def test_extra_appended_after_base_in_order(self) -> None:
        assert combine_acceptance(["make test"], ["pytest tests/test_x.py", "make e2e"]) == [
            "make test",
            "pytest tests/test_x.py",
            "make e2e",
        ]

    def test_dedup_keeps_first_occurrence_and_order(self) -> None:
        assert combine_acceptance(
            ["make test", "make lint", "make test"],
            ["make lint", "pytest tests/test_x.py"],
        ) == ["make test", "make lint", "pytest tests/test_x.py"]

    def test_extra_duplicating_base_dedups(self) -> None:
        assert combine_acceptance(["make test"], ["make test"]) == ["make test"]

    def test_empty_strings_skipped_in_base_and_extra(self) -> None:
        assert combine_acceptance(["make test", ""], ["", "make e2e"]) == ["make test", "make e2e"]

    def test_base_of_only_empty_strings_raises(self) -> None:
        with pytest.raises(ValueError):
            combine_acceptance(["", ""], ["make test"])

    @pytest.mark.parametrize("base", [[], None])
    def test_empty_base_raises(self, base: list[str] | None) -> None:
        with pytest.raises(ValueError):
            combine_acceptance(base, ["make test"])

    def test_extra_none_or_empty_ok(self) -> None:
        assert combine_acceptance(["make test"], None) == ["make test"]
        assert combine_acceptance(["make test"], []) == ["make test"]


# ---------------------------------------------------------------------------
# tail_of
# ---------------------------------------------------------------------------


class TestTailOf:
    def test_short_text_verbatim(self) -> None:
        assert tail_of("hello\nworld\n") == "hello\nworld\n"
        assert tail_of("") == ""

    def test_truncated_cut_at_line_boundary(self) -> None:
        # 5 lines of 10 chars; limit 24 fits lines 4-5 plus their newlines.
        text = "0123456789\n" * 5
        out = tail_of(text, 24)
        assert out.startswith("…[truncated ")
        assert out.endswith("0123456789\n0123456789\n")
        assert "0123456789\n0123456789\n0123456789" not in out

    def test_marker_names_dropped_chars(self) -> None:
        text = "aaaa\nbbbb\n"
        out = tail_of(text, 5)
        assert out.startswith("…[truncated 5 chars]\n")
        assert out.endswith("bbbb\n")

    def test_keeps_line_starting_at_boundary(self) -> None:
        # len("bb\n") == 3 == limit, and the char before the tail region is a
        # newline: the whole line "bb\n" is kept and the cut is clean.
        assert tail_of("aaaa\nbb\n", 3) == "…[truncated 5 chars]\nbb\n"

    def test_result_length_bounded(self) -> None:
        text = "".join(f"line-{i:03d}\n" for i in range(500))
        out = tail_of(text, 100)
        assert len(out) <= 100 + len("…[truncated 99999 chars]\n")

    def test_last_line_alone_over_limit_cut_at_char_limit(self) -> None:
        text = "x" * 300
        out = tail_of(text, 100)
        assert out == "…[truncated 200 chars]\n" + "x" * 100

    def test_no_newline_in_tail_region_cuts_line(self) -> None:
        # Only newline is before the tail region: line-boundary cut impossible.
        text = "head\n" + "y" * 300
        out = tail_of(text, 100)
        assert out == "…[truncated 205 chars]\n" + "y" * 100

    def test_default_limit_constant(self) -> None:
        assert TAIL_LIMIT == 4000
        text = "a" * (TAIL_LIMIT + 1)
        out = tail_of(text)
        assert out == "…[truncated 1 chars]\n" + "a" * TAIL_LIMIT
        assert len(out) <= TAIL_LIMIT + len("…[truncated 99999 chars]\n")

    def test_negative_limit_raises(self) -> None:
        with pytest.raises(ValueError):
            tail_of("abc", -1)


# ---------------------------------------------------------------------------
# run_acceptance (fake runner)
# ---------------------------------------------------------------------------


class TestRunAcceptance:
    def test_all_green_passes(self) -> None:
        runner = RecordingRunner(
            {"make lint": (0, "lint ok\n", False), "make test": (0, "t\n", False)}
        )
        run = run_acceptance(["make lint", "make test"], cwd="/wt", runner=runner, timeout_s=60)
        assert run.passed is True
        assert run.failed_cmd is None
        assert [r.cmd for r in run.results] == ["make lint", "make test"]
        assert all(r.exit_code == 0 for r in run.results)
        assert all(r.timed_out is False for r in run.results)

    def test_fail_fast_second_failing_stops_before_third(self) -> None:
        runner = RecordingRunner(
            {
                "cmd-one": (0, "one\n", False),
                "cmd-two": (1, "boom\n", False),
                "cmd-three": (0, "three\n", False),
            }
        )
        run = run_acceptance(
            ["cmd-one", "cmd-two", "cmd-three"], cwd="/wt", runner=runner, timeout_s=60
        )
        assert [c["cmd"] for c in runner.calls] == ["cmd-one", "cmd-two"]
        assert run.passed is False
        assert run.failed_cmd == "cmd-two"
        assert [r.cmd for r in run.results] == ["cmd-one", "cmd-two"]
        assert run.results[-1].exit_code == 1
        assert run.results[-1].timed_out is False

    def test_timeout_stops_batch_and_marks_timed_out(self) -> None:
        runner = RecordingRunner({"slow": (0, "", True), "next": (0, "", False)})
        run = run_acceptance(["slow", "next"], cwd="/wt", runner=runner, timeout_s=30)
        assert [c["cmd"] for c in runner.calls] == ["slow"]
        assert run.passed is False
        assert run.failed_cmd == "slow"
        assert run.results[0].timed_out is True

    def test_nonzero_exit_code_with_timed_out_flag_fails(self) -> None:
        # Defensive: even a runner reporting a zero exit together with a
        # timeout must count as a failure.
        runner = RecordingRunner({"weird": (0, "", True), "after": (0, "", False)})
        run = run_acceptance(["weird", "after"], cwd="/wt", runner=runner, timeout_s=30)
        assert run.passed is False
        assert run.failed_cmd == "weird"
        assert [r.cmd for r in run.results] == ["weird"]

    def test_first_command_failing_records_only_it(self) -> None:
        runner = RecordingRunner({"bad": (2, "err\n", False), "good": (0, "", False)})
        run = run_acceptance(["bad", "good"], cwd="/wt", runner=runner, timeout_s=60)
        assert run.passed is False
        assert run.failed_cmd == "bad"
        assert [r.cmd for r in run.results] == ["bad"]

    def test_single_command_batches(self) -> None:
        green = RecordingRunner(_green())
        assert run_acceptance(["make test"], cwd="/wt", runner=green, timeout_s=60).passed is True
        bad = RecordingRunner({"make test": (1, "x", False)})
        run = run_acceptance(["make test"], cwd="/wt", runner=bad, timeout_s=60)
        assert run.passed is False
        assert run.failed_cmd == "make test"

    def test_empty_cmds_raises(self) -> None:
        with pytest.raises(ValueError):
            run_acceptance([], cwd="/wt", runner=RecordingRunner(), timeout_s=60)

    def test_runner_receives_cwd_timeout_and_env(self) -> None:
        runner = RecordingRunner(_green())
        env = {"PATH": "/usr/bin"}
        run_acceptance(["make test"], cwd="/wt/x", runner=runner, timeout_s=77, env=env)
        assert runner.calls == [{"cmd": "make test", "cwd": "/wt/x", "timeout_s": 77, "env": env}]

    def test_env_none_is_passed_through(self) -> None:
        runner = RecordingRunner(_green())
        run_acceptance(["make test"], cwd="/wt", runner=runner, timeout_s=60, env=None)
        assert runner.calls[0]["env"] is None

    def test_output_tailed_into_result(self) -> None:
        runner = RecordingRunner({"noisy": (1, "e" * 9999, False)})
        run = run_acceptance(["noisy"], cwd="/wt", runner=runner, timeout_s=60)
        assert len(run.results[0].tail) <= TAIL_LIMIT + len("…[truncated 99999 chars]\n")
        assert run.results[0].tail.endswith("e" * 4000)

    def test_duration_measured_positive(self) -> None:
        runner = RecordingRunner(_green())
        run = run_acceptance(["make test"], cwd="/wt", runner=runner, timeout_s=60)
        assert run.results[0].duration_s >= 0.0


# ---------------------------------------------------------------------------
# projections
# ---------------------------------------------------------------------------


class TestProjections:
    def _run(self) -> AcceptanceRun:
        runner = RecordingRunner(
            {"make lint": (0, "lint\n", False), "make test": (3, "boom\n", False)}
        )
        return run_acceptance(["make lint", "make test"], cwd="/wt", runner=runner, timeout_s=60)

    def test_acceptance_results_shape_keys_and_order(self) -> None:
        run = self._run()
        assert acceptance_results(run) == [
            {"cmd": "make lint", "exit": 0, "tail": "lint\n"},
            {"cmd": "make test", "exit": 3, "tail": "boom\n"},
        ]

    def test_acceptance_results_uses_exit_not_exit_code(self) -> None:
        for entry in acceptance_results(self._run()):
            assert "exit" in entry
            assert "exit_code" not in entry
            assert "duration_s" not in entry
            assert "timed_out" not in entry

    def test_acceptance_results_empty_only_when_nothing_ran(self) -> None:
        # A passing single-command batch always yields one entry; the empty
        # list is only reachable via a hand-built run with no results.
        runner = RecordingRunner(_green())
        run = run_acceptance(["make test"], cwd="/wt", runner=runner, timeout_s=60)
        assert len(acceptance_results(run)) == 1
        assert acceptance_results(AcceptanceRun(results=(), passed=False, failed_cmd=None)) == []

    def test_acceptance_event_payload_fields(self) -> None:
        run = self._run()
        payload = acceptance_event_payload(run.results[0], index=0, total=2)
        assert payload == {
            "cmd": "make lint",
            "exit": 0,
            "tail": "lint\n",
            "duration_s": payload["duration_s"],
            "timed_out": False,
            "index": 0,
            "total": 2,
        }
        assert isinstance(payload["duration_s"], float)

    def test_acceptance_event_payload_for_failed_command(self) -> None:
        run = self._run()
        payload = acceptance_event_payload(run.results[1], index=1, total=2)
        assert payload["cmd"] == "make test"
        assert payload["exit"] == 3
        assert payload["timed_out"] is False
        assert payload["index"] == 1
        assert payload["total"] == 2

    def test_acceptance_event_payload_from_handbuilt_result(self) -> None:
        result = CommandResult(
            cmd="sleep 5", exit_code=-9, tail="", duration_s=1.25, timed_out=True
        )
        payload = acceptance_event_payload(result, index=2, total=3)
        assert payload == {
            "cmd": "sleep 5",
            "exit": -9,
            "tail": "",
            "duration_s": 1.25,
            "timed_out": True,
            "index": 2,
            "total": 3,
        }


# ---------------------------------------------------------------------------
# BashRunner (real subprocesses)
# ---------------------------------------------------------------------------


class TestBashRunner:
    def test_true_exits_zero(self, tmp_path: Any) -> None:
        completed = BashRunner().run("true", str(tmp_path), 30, None)
        assert completed.exit_code == 0
        assert completed.timed_out is False
        assert completed.output == ""

    def test_false_exits_nonzero(self, tmp_path: Any) -> None:
        completed = BashRunner().run("false", str(tmp_path), 30, None)
        assert completed.exit_code != 0
        assert completed.timed_out is False

    def test_echo_output_stdout_stderr_merged(self, tmp_path: Any) -> None:
        completed = BashRunner().run("echo out; echo err >&2", str(tmp_path), 30, None)
        assert completed.exit_code == 0
        assert "out" in completed.output
        assert "err" in completed.output

    def test_runs_in_given_cwd(self, tmp_path: Any) -> None:
        completed = BashRunner().run("pwd", str(tmp_path), 30, None)
        assert completed.exit_code == 0
        assert completed.output.strip() == str(tmp_path)

    def test_timeout_kills_process_group_and_marks_timed_out(self, tmp_path: Any) -> None:
        start = time.monotonic()
        completed = BashRunner().run("sleep 30; echo never", str(tmp_path), 1, None)
        elapsed = time.monotonic() - start
        assert completed.timed_out is True
        assert completed.exit_code != 0
        assert completed.exit_code < 0 or completed.exit_code == 124
        assert "never" not in completed.output
        # The whole group died at the timeout: no zombie child held the pipe.
        assert elapsed < 15

    def test_timeout_kills_bash_children_too(self, tmp_path: Any) -> None:
        marker = tmp_path / "child_done"
        completed = BashRunner().run(
            f"sleep 30 && touch {marker} & sleep 60", str(tmp_path), 1, None
        )
        assert completed.timed_out is True
        time.sleep(0.3)
        assert not marker.exists()

    def test_env_replaces_environment(self, tmp_path: Any) -> None:
        # bash -lc is a login shell: profile files may add stderr noise that
        # merges into the output; only the probe variable itself is asserted.
        completed = BashRunner().run(
            'printf "%s" "$ACCEPTANCE_ENV_PROBE"', str(tmp_path), 30, {"ACCEPTANCE_ENV_PROBE": "on"}
        )
        assert completed.exit_code == 0
        assert completed.output.endswith("on")

    def test_env_none_inherits_parent(self, tmp_path: Any) -> None:
        completed = BashRunner().run('printf "%s" "$HOME"', str(tmp_path), 30, None)
        assert completed.exit_code == 0
        assert completed.output != ""


class TestBashRunnerIntegration:
    def test_fail_fast_with_real_runner(self, tmp_path: Any) -> None:
        run = run_acceptance(
            ["true", "false", "echo unreachable"],
            cwd=str(tmp_path),
            runner=BashRunner(),
            timeout_s=30,
        )
        assert run.passed is False
        assert run.failed_cmd == "false"
        assert [r.cmd for r in run.results] == ["true", "false"]
        assert acceptance_results(run)[1]["exit"] == 1

    def test_all_green_with_real_runner(self, tmp_path: Any) -> None:
        run = run_acceptance(
            ["true", "echo ok"], cwd=str(tmp_path), runner=BashRunner(), timeout_s=30
        )
        assert run.passed is True
        assert run.failed_cmd is None
        assert acceptance_results(run) == [
            {"cmd": "true", "exit": 0, "tail": ""},
            {"cmd": "echo ok", "exit": 0, "tail": "ok\n"},
        ]

    def test_timeout_in_batch_fails_fast(self, tmp_path: Any) -> None:
        run = run_acceptance(
            ["echo first", "sleep 30", "echo last"],
            cwd=str(tmp_path),
            runner=BashRunner(),
            timeout_s=1,
        )
        assert run.passed is False
        assert run.failed_cmd == "sleep 30"
        assert [r.cmd for r in run.results] == ["echo first", "sleep 30"]
        assert run.results[1].timed_out is True
        assert run.results[1].duration_s < 15
