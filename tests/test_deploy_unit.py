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


class TestTheRestartPolicyMatchesTheReAdoptDesign:
    def test_kill_mode_lets_executors_outlive_the_daemon(self) -> None:
        """The whole point of the re-adopt primitive: killing the daemon must
        not kill the agent runs it is supervising."""
        assert "KillMode=process" in UNIT.read_text(encoding="utf-8")
