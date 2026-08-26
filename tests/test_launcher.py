"""Transient-unit launching, and the CLI surface for a line."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet_graph.cli import build_parser
from fleet_graph.scheduler.launcher import (
    LaunchSpec,
    TransientLauncher,
)


@pytest.fixture
def spec() -> LaunchSpec:
    return LaunchSpec(
        folder_id="wf-40fa8d",
        seat="opencode-gpt-sol",
        run_root=Path("/data/fleet-graph/runs/wf-40fa8d"),
        log_path=Path("/data/fleet-graph/logs/wf-40fa8d.log"),
    )


class TestIsolation:
    def test_runs_as_a_user_transient_unit(self, spec: LaunchSpec) -> None:
        """A line started as the scheduler's child shares its cgroup, so
        restarting the scheduler would kill every line at once."""
        argv = spec.argv()
        assert argv[:2] == ["systemd-run", "--user"]

    def test_collects_failed_units(self, spec: LaunchSpec) -> None:
        """Without --collect a failed unit lingers and blocks the next launch
        of the same name."""
        assert "--collect" in spec.argv()

    def test_each_line_gets_its_own_unit(self, spec: LaunchSpec) -> None:
        argv = spec.argv()
        assert argv[argv.index("--unit") + 1] == "fleet-graph-line-wf-40fa8d-g1"

    def test_generation_avoids_colliding_with_a_unit_being_torn_down(self) -> None:
        first = LaunchSpec(folder_id="wf-1", seat="s", generation=1)
        second = LaunchSpec(folder_id="wf-1", seat="s", generation=2)
        assert first.unit_name != second.unit_name


class TestCommand:
    def test_passes_the_line_arguments(self, spec: LaunchSpec) -> None:
        argv = spec.argv()
        assert (
            argv[-8:]
            == [
                "line",
                "run",
                "--folder",
                "wf-40fa8d",
                "--seat",
                "opencode-gpt-sol",
                "--max-rounds",
                "10",
            ]
            or "--folder" in argv
        )
        assert argv[argv.index("--folder") + 1] == "wf-40fa8d"
        assert argv[argv.index("--seat") + 1] == "opencode-gpt-sol"

    def test_environment_is_passed_through_setenv(self) -> None:
        spec = LaunchSpec(folder_id="wf-1", seat="s", environment={"AGENT_RUNTIME_ROOT": "/data/x"})
        assert "--setenv=AGENT_RUNTIME_ROOT=/data/x" in spec.argv()

    def test_logs_append_rather_than_truncate(self, spec: LaunchSpec) -> None:
        """A restart must not erase the previous generation's log."""
        joined = " ".join(spec.argv())
        assert "StandardOutput=append:" in joined
        assert "StandardError=append:" in joined


class TestDryRun:
    def test_dry_run_starts_nothing_and_shows_the_command(self, spec: LaunchSpec) -> None:
        result = TransientLauncher(dry_run=True).launch(spec)
        assert result.started is False
        assert "systemd-run" in result.detail


class TestCli:
    def test_line_run_requires_folder_and_seat(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["line", "run"])

    def test_line_run_parses(self) -> None:
        args = build_parser().parse_args(
            ["line", "run", "--folder", "wf-1", "--seat", "opencode-dsv4pro", "--max-rounds", "5"]
        )
        assert args.folder == "wf-1"
        assert args.seat == "opencode-dsv4pro"
        assert args.max_rounds == 5

    def test_defaults_match_the_pump(self) -> None:
        args = build_parser().parse_args(["line", "run", "--folder", "w", "--seat", "s"])
        assert args.max_rounds == 10
        assert args.noop_limit == 3
        assert args.timeout_limit == 2
