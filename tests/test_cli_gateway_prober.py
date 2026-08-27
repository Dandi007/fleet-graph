"""Probing through `agent-run probe` (R3 step 2).

The contract is agent-run's three-state exit code, one per gateway_healthy
value: 0 -> True, 96 (PROBE_RED) -> False, 90 (CONFIG_ERROR) -> raised as
"cannot ask", which the daemon's untouched except clause turns into None.
Everything here runs against a fake agent-run script -- the real one bills a
16-token completion per call.
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.scheduler.probe import (
    AGENT_RUN_BIN_ENV,
    CliGatewayProber,
    MissingProbeCredential,
    ProbeConfigError,
    UnknownSeat,
)


def fake_agent_run(
    tmp_path: Path,
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    sleep: float = 0.0,
) -> tuple[Path, Path]:
    """A stand-in agent-run: records its argv, then answers as instructed."""
    argv_log = tmp_path / "argv.json"
    script = tmp_path / "agent-run"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, pathlib, sys, time
            pathlib.Path({str(argv_log)!r}).write_text(json.dumps(sys.argv[1:]))
            time.sleep({sleep!r})
            sys.stdout.write({stdout!r})
            sys.stderr.write({stderr!r})
            sys.exit({exit_code!r})
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, argv_log


class TestExitCodeMapping:
    def test_exit_zero_is_healthy_and_asks_with_json(self, tmp_path: Path) -> None:
        script, argv_log = fake_agent_run(
            tmp_path, exit_code=0, stdout='{"agent":"opencode-dsv4pro","healthy":true}'
        )
        prober = CliGatewayProber(binary=str(script))

        assert prober.check("opencode-dsv4pro") is True
        assert json.loads(argv_log.read_text()) == ["probe", "opencode-dsv4pro", "--json"]

    def test_probe_red_is_false_not_an_exception(self, tmp_path: Path) -> None:
        script, _ = fake_agent_run(
            tmp_path, exit_code=96, stdout='{"agent":"opencode-gpt-terra","healthy":false}'
        )
        assert CliGatewayProber(binary=str(script)).check("opencode-gpt-terra") is False

    def test_config_error_raises_with_agent_runs_detail_verbatim(self, tmp_path: Path) -> None:
        detail = 'unknown agent "wat" (agents.yaml)'
        script, _ = fake_agent_run(
            tmp_path,
            exit_code=90,
            stdout=json.dumps({"agent": "wat", "healthy": None, "detail": detail}),
            stderr=f"AGENT_RUN_ERROR code=CONFIG_ERROR detail={detail}\n",
        )
        with pytest.raises(ProbeConfigError) as excinfo:
            CliGatewayProber(binary=str(script)).check("wat")
        assert detail in str(excinfo.value)

    def test_config_error_is_caught_by_the_daemons_existing_except_clause(
        self, tmp_path: Path
    ) -> None:
        """R3 step 2 leaves daemon.py untouched, so "cannot ask" must be an
        instance of both exceptions its except tuple already names."""
        script, _ = fake_agent_run(tmp_path, exit_code=90, stderr="broken\n")
        with pytest.raises((UnknownSeat, MissingProbeCredential)) as excinfo:
            CliGatewayProber(binary=str(script)).check("opencode-dsv4pro")
        assert isinstance(excinfo.value, UnknownSeat)
        assert isinstance(excinfo.value, MissingProbeCredential)

    def test_config_error_falls_back_to_stderr_when_stdout_is_not_json(
        self, tmp_path: Path
    ) -> None:
        stderr_line = "AGENT_RUN_ERROR code=CONFIG_ERROR detail=secrets.env is unreadable"
        script, _ = fake_agent_run(tmp_path, exit_code=90, stderr=stderr_line + "\n")
        with pytest.raises(ProbeConfigError) as excinfo:
            CliGatewayProber(binary=str(script)).check("opencode-dsv4pro")
        assert stderr_line in str(excinfo.value)

    def test_an_unexpected_exit_code_is_cannot_ask_not_red(self, tmp_path: Path) -> None:
        script, _ = fake_agent_run(tmp_path, exit_code=1, stderr="bun crashed\n")
        with pytest.raises(ProbeConfigError) as excinfo:
            CliGatewayProber(binary=str(script)).check("opencode-dsv4pro")
        assert "1" in str(excinfo.value)
        assert "bun crashed" in str(excinfo.value)


class TestCannotAskFailures:
    def test_a_timeout_is_cannot_ask_not_red(self, tmp_path: Path) -> None:
        """An unanswered question is not a red answer: red burns backoff on a
        seat that may be perfectly healthy; None makes `decide` refuse."""
        script, _ = fake_agent_run(tmp_path, exit_code=0, sleep=5.0)
        prober = CliGatewayProber(binary=str(script), timeout=0.5)
        with pytest.raises(ProbeConfigError) as excinfo:
            prober.check("opencode-dsv4pro")
        assert "no answer" in str(excinfo.value)

    def test_a_missing_binary_is_cannot_ask(self, tmp_path: Path) -> None:
        prober = CliGatewayProber(binary=str(tmp_path / "does-not-exist"))
        with pytest.raises(ProbeConfigError) as excinfo:
            prober.check("opencode-dsv4pro")
        assert AGENT_RUN_BIN_ENV in str(excinfo.value)


class TestBinaryResolution:
    def test_the_env_escape_hatch_wins_over_the_default(self, tmp_path: Path) -> None:
        script, _ = fake_agent_run(tmp_path, exit_code=0)
        prober = CliGatewayProber(env={AGENT_RUN_BIN_ENV: str(script)})
        assert prober.binary == str(script)
        assert prober.check("opencode-dsv4pro") is True

    def test_the_default_is_the_production_wrapper(self) -> None:
        prober = CliGatewayProber(env={})
        assert prober.binary == os.path.expanduser("~/.local/bin/agent-run")

    def test_an_explicit_binary_wins_over_everything(self, tmp_path: Path) -> None:
        script, _ = fake_agent_run(tmp_path, exit_code=0)
        prober = CliGatewayProber(binary=str(script), env={AGENT_RUN_BIN_ENV: "/elsewhere"})
        assert prober.binary == str(script)


class TestConfigErrorDetailReachesProbeReasons:
    def test_the_daemon_logs_agent_runs_detail_as_probe_detail(self, tmp_path: Path) -> None:
        """End to end through the untouched daemon: CONFIG_ERROR's detail must
        land verbatim in the no_probe refusal's probe_detail."""
        from test_scheduler_daemon import make

        detail = "NEW_API_GATEWAY_TOKEN_OPENAI is unset in secrets.env"
        script, _ = fake_agent_run(
            tmp_path,
            exit_code=90,
            stdout=json.dumps({"agent": "opencode-dsv4pro", "healthy": None, "detail": detail}),
        )
        scheduler = make(tmp_path, prober=CliGatewayProber(binary=str(script)))
        record = scheduler.tick()[0].as_dict()

        assert record["refusal"] == "no_probe"
        assert detail in record["probe_detail"]


class TestSchedulerRunWiring:
    """cli.py must both read the switch and act on it: a field the file never
    reads, or a flag the constructor never consults, is the launch_stagger
    lesson all over again."""

    def _prober_for(self, tmp_path: Path, monkeypatch: Any, body: dict[str, Any], **args: Any):
        config = tmp_path / "scheduler.json"
        config.write_text(json.dumps(body), encoding="utf-8")

        captured: dict[str, Any] = {}

        class FakeScheduler:
            def __init__(self, cfg: Any, *, prober: Any = None, **kwargs: Any) -> None:
                captured["prober"] = prober

            def tick(self) -> list[Any]:
                return []

        import fleet_graph.scheduler.daemon as daemon
        from fleet_graph.cli import _scheduler_run

        monkeypatch.setattr(daemon, "Scheduler", FakeScheduler)
        defaults: dict[str, Any] = {
            "config": str(config),
            "no_probe": False,
            "dry_run": True,
            "once": True,
        }
        defaults.update(args)
        assert _scheduler_run(argparse.Namespace(**defaults)) == 0
        return captured["prober"]

    def test_the_switch_selects_the_runtime_prober(self, tmp_path: Path, monkeypatch: Any) -> None:
        prober = self._prober_for(tmp_path, monkeypatch, {"probe_via_runtime": True})
        assert isinstance(prober, CliGatewayProber)

    def test_the_default_stays_on_the_direct_http_prober(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from fleet_graph.scheduler.probe import GatewayProber

        prober = self._prober_for(tmp_path, monkeypatch, {})
        assert isinstance(prober, GatewayProber)
        assert not isinstance(prober, CliGatewayProber)

    def test_no_probe_still_means_no_prober_either_way(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        prober = self._prober_for(tmp_path, monkeypatch, {"probe_via_runtime": True}, no_probe=True)
        assert prober is None
