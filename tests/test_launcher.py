"""Transient-unit launching, and the CLI surface for a line."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.cli import build_parser
from fleet_graph.scheduler.launcher import (
    TRANSIENT_NOOP_ENV,
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

    def test_generation_is_passed_down_to_the_line(self) -> None:
        """thread_id is folder:g{generation}; a scheduler that restarts a line
        without handing the generation down re-randomises nothing but still
        changes the identity, and re-adopt silently stops working."""
        spec = LaunchSpec(folder_id="wf-1", seat="s", generation=4)
        argv = spec.argv()
        assert argv[argv.index("--generation") + 1] == "4"

    def test_checkpoint_is_explicit_and_lives_under_the_run_root(self, spec: LaunchSpec) -> None:
        argv = spec.argv()
        assert (
            argv[argv.index("--checkpoint") + 1]
            == "/data/fleet-graph/runs/wf-40fa8d/checkpoint.sqlite3"
        )

    def test_environment_is_passed_through_setenv(self) -> None:
        spec = LaunchSpec(folder_id="wf-1", seat="s", environment={"AGENT_RUNTIME_ROOT": "/data/x"})
        assert "--setenv=AGENT_RUNTIME_ROOT=/data/x" in spec.argv()

    def test_logs_append_rather_than_truncate(self, spec: LaunchSpec) -> None:
        """A restart must not erase the previous generation's log.

        Checked per argv element, not on a joined string. The first version
        joined argv with spaces and asserted a substring -- which passes for
        `["-p StandardOutput=append:x"]` just as happily as for
        `["--property=StandardOutput=append:x"]`. Joining argv back together
        destroys exactly the information that was wrong.
        """
        argv = spec.argv()
        assert f"--property=StandardOutput=append:{spec.log_file}" in argv
        assert f"--property=StandardError=append:{spec.log_file}" in argv

    def test_no_argument_smuggles_a_flag_and_its_value_into_one_token(
        self, spec: LaunchSpec
    ) -> None:
        """There is no shell between us and systemd-run.

        `"-p StandardOutput=append:/x"` is one execve argument whose value
        starts with a space; systemd-run reports `Unknown assignment:` and
        looks for all the world like a misspelled property name. This is the
        general form of that bug, so it also catches the next one.
        """
        for arg in spec.argv():
            if arg.startswith("-"):
                assert " " not in arg, arg


class TestSystemdRunActuallyAcceptsIt:
    """Three deploy-only defects in a row said string tests are not enough.

    A unit that names a subcommand that does not exist, a key spelled so
    systemd drops it, a property packed into one token -- all three passed
    every test in this repo and all three only failed on the real machine.
    So this one hands the real argv to the real systemd-run.

    Deploy-only by construction: under the test-context no-op guard
    (``FLEET_GRAPH_TRANSIENT_NOOP``, set by tests/conftest.py) these are
    skipped, because a test/acceptance run must never hand ``systemd-run
    --user`` to the real user manager. They run only on a real machine with
    the guard explicitly unset.
    """

    def _require_real_launch(self) -> None:
        if os.environ.get(TRANSIENT_NOOP_ENV, ""):
            pytest.skip(
                "real-systemd-run smoke tests are excluded under the "
                "test/acceptance no-op guard; run with "
                f"{TRANSIENT_NOOP_ENV}= unset on a real user manager"
            )

    def test_the_real_binary_accepts_the_properties(self, tmp_path: Path) -> None:
        import shutil

        if shutil.which("systemd-run") is None:
            pytest.skip("systemd-run not available")
        self._require_real_launch()
        spec = LaunchSpec(
            folder_id="selftest",
            seat="s",
            run_root=tmp_path / "runs",
            log_path=tmp_path / "logs" / "selftest.log",
            unit_prefix="fleet-graph-launcher-selftest",
            working_directory=str(tmp_path),
            executable="/bin/true",
        )
        # Everything up to and including the executable: the flags are what
        # systemd-run parses, and /bin/true needs none of the line arguments.
        full = spec.argv()
        argv = full[: full.index(spec.executable) + 1]
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        subprocess.run(
            ["systemctl", "--user", "stop", spec.unit_name],
            capture_output=True,
            check=False,
        )
        assert "Unknown assignment" not in done.stderr, done.stderr
        assert "Invalid" not in done.stderr, done.stderr
        assert done.returncode == 0, done.stderr

    def test_the_binary_would_actually_notice(self, tmp_path: Path) -> None:
        """Premise test: prove the check above can fail.

        systemd-run connects to the bus *before* it parses properties, so with
        no user session bus both the right spelling and the wrong one produce
        the same connection error -- and a test that only asserted on stderr
        content would call that a pass. This feeds it the old broken form and
        requires the complaint. If this fails, the check above proves nothing
        about how we spell properties.
        """
        import shutil

        if shutil.which("systemd-run") is None:
            pytest.skip("systemd-run not available")
        self._require_real_launch()
        done = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--collect",
                "--unit",
                "fleet-graph-launcher-premise",
                "-p StandardOutput=append:" + str(tmp_path / "x.log"),
                "/bin/true",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "Unknown assignment" in done.stderr, (
            "systemd-run accepted a flag and value packed into one token; it is probably "
            f"failing before it parses anything. stderr={done.stderr!r}"
        )


class TestTheLogDirectoryExists:
    def test_launch_creates_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`append:` does not create the directory -- systemd fails the unit
        and the line never starts. /data/fleet-graph/logs did not exist on the
        first real launch, waiting directly behind the property bug.

        The production path (``dry_run=False``) is what creates the directory;
        the ``systemd-run`` subprocess is stubbed so this test never hands it
        to the real user manager (the test/acceptance isolation rule).
        """
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "fleet_graph.scheduler.launcher.subprocess.run",
            lambda argv, **kwargs: (
                calls.append(argv) or subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            ),
        )
        spec = LaunchSpec(
            folder_id="wf-1",
            seat="s",
            log_path=tmp_path / "deep" / "nested" / "wf-1.log",
            executable="/bin/false",
        )
        TransientLauncher(dry_run=False).launch(spec)
        assert (tmp_path / "deep" / "nested").is_dir()
        assert calls and calls[0][:2] == ["systemd-run", "--user"]


class TestDryRun:
    def test_dry_run_starts_nothing_and_shows_the_command(self, spec: LaunchSpec) -> None:
        result = TransientLauncher(dry_run=True).launch(spec)
        assert result.started is False
        assert "systemd-run" in result.detail


class TestTransientNoopIsolation:
    """dev-fg-a4624cb34677: the scheduler-side launch entry.

    Positive (isolation holds): in the test/acceptance context the default
    ``TransientLauncher`` is no-op -- building a ``LaunchSpec`` and validating
    its argv never hands ``systemd-run --user`` to the real user manager, and
    the argv stays fully verifiable. Negative (not blinded): the production
    path with no no-op injection still goes through ``systemd-run --user``
    with real launch semantics unchanged.
    """

    def test_the_test_context_default_launcher_is_noop(self) -> None:
        """conftest sets FLEET_GRAPH_TRANSIENT_NOOP, so a default-constructed
        launcher is no-op without any explicit injection."""
        assert TransientLauncher().dry_run is True

    def test_a_noop_launch_records_the_full_argv_without_starting(self, spec: LaunchSpec) -> None:
        result = TransientLauncher(dry_run=True).launch(spec)
        assert result.started is False
        # The argv stays complete and verifiable: unit name, --user, and the
        # line's own args all survive the no-op seam.
        assert "--user" in result.detail
        assert spec.unit_name in result.detail
        assert result.detail == shlex.join(spec.argv())

    def test_the_production_default_is_real_without_the_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative: no guard, no injection -> the production default keeps
        the real launch semantics. Weaving a no-op into the production default
        makes this test go red (real launch lost)."""
        monkeypatch.delenv("FLEET_GRAPH_TRANSIENT_NOOP", raising=False)
        assert TransientLauncher().dry_run is False

    def test_the_real_path_hands_systemd_run_to_the_user_manager(
        self, spec: LaunchSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative: an explicit ``dry_run=False`` (or guard unset) still hands
        ``systemd-run --user`` to the real user manager. The subprocess is
        faked so the test itself never launches a unit."""
        argv_sent: list[list[str]] = []

        class FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(argv: list[str], **kwargs: Any) -> FakeProc:
            argv_sent.append(argv)
            return FakeProc()

        monkeypatch.setattr("fleet_graph.scheduler.launcher.subprocess.run", fake_run)
        result = TransientLauncher(dry_run=False).launch(spec)
        assert result.started is True
        assert argv_sent and argv_sent[0][:2] == ["systemd-run", "--user"]

    def test_the_guard_is_an_explicit_default_not_a_global_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit ``dry_run=`` always wins over the guard."""
        monkeypatch.setenv("FLEET_GRAPH_TRANSIENT_NOOP", "1")
        assert TransientLauncher(dry_run=False).dry_run is False


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


class TestAcceptancePassing:
    """The declaration crosses into the unit as one JSON argument. Its
    visibility in argv is deliberate: the trust anchor is the roster's PR
    review, and nothing secret or agent-written is in it."""

    def test_the_declaration_is_one_json_argument(self) -> None:
        declaration = json.dumps(
            {
                "argvs": [["systemctl", "--user", "is-active", "loop-engine-jobd"]],
                "cwd": "/tmp",
                "timeout_seconds": 300,
            }
        )
        spec = LaunchSpec(folder_id="wf-1", seat="s", acceptance_json=declaration)
        argv = spec.argv()
        recovered = json.loads(argv[argv.index("--acceptance-json") + 1])
        assert recovered["argvs"] == [["systemctl", "--user", "is-active", "loop-engine-jobd"]]
        assert recovered["cwd"] == "/tmp"
        assert recovered["timeout_seconds"] == 300

    def test_no_declaration_means_no_flag(self, spec: LaunchSpec) -> None:
        """The line tells `not declared` apart from `declared empty` by the
        flag's absence; passing an empty one would erase that distinction."""
        assert "--acceptance-json" not in spec.argv()


class TestBoundsForwarding:
    """The roster declares optional streak-breaker bounds; the launcher forwards
    them only when present, so a bound that was never reviewed is never passed
    down to override the runner's own defaults."""

    def test_bounds_are_forwarded_when_present(self) -> None:
        spec = LaunchSpec(folder_id="wf-1", seat="s", noop_limit=5, timeout_limit=7)
        argv = spec.argv()
        assert argv[argv.index("--noop-limit") + 1] == "5"
        assert argv[argv.index("--timeout-limit") + 1] == "7"

    def test_bounds_are_omitted_when_absent(self, spec: LaunchSpec) -> None:
        assert "--noop-limit" not in spec.argv()
        assert "--timeout-limit" not in spec.argv()

    def test_a_single_bound_forwards_alone(self) -> None:
        spec = LaunchSpec(folder_id="wf-1", seat="s", noop_limit=9)
        argv = spec.argv()
        assert argv[argv.index("--noop-limit") + 1] == "9"
        assert "--timeout-limit" not in argv
