"""The shipped systemd unit, checked against the CLI it claims to run.

The P0 skeleton named `fleet-graph serve`, a subcommand that has never
existed. With `Restart=always` and `RestartSec=5` that is not a visible
failure -- it is a crash loop, quietly, forever. A unit file no test reads is
a unit file that is only validated in production.
"""

from __future__ import annotations

import re
from pathlib import Path

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
        """Without the leading `-`, an absent env file fails the unit with a
        message about systemd, not about the credential that is missing."""
        text = UNIT.read_text(encoding="utf-8")
        assert re.search(r"^-EnvironmentFile=", text, re.MULTILINE), (
            "EnvironmentFile must be optional; the scheduler reports its own missing credentials"
        )

    def test_no_credential_is_baked_into_the_unit(self) -> None:
        """Credentials are env-only (golden rule 3). A token in a unit file is
        a token in git."""
        for line in UNIT.read_text(encoding="utf-8").splitlines():
            if line.startswith("Environment="):
                assert "TOKEN" not in line.upper(), line
                assert "KEY" not in line.upper(), line


class TestTheRestartPolicyMatchesTheReAdoptDesign:
    def test_kill_mode_lets_executors_outlive_the_daemon(self) -> None:
        """The whole point of the re-adopt primitive: killing the daemon must
        not kill the agent runs it is supervising."""
        assert "KillMode=process" in UNIT.read_text(encoding="utf-8")
