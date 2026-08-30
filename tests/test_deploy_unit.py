"""The shipped systemd unit, checked against the CLI it claims to run.

The P0 skeleton named `fleet-graph serve`, a subcommand that has never
existed. With `Restart=always` and `RestartSec=5` that is not a visible
failure -- it is a crash loop, quietly, forever. A unit file no test reads is
a unit file that is only validated in production.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from fleet_graph.cli import build_parser

UNIT = Path(__file__).resolve().parent.parent / "deploy" / "systemd" / "fleet-graphd.service"
DD_MCP_UNIT = (
    Path(__file__).resolve().parent.parent / "deploy" / "systemd" / "fleet-graph-dd-mcp.service"
)
ARBITER_UNIT = (
    Path(__file__).resolve().parent.parent / "deploy" / "systemd" / "fleet-graph-arbiter.service"
)
STATE_UNIT = (
    Path(__file__).resolve().parent.parent / "deploy" / "systemd" / "fleet-graph-state.service"
)
ARBITER_TIMER = (
    Path(__file__).resolve().parent.parent / "deploy" / "systemd" / "fleet-graph-arbiter.timer"
)


def exec_start(text: str) -> list[str]:
    """The ExecStart argv, line continuations joined."""
    joined = text.replace("\\\n", " ")
    for line in joined.splitlines():
        if line.startswith("ExecStart="):
            return line[len("ExecStart=") :].split()
    raise AssertionError("the unit has no ExecStart")


class TestTheUnitRunsSomethingThatExists:
    def test_the_subcommand_is_one_the_cli_accepts(self) -> None:
        argv = exec_start(UNIT.read_text(encoding="utf-8"))
        assert argv[0].endswith("fleet-graph"), argv[0]
        # Parsing is the check: an invented subcommand exits non-zero here,
        # which is exactly what production would have discovered instead.
        parsed = build_parser().parse_args(argv[1:])
        assert parsed.func is not None

    def test_it_is_not_the_skeleton_command(self) -> None:
        """Assert on the argv, not on the file text: the comment above
        ExecStart names the broken command on purpose, and a whole-file scan
        matches that -- the same "name match is not semantics" trap this file
        exists to catch."""
        argv = exec_start(UNIT.read_text(encoding="utf-8"))
        assert "serve" not in argv[1:2], argv


class TestTheUnitCannotFailForUnreadableReasons:
    def test_a_missing_env_file_is_tolerated(self) -> None:
        """The optional marker belongs after the `=`.

        The first version of this test asserted `^-EnvironmentFile=` and so
        pinned the broken spelling in place: systemd reads that as an unknown
        key, drops the line, starts the unit anyway, and the daemon runs with
        no credentials. A regex can only check the grammar I believed in.
        `test_systemd_itself_accepts_every_key` below asks systemd instead.
        """
        text = UNIT.read_text(encoding="utf-8")
        assert re.search(r"^EnvironmentFile=-", text, re.MULTILINE), (
            "EnvironmentFile must be optional; the scheduler reports its own missing credentials"
        )
        assert not re.search(r"^-\w+=", text, re.MULTILINE), (
            "a leading `-` on a key is not systemd syntax; the whole line is dropped"
        )

    def test_systemd_itself_accepts_every_key(self) -> None:
        """Ask the parser, not the author.

        `systemd-analyze verify` exits 0 even when it drops keys it does not
        recognise, so the exit code proves nothing -- the warning text is the
        finding. This catches any misspelled directive in the unit, not just
        the one that already bit us.
        """
        analyze = shutil.which("systemd-analyze")
        if analyze is None:
            pytest.skip("systemd-analyze not available")
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / UNIT.name
            staged.write_text(UNIT.read_text(encoding="utf-8"), encoding="utf-8")
            done = subprocess.run(
                [analyze, "--user", "verify", str(staged)],
                capture_output=True,
                text=True,
                check=False,
            )
        noise = done.stderr + done.stdout
        assert "Unknown key" not in noise, noise

    def test_the_verifier_would_actually_notice(self, tmp_path: Path) -> None:
        """Premise test: prove the check above can fail.

        `systemd-analyze verify` exits 0 when it cannot reach a user manager
        at all, printing "Failed to initialize manager" and parsing nothing --
        so the assertion above passes without having looked. That is worse
        than a hard failure: it is a green light from a check that never ran.
        This feeds it a unit with a deliberately misspelled key and requires
        the complaint. If this test fails, the one above proves nothing.
        """
        analyze = shutil.which("systemd-analyze")
        if analyze is None:
            pytest.skip("systemd-analyze not available")
        broken = tmp_path / "broken.service"
        broken.write_text(
            "[Service]\nType=simple\nExecStart=/bin/true\n-EnvironmentFile=/tmp/nope\n",
            encoding="utf-8",
        )
        done = subprocess.run(
            [analyze, "--user", "verify", str(broken)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "Unknown key" in done.stderr + done.stdout, (
            "systemd-analyze did not flag a key it should not recognise; it is probably "
            "running without a user manager and parsing nothing"
        )

    def test_no_credential_is_baked_into_the_unit(self) -> None:
        """Credentials are env-only (golden rule 3). A token in a unit file is
        a token in git."""
        for line in UNIT.read_text(encoding="utf-8").splitlines():
            if line.startswith("Environment="):
                assert "TOKEN" not in line.upper(), line
                assert "KEY" not in line.upper(), line


class TestTheUnitGivesLinesAWorkingPath:
    def test_bun_is_on_the_path_the_unit_defines(self) -> None:
        """agent-run's shebang is `#!/usr/bin/env bun`. A systemd user unit
        does not inherit a login shell's PATH, so without this every line dies
        with `env: 'bun': No such file or directory` before doing any work."""
        path_lines = [
            line
            for line in UNIT.read_text(encoding="utf-8").splitlines()
            if line.startswith("Environment=PATH=")
        ]
        assert path_lines, "the unit must define PATH; the default has no bun"
        assert ".bun/bin" in path_lines[0], path_lines[0]
        assert "/usr/bin" in path_lines[0], "PATH must still carry the system binaries"

    def test_the_path_carries_what_the_old_pump_carried(self) -> None:
        """Migration equivalence, fifth instance. Every migrated seat is named
        `opencode-*`, and the `opencode` binary lives in `~/.opencode/bin`
        (symlinked from `~/.local/bin`) -- neither of which was on our PATH.
        The canary happened not to need it; that is luck, not equivalence."""
        path_line = next(
            line
            for line in UNIT.read_text(encoding="utf-8").splitlines()
            if line.startswith("Environment=PATH=")
        )
        for wanted in (".bun/bin", ".local/bin", ".opencode/bin", ".cargo/bin"):
            assert wanted in path_line, f"{wanted} missing from {path_line}"


class TestTheRestartPolicyMatchesTheReAdoptDesign:
    def test_kill_mode_lets_executors_outlive_the_daemon(self) -> None:
        """The whole point of the re-adopt primitive: killing the daemon must
        not kill the agent runs it is supervising."""
        assert "KillMode=process" in UNIT.read_text(encoding="utf-8")


class TestTheDdMcpUnitRunsSomethingThatExists:
    """Same discipline as fleet-graphd.service, applied to the shipped (but
    deliberately not enabled) dev-dispatch MCP unit template."""

    def test_the_subcommand_is_one_the_cli_accepts(self) -> None:
        argv = exec_start(DD_MCP_UNIT.read_text(encoding="utf-8"))
        assert argv[0].endswith("fleet-graph"), argv[0]
        parsed = build_parser().parse_args(argv[1:])
        assert parsed.func is not None

    def test_it_serves_the_agreed_loopback_port(self) -> None:
        argv = exec_start(DD_MCP_UNIT.read_text(encoding="utf-8"))
        assert argv[1:3] == ["dd", "serve"], argv
        assert "--port" in argv and argv[argv.index("--port") + 1] == "5610", argv
        assert "--host" in argv and argv[argv.index("--host") + 1] == "127.0.0.1", argv

    def test_it_restarts_and_runs_from_the_current_snapshot(self) -> None:
        text = DD_MCP_UNIT.read_text(encoding="utf-8")
        assert "Restart=always" in text
        assert "WorkingDirectory=/data/apps/fleet-graph/current" in text
        argv = exec_start(text)
        assert argv[0].startswith("/data/apps/fleet-graph/current/"), argv[0]

    def test_a_missing_env_file_is_tolerated(self) -> None:
        text = DD_MCP_UNIT.read_text(encoding="utf-8")
        assert re.search(r"^EnvironmentFile=-", text, re.MULTILINE)
        assert not re.search(r"^-\w+=", text, re.MULTILINE)

    def test_no_credential_is_baked_into_the_unit(self) -> None:
        for line in DD_MCP_UNIT.read_text(encoding="utf-8").splitlines():
            if line.startswith("Environment="):
                assert "TOKEN" not in line.upper(), line
                assert "KEY" not in line.upper(), line

    def test_systemd_itself_accepts_every_key(self) -> None:
        analyze = shutil.which("systemd-analyze")
        if analyze is None:
            pytest.skip("systemd-analyze not available")
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / DD_MCP_UNIT.name
            staged.write_text(DD_MCP_UNIT.read_text(encoding="utf-8"), encoding="utf-8")
            done = subprocess.run(
                [analyze, "--user", "verify", str(staged)],
                capture_output=True,
                text=True,
                check=False,
            )
        noise = done.stderr + done.stdout
        assert "Unknown key" not in noise, noise


class TestTheStateUnitRunsSomethingThatExists:
    """The M1 fleet-state read-model unit, checked against the CLI it claims to
    run. Same discipline as fleet-graphd.service: an invented subcommand plus
    Restart=always would be a crash loop in the dark."""

    def test_the_subcommand_is_one_the_cli_accepts(self) -> None:
        argv = exec_start(STATE_UNIT.read_text(encoding="utf-8"))
        assert argv[0].endswith("fleet-graph"), argv[0]
        parsed = build_parser().parse_args(argv[1:])
        assert parsed.func is not None

    def test_it_serves_the_agreed_loopback_port(self) -> None:
        argv = exec_start(STATE_UNIT.read_text(encoding="utf-8"))
        assert argv[1:3] == ["state", "serve"], argv
        assert "--port" in argv and argv[argv.index("--port") + 1] == "7494", argv
        assert "--host" in argv and argv[argv.index("--host") + 1] == "127.0.0.1", argv

    def test_it_restarts_and_runs_from_the_current_snapshot(self) -> None:
        text = STATE_UNIT.read_text(encoding="utf-8")
        assert "Restart=always" in text
        assert "WorkingDirectory=/data/apps/fleet-graph/current" in text
        argv = exec_start(text)
        assert argv[0].startswith("/data/apps/fleet-graph/current/"), argv[0]

    def test_a_missing_env_file_is_tolerated(self) -> None:
        text = STATE_UNIT.read_text(encoding="utf-8")
        assert re.search(r"^EnvironmentFile=-", text, re.MULTILINE)
        assert not re.search(r"^-\w+=", text, re.MULTILINE)

    def test_no_credential_is_baked_into_the_unit(self) -> None:
        for line in STATE_UNIT.read_text(encoding="utf-8").splitlines():
            if line.startswith("Environment="):
                assert "TOKEN" not in line.upper(), line
                assert "KEY" not in line.upper(), line

    def test_systemd_itself_accepts_every_key(self) -> None:
        analyze = shutil.which("systemd-analyze")
        if analyze is None:
            pytest.skip("systemd-analyze not available")
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / STATE_UNIT.name
            staged.write_text(STATE_UNIT.read_text(encoding="utf-8"), encoding="utf-8")
            done = subprocess.run(
                [analyze, "--user", "verify", str(staged)],
                capture_output=True,
                text=True,
                check=False,
            )
        noise = done.stderr + done.stdout
        assert "Unknown key" not in noise, noise


class TestTheArbiterUnitRunsSomethingThatExists:
    """The A2 arbiter oneshot unit, checked against the CLI it claims to run.

    Same discipline as fleet-graphd.service: an invented subcommand plus
    Type=oneshot is a silent failure every timer firing, not a visible one.
    """

    def test_the_subcommand_is_one_the_cli_accepts(self) -> None:
        argv = exec_start(ARBITER_UNIT.read_text(encoding="utf-8"))
        assert argv[0].endswith("fleet-graph"), argv[0]
        parsed = build_parser().parse_args(argv[1:])
        assert parsed.func is not None

    def test_the_exact_managed_command(self) -> None:
        argv = exec_start(ARBITER_UNIT.read_text(encoding="utf-8"))
        assert argv == [
            "/data/apps/fleet-graph/current/.venv/bin/fleet-graph",
            "arbiter",
            "run",
            "--publish",
            "--alias",
            "arbiter",
        ], argv

    def test_it_is_oneshot_with_no_restart_loop(self) -> None:
        text = ARBITER_UNIT.read_text(encoding="utf-8")
        assert re.search(r"^Type=oneshot$", text, re.MULTILINE)
        assert not re.search(r"^Restart=", text, re.MULTILINE), (
            "A oneshot arbiter tick must not respawn itself"
        )

    def test_the_mandatory_dedicated_environment_file(self) -> None:
        text = ARBITER_UNIT.read_text(encoding="utf-8")
        assert re.search(
            r"^EnvironmentFile=%h/\.config/fleet-graph/arbiter\.env$", text, re.MULTILINE
        ), "the arbiter's EnvironmentFile must be mandatory (no optional `-`) and dedicated"
        assert not re.search(r"^EnvironmentFile=-", text, re.MULTILINE)

    def test_no_credential_is_baked_into_the_unit(self) -> None:
        for line in ARBITER_UNIT.read_text(encoding="utf-8").splitlines():
            if line.startswith("Environment"):
                assert "TOKEN" not in line.upper(), line
                assert "KEY" not in line.upper(), line
        assert "DECISION_TOKEN" not in ARBITER_UNIT.read_text(encoding="utf-8")
        assert "arbiter.env" in ARBITER_UNIT.read_text(encoding="utf-8")

    def test_it_runs_from_the_current_snapshot(self) -> None:
        argv = exec_start(ARBITER_UNIT.read_text(encoding="utf-8"))
        assert argv[0].startswith("/data/apps/fleet-graph/current/"), argv[0]

    def test_systemd_itself_accepts_every_key(self) -> None:
        analyze = shutil.which("systemd-analyze")
        if analyze is None:
            pytest.skip("systemd-analyze not available")
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / ARBITER_UNIT.name
            staged.write_text(ARBITER_UNIT.read_text(encoding="utf-8"), encoding="utf-8")
            done = subprocess.run(
                [analyze, "--user", "verify", str(staged)],
                capture_output=True,
                text=True,
                check=False,
            )
        noise = done.stderr + done.stdout
        assert "Unknown key" not in noise, noise


class TestTheArbiterTimer:
    """The timer carries install metadata only; nothing here activates it."""

    def test_it_targets_only_the_oneshot_service(self) -> None:
        text = ARBITER_TIMER.read_text(encoding="utf-8")
        assert re.search(r"^Unit=fleet-graph-arbiter\.service$", text, re.MULTILINE)
        units = re.findall(r"^Unit=(\S+)$", text, re.MULTILINE)
        assert units == ["fleet-graph-arbiter.service"], units

    def test_documented_bounded_cadence(self) -> None:
        text = ARBITER_TIMER.read_text(encoding="utf-8")
        assert re.search(r"^OnCalendar=", text, re.MULTILINE), "the cadence must be declared"

    def test_install_metadata_without_activation(self) -> None:
        text = ARBITER_TIMER.read_text(encoding="utf-8")
        assert "[Install]" in text
        assert "WantedBy=timers.target" in text
        assert "WantedBy=default.target" not in text

    def test_systemd_itself_accepts_every_key(self) -> None:
        analyze = shutil.which("systemd-analyze")
        if analyze is None:
            pytest.skip("systemd-analyze not available")
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / ARBITER_TIMER.name
            staged.write_text(ARBITER_TIMER.read_text(encoding="utf-8"), encoding="utf-8")
            done = subprocess.run(
                [analyze, "--user", "verify", str(staged)],
                capture_output=True,
                text=True,
                check=False,
            )
        noise = done.stderr + done.stdout
        assert "Unknown key" not in noise, noise


class TestNoActivationSideEffect:
    """No repo script enables, starts, or daemon-reloads the arbiter unit.

    Shipping install metadata is allowed; activating it is a supervision-plane
    decision for a later approved window, so no committed script may do it.
    """

    def test_no_script_enables_or_starts_the_arbiter_unit(self) -> None:
        action_words = ("enable", "start", "daemon-reload", "restart", "systemctl")
        offenders: list[str] = []
        script_paths = [
            *sorted(Path(__file__).resolve().parent.parent.glob("scripts/*.py")),
            Path(__file__).resolve().parent.parent / "deploy" / "release.sh",
        ]
        for path in script_paths:
            for line in path.read_text(encoding="utf-8").splitlines():
                if "fleet-graph-arbiter" in line and any(word in line for word in action_words):
                    offenders.append(f"{path.name}: {line.strip()}")
        assert not offenders, offenders
