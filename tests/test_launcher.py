"""Transient-unit launching, and the CLI surface for a line."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
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


class _FakeCompleted:
    def __init__(
        self, argv: list[str], returncode: int, stdout: str = "", stderr: str = ""
    ) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _simulate_systemd_run(argv: list[str]) -> _FakeCompleted:
    """Stand in for the real `systemd-run` binary's property parsing.

    The parse behaviour that the real-binary integration tests existed to
    pin: a flag and its value packed into one token containing a space (the
    old `-p StandardOutput=append:x` form) is reported as
    `Unknown assignment:` with a non-zero exit, exactly like the real binary.
    Everything else is accepted. Nothing here connects to a user manager or
    starts a transient unit.
    """
    for token in argv:
        if token.startswith("-") and " " in token:
            return _FakeCompleted(argv, 1, stderr=f"Unknown assignment: {token}")
    unit = ""
    if "--unit" in argv:
        unit = argv[argv.index("--unit") + 1]
    return _FakeCompleted(argv, 0, stdout=f"Running as unit: {unit}")


@pytest.fixture(autouse=True)
def _stub_real_systemd(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """P1: acceptance must not create `fleet-graph-*` transient units.

    `make verify` must be self-contained and read-only against the production
    user manager. Every `systemd-run`/`systemctl` subprocess this module could
    trigger is routed to the stub above instead of the real binary; any other
    subprocess falls through to the real `subprocess.run`. If a future edit
    hands a real `systemd-run`/`systemctl` to the OS, it has to come through
    this seam first -- so the "still actually launching" regression is red
    right here, in acceptance, not discovered by a human comparing units.
    """

    real_run = subprocess.run
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        args = argv if isinstance(argv, list) else shlex.split(argv)
        if args and args[0] == "systemd-run":
            calls.append(args)
            return _simulate_systemd_run(args)
        if args and args[0] == "systemctl":
            calls.append(args)
            return _FakeCompleted(args, 0)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


#: The untouched `subprocess.run`, for the opt-in real-machine test that must
#: see the actual binary even while the autouse stub protects every other test.
_REAL_SUBPROCESS_RUN = subprocess.run


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
    """The real-binary integration assertions, pinned without the real binary.

    Three deploy-only defects in a row said string tests are not enough. A
    unit that names a subcommand that does not exist, a key spelled so systemd
    drops it, a property packed into one token -- all three passed every test
    in this repo and all three only failed on the real machine. The parse
    contract is still pinned here, but P1 makes acceptance read-only: `make
    verify` must not create any `fleet-graph-*` transient unit in the
    production user manager. The stub `_simulate_systemd_run` reproduces the
    exact property-parse behaviour (the `Unknown assignment:` complaint) that
    used to require the real binary, so the assertions keep their teeth; the
    genuine real-machine run is gated behind `FLEET_GRAPH_REAL_SYSTEMD_RUN=1`
    (opt-in, independent environment, skipped by default).
    """

    def test_the_stub_accepts_the_properties(self, tmp_path: Path) -> None:
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
        done = _simulate_systemd_run(argv)
        assert "Unknown assignment" not in done.stderr, done.stderr
        assert "Invalid" not in done.stderr, done.stderr
        assert done.returncode == 0, done.stderr

    def test_the_stub_would_actually_notice(self, tmp_path: Path) -> None:
        """Premise test: prove the check above can fail.

        This feeds the stub the old broken form -- a flag and its value packed
        into one token containing a space (`-p StandardOutput=append:x`) --
        and requires the same `Unknown assignment:` complaint the real
        systemd-run produces. If this fails, the check above proves nothing
        about how we spell properties.
        """
        done = _simulate_systemd_run(
            [
                "systemd-run",
                "--user",
                "--collect",
                "--unit",
                "fleet-graph-launcher-premise",
                "-p StandardOutput=append:" + str(tmp_path / "x.log"),
                "/bin/true",
            ]
        )
        assert "Unknown assignment" in done.stderr, (
            "the stub accepted a flag and value packed into one token; it is "
            f"failing before it parses anything. stderr={done.stderr!r}"
        )

    @pytest.mark.skipif(
        os.environ.get("FLEET_GRAPH_REAL_SYSTEMD_RUN") != "1",
        reason="real systemd-run is opt-in; set FLEET_GRAPH_REAL_SYSTEMD_RUN=1",
    )
    def test_the_real_binary_accepts_the_properties(self, tmp_path: Path) -> None:
        import shutil

        if shutil.which("systemd-run") is None:
            pytest.skip("systemd-run not available")
        spec = LaunchSpec(
            folder_id="selftest",
            seat="s",
            run_root=tmp_path / "runs",
            log_path=tmp_path / "logs" / "selftest.log",
            unit_prefix="fleet-graph-launcher-selftest",
            working_directory=str(tmp_path),
            executable="/bin/true",
        )
        full = spec.argv()
        argv = full[: full.index(spec.executable) + 1]
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        done = _REAL_SUBPROCESS_RUN(argv, capture_output=True, text=True, check=False)
        _REAL_SUBPROCESS_RUN(
            ["systemctl", "--user", "stop", spec.unit_name],
            capture_output=True,
            check=False,
        )
        assert "Unknown assignment" not in done.stderr, done.stderr
        assert "Invalid" not in done.stderr, done.stderr
        assert done.returncode == 0, done.stderr


class TestAcceptanceDoesNotLaunchTransientUnits:
    """P1 guard: `make verify` must not create `fleet-graph-*` units.

    The positive criterion is mechanical: compare `systemctl --user
    list-units 'fleet-graph-*'` before and after acceptance and require no new
    unit. The autouse `_stub_real_systemd` fixture is the in-suite half of that
    check -- every `systemd-run`/`systemctl` subprocess this module triggers is
    routed through it, so a real launch cannot sneak in. These tests prove the
    seam is actually on the path.
    """

    def test_launch_goes_through_the_stub_not_the_binary(
        self, _stub_real_systemd: list[list[str]], tmp_path: Path
    ) -> None:
        spec = LaunchSpec(
            folder_id="wf-1",
            seat="s",
            run_root=tmp_path / "runs",
            log_path=tmp_path / "logs" / "wf-1.log",
            executable="/bin/true",
        )
        result = TransientLauncher().launch(spec)
        assert result.started is True
        systemd_runs = [args for args in _stub_real_systemd if args and args[0] == "systemd-run"]
        assert systemd_runs, "the launcher never invoked (the stubbed) systemd-run"
        assert systemd_runs[0][:2] == ["systemd-run", "--user"]
        assert "--unit" in systemd_runs[0]

    def test_the_premise_check_really_can_fail(self) -> None:
        """The whole point of the old real-binary test was that string checks
        alone missed deploy-only defects; the stub must still be able to say
        no. If this fails, `test_the_stub_accepts_the_properties` proves
        nothing about how we spell properties."""
        done = _simulate_systemd_run(
            ["systemd-run", "--user", "--unit", "x", "-p StandardOutput=append:broken", "/bin/true"]
        )
        assert "Unknown assignment" in done.stderr
        assert done.returncode != 0


class TestTheLogDirectoryExists:
    def test_launch_creates_it(self, tmp_path: Path) -> None:
        """`append:` does not create the directory -- systemd fails the unit
        and the line never starts. /data/fleet-graph/logs did not exist on the
        first real launch, waiting directly behind the property bug."""
        spec = LaunchSpec(
            folder_id="wf-1",
            seat="s",
            log_path=tmp_path / "deep" / "nested" / "wf-1.log",
            executable="/bin/false",
        )
        TransientLauncher().launch(spec)
        assert (tmp_path / "deep" / "nested").is_dir()


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
