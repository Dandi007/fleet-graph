"""The disk contract fleet-sentinel reads.

These shapes are transcribed from goal-agent's pump, not designed here, so the
assertions are deliberately exact: a renamed or dropped field must fail a test
rather than silently blind the monitoring.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from fleet_graph.state.run_artifacts import (
    HEARTBEAT_FIELDS,
    HEARTBEAT_INTERVAL_SECONDS,
    TERMINAL_FIELDS,
    RunArtifacts,
    capture_release_id,
    iso,
    signal_terminal_name,
)


class FakeClock:
    def __init__(self, start: float = 1_787_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def artifacts(tmp_path: Path, clock: FakeClock) -> RunArtifacts:
    return RunArtifacts(
        tmp_path / "run",
        run_id="run-1",
        folder_id="wf-3f30cd",
        clock=clock,
        pid=4242,
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestTimestampFormat:
    def test_matches_the_pump_format(self) -> None:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", iso(1_787_000_000.0))

    def test_is_utc(self) -> None:
        assert iso(0) == "1970-01-01T00:00:00Z"


class TestHeartbeat:
    def test_field_set_is_exact(self, artifacts: RunArtifacts) -> None:
        artifacts.heartbeat(1, "coordinator")
        assert set(read_json(artifacts.heartbeat_path)) == HEARTBEAT_FIELDS

    def test_carries_folder_id(self, artifacts: RunArtifacts) -> None:
        """Without it a SIGKILLed pump cannot be tied back to its work folder."""
        artifacts.heartbeat(1, "coordinator")
        assert read_json(artifacts.heartbeat_path)["folder_id"] == "wf-3f30cd"

    def test_updated_at_advances_on_every_write(
        self, artifacts: RunArtifacts, clock: FakeClock
    ) -> None:
        """This is the only defence against a killed pump looking alive."""
        artifacts.heartbeat(1, "coordinator")
        first = read_json(artifacts.heartbeat_path)["updated_at"]
        clock.advance(HEARTBEAT_INTERVAL_SECONDS + 1)
        assert artifacts.heartbeat(1, "coordinator") is True
        assert read_json(artifacts.heartbeat_path)["updated_at"] != first

    def test_phase_change_writes_immediately(
        self, artifacts: RunArtifacts, clock: FakeClock
    ) -> None:
        artifacts.heartbeat(1, "coordinator")
        clock.advance(0.1)
        assert artifacts.heartbeat(1, "worker") is True
        assert read_json(artifacts.heartbeat_path)["phase"] == "worker"

    def test_within_interval_and_unchanged_does_not_rewrite(
        self, artifacts: RunArtifacts, clock: FakeClock
    ) -> None:
        artifacts.heartbeat(1, "coordinator")
        clock.advance(1.0)
        assert artifacts.heartbeat(1, "coordinator") is False

    def test_phase_started_at_resets_only_on_change(
        self, artifacts: RunArtifacts, clock: FakeClock
    ) -> None:
        artifacts.heartbeat(1, "coordinator")
        started = read_json(artifacts.heartbeat_path)["phase_started_at"]
        clock.advance(HEARTBEAT_INTERVAL_SECONDS + 1)
        artifacts.heartbeat(1, "coordinator")
        assert read_json(artifacts.heartbeat_path)["phase_started_at"] == started

        clock.advance(1)
        artifacts.heartbeat(1, "worker")
        assert read_json(artifacts.heartbeat_path)["phase_started_at"] != started

    def test_unknown_phase_is_rejected(self, artifacts: RunArtifacts) -> None:
        with pytest.raises(ValueError, match="phase must be one of"):
            artifacts.heartbeat(1, "thinking")  # type: ignore[arg-type]

    def test_write_failure_does_not_raise(
        self, artifacts: RunArtifacts, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full disk degrades observability; it must not stop the line."""

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "open", boom)
        assert artifacts.heartbeat(1, "coordinator") is False


class TestRounds:
    def test_appends_one_line_per_round(self, artifacts: RunArtifacts) -> None:
        artifacts.append_round({"round": 1, "verdict": "continue"})
        artifacts.append_round({"round": 2, "verdict": "done"})
        assert [r["round"] for r in artifacts.read_rounds()] == [1, 2]

    def test_never_rewrites_earlier_lines(self, artifacts: RunArtifacts) -> None:
        """An earlier implementation rewrote the file at termination and a
        killed line lost its whole history."""
        artifacts.append_round({"round": 1})
        first_bytes = artifacts.rounds_path.read_bytes()
        artifacts.append_round({"round": 2})
        assert artifacts.rounds_path.read_bytes().startswith(first_bytes)

    def test_survives_a_terminal_write(self, artifacts: RunArtifacts) -> None:
        artifacts.append_round({"round": 1})
        artifacts.write_terminal(terminal="done", rounds=1)
        assert len(artifacts.read_rounds()) == 1

    def test_non_ascii_is_not_escaped(self, artifacts: RunArtifacts) -> None:
        artifacts.append_round({"reason": "验收通过"})
        assert "验收通过" in artifacts.rounds_path.read_text(encoding="utf-8")

    def test_write_failure_does_not_raise(
        self, artifacts: RunArtifacts, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "open", boom)
        assert artifacts.append_round({"round": 1}) is False

    def test_reading_before_any_round_is_empty(self, artifacts: RunArtifacts) -> None:
        assert artifacts.read_rounds() == []


class TestTerminal:
    def test_field_set_is_exact(self, artifacts: RunArtifacts) -> None:
        artifacts.write_terminal(terminal="done", rounds=3, reason="acceptance passed")
        assert set(read_json(artifacts.terminal_path)) == TERMINAL_FIELDS

    def test_records_the_verdict_and_round_count(self, artifacts: RunArtifacts) -> None:
        artifacts.write_terminal(terminal="blocked", rounds=7, reason="needs a ruling")
        event = read_json(artifacts.terminal_path)
        assert event["terminal"] == "blocked"
        assert event["rounds"] == 7
        assert event["reason"] == "needs a ruling"
        assert event["pump_fault"] is False

    def test_signal_termination_is_a_pump_fault(self, artifacts: RunArtifacts) -> None:
        artifacts.write_terminal(terminal="killed", rounds=2, reason="SIGTERM", pump_fault=True)
        event = read_json(artifacts.terminal_path)
        assert event["terminal"] == "killed"
        assert event["pump_fault"] is True

    def test_failure_is_loud_unlike_the_other_writers(
        self, artifacts: RunArtifacts, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A terminal with no trace is indistinguishable from a vanished line."""

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "open", boom)
        with pytest.raises(OSError, match="disk full"):
            artifacts.write_terminal(terminal="done", rounds=1)


class TestFaultTerminal:
    """The exception boundary's counterpart: a crash writes terminal: fault,
    with the exception class, a one-line message and a truncated traceback."""

    def test_records_class_message_and_traceback(self, artifacts: RunArtifacts) -> None:
        try:
            raise ValueError("boom\non two lines")
        except ValueError as exc:
            artifacts.write_fault_terminal(exception=exc)
        event = read_json(artifacts.terminal_path)
        assert event["terminal"] == "fault"
        assert event["pump_fault"] is True
        assert event["exception_class"] == "ValueError"
        assert event["message"] == "boom on two lines"
        assert "ValueError" in event["traceback"]

    def test_the_traceback_summary_is_truncated(self, artifacts: RunArtifacts) -> None:
        from fleet_graph.state.run_artifacts import TRACEBACK_SUMMARY_LIMIT

        try:
            raise RuntimeError("x" * (TRACEBACK_SUMMARY_LIMIT + 10_000))
        except RuntimeError as exc:
            artifacts.write_fault_terminal(exception=exc)
        event = read_json(artifacts.terminal_path)
        assert len(event["traceback"]) <= TRACEBACK_SUMMARY_LIMIT

    def test_a_fault_terminal_names_the_log(self, artifacts: RunArtifacts) -> None:
        try:
            raise RuntimeError("kaboom")
        except RuntimeError as exc:
            artifacts.write_fault_terminal(exception=exc)
        assert read_json(artifacts.terminal_path)["log_path"].endswith("wf-3f30cd.log")


class TestLogPath:
    """The run root is self-describing: heartbeat and terminal name the log."""

    def test_heartbeat_carries_the_log_path(self, artifacts: RunArtifacts) -> None:
        artifacts.heartbeat(1, "coordinator")
        assert read_json(artifacts.heartbeat_path)["log_path"].endswith("wf-3f30cd.log")

    def test_terminal_carries_the_log_path(self, artifacts: RunArtifacts) -> None:
        artifacts.write_terminal(terminal="done", rounds=1)
        assert read_json(artifacts.terminal_path)["log_path"].endswith("wf-3f30cd.log")

    def test_defaults_from_the_folder_id(self, tmp_path: Path) -> None:
        artifacts = RunArtifacts(tmp_path / "run", run_id="r", folder_id="wf-abc123")
        assert artifacts.log_path == "/data/fleet-graph/logs/wf-abc123.log"

    def test_is_overridable(self, tmp_path: Path) -> None:
        artifacts = RunArtifacts(
            tmp_path / "run", run_id="r", folder_id="wf-x", log_path="/tmp/custom.log"
        )
        assert artifacts.log_path == "/tmp/custom.log"


class TestSignalNames:
    def test_known_signal(self) -> None:
        assert signal_terminal_name(15) == "SIGTERM"

    def test_unknown_signal_degrades_gracefully(self) -> None:
        assert signal_terminal_name(9999) == "SIG9999"


class TestEquivalenceWithTheRealPump:
    """Compare against files the live pump actually wrote.

    Transcribing a contract by reading source is not proof. These files are the
    thing fleet-sentinel consumes, so they are the authority. Skipped where the
    old fleet's run root is absent (CI, a fresh machine), which is why the
    field-set constants above are also asserted directly.
    """

    RUNS_ROOT = Path("/data/ronin/runs")

    def _newest(self, name: str) -> Path | None:
        if not self.RUNS_ROOT.is_dir():
            return None
        samples = sorted(self.RUNS_ROOT.glob(f"**/{name}"), key=lambda p: p.stat().st_mtime)
        return samples[-1] if samples else None

    #: Fields fleet-graph added on top of the transcribed pump shape. The
    #: legacy samples under /data/ronin/runs are from the retired pump and
    #: will never grow them; the equivalence check below covers the
    #: transcribed core, and additions here must be additive-only so
    #: fleet-sentinel's reads of the old fields keep working.
    FLEET_GRAPH_ADDITIONS = frozenset(
        {
            "waiting_on",
            "waiting_on_declared",
            "log_path",
            "goal_revision",
            "release_id",
            "last_tick_at",
        }
    )

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [("heartbeat.json", HEARTBEAT_FIELDS), ("terminal.json", TERMINAL_FIELDS)],
    )
    def test_field_set_matches_a_real_sample(self, filename: str, expected: frozenset[str]) -> None:
        sample = self._newest(filename)
        if sample is None:
            pytest.skip(f"no live {filename} available on this machine")
        real = set(json.loads(sample.read_text(encoding="utf-8")))
        core = set(expected) - self.FLEET_GRAPH_ADDITIONS
        assert real == core, (
            f"{filename} drifted from the live pump: "
            f"missing={sorted(real - core)} extra={sorted(core - real)}"
        )


class TestWaitingOnField:
    """The parking machine field, always present so the field set stays exact."""

    def test_defaults_to_none(self, artifacts: RunArtifacts) -> None:
        artifacts.write_terminal(terminal="done", rounds=3)
        event = read_json(artifacts.terminal_path)
        assert event["waiting_on"] == "none"
        assert event["waiting_on_declared"] is None

    def test_written_when_declared(self, artifacts: RunArtifacts) -> None:
        artifacts.write_terminal(
            terminal="blocked",
            rounds=5,
            reason="needs a ruling",
            waiting_on="decision",
            waiting_on_declared="decision",
        )
        event = read_json(artifacts.terminal_path)
        assert event["waiting_on"] == "decision"
        assert event["waiting_on_declared"] == "decision"


class TestNormalizeWaitingOn:
    def test_absent_is_none_and_nothing_declared(self) -> None:
        from fleet_graph.state.run_artifacts import normalize_waiting_on

        assert normalize_waiting_on(None) == ("none", None)

    @pytest.mark.parametrize("value", ["decision", "external", "none"])
    def test_known_values_pass_through(self, value: str) -> None:
        from fleet_graph.state.run_artifacts import normalize_waiting_on

        assert normalize_waiting_on(value) == (value, value)

    def test_case_and_whitespace_are_forgiven(self) -> None:
        from fleet_graph.state.run_artifacts import normalize_waiting_on

        assert normalize_waiting_on(" Decision ") == ("decision", " Decision ")

    def test_unknown_values_normalise_to_none_but_are_kept(self) -> None:
        """Parking is an optimisation: a coordinator inventing a value must
        degrade to no parking, never to a fault."""
        from fleet_graph.state.run_artifacts import normalize_waiting_on

        assert normalize_waiting_on("human") == ("none", "human")
        assert normalize_waiting_on(42) == ("none", "42")


class TestReleaseId:
    """A-类可观测缺口: heartbeat carries the release this generation runs,
    frozen once at construction -- never re-resolved from the deploy `current`
    symlink, and fail-soft null when it cannot be read."""

    def test_frozen_at_construction_and_written_to_heartbeat(self, tmp_path: Path) -> None:
        artifacts = RunArtifacts(
            tmp_path / "run",
            run_id="run-1",
            folder_id="wf-x",
            release_id="20260902-030934-05dec3709ba0",
        )
        artifacts.heartbeat(1, "coordinator")
        heartbeat = read_json(artifacts.heartbeat_path)
        assert heartbeat["release_id"] == "20260902-030934-05dec3709ba0"

    def test_written_on_every_heartbeat_write(self, tmp_path: Path, clock: FakeClock) -> None:
        artifacts = RunArtifacts(
            tmp_path / "run",
            run_id="run-1",
            folder_id="wf-x",
            clock=clock,
            release_id="rel-a",
        )
        artifacts.heartbeat(1, "coordinator")
        clock.advance(HEARTBEAT_INTERVAL_SECONDS + 1)
        artifacts.heartbeat(1, "coordinator")
        assert read_json(artifacts.heartbeat_path)["release_id"] == "rel-a"

    def test_defaults_to_null(self, tmp_path: Path) -> None:
        artifacts = RunArtifacts(tmp_path / "run", run_id="run-1", folder_id="wf-x")
        artifacts.heartbeat(1, "coordinator")
        assert read_json(artifacts.heartbeat_path)["release_id"] is None

    def test_repointing_the_symlink_does_not_change_the_frozen_value(self, tmp_path: Path) -> None:
        """Negative: this generation's process exec'd through `current` once;
        re-pointing it mid-generation must not change the persisted value."""
        current = tmp_path / "current"
        releases = tmp_path / "releases"
        (releases / "rel-a").mkdir(parents=True)
        (releases / "rel-b").mkdir()
        current.symlink_to(releases / "rel-a")

        artifacts = RunArtifacts(
            tmp_path / "run",
            run_id="run-1",
            folder_id="wf-x",
            release_id=capture_release_id(current),
        )
        artifacts.heartbeat(1, "coordinator")
        assert read_json(artifacts.heartbeat_path)["release_id"] == "rel-a"

        current.unlink()
        current.symlink_to(releases / "rel-b")
        artifacts.heartbeat(2, "coordinator")
        assert read_json(artifacts.heartbeat_path)["release_id"] == "rel-a"


class TestCaptureReleaseId:
    """The one-shot startup resolution behind the frozen heartbeat field."""

    def test_resolves_the_current_symlink_basename(self, tmp_path: Path) -> None:
        (tmp_path / "releases" / "rel-1").mkdir(parents=True)
        current = tmp_path / "current"
        current.symlink_to(tmp_path / "releases" / "rel-1")
        assert capture_release_id(current) == "rel-1"

    def test_missing_path_is_fail_soft_null(self, tmp_path: Path) -> None:
        assert capture_release_id(tmp_path / "no-such-current") is None

    def test_broken_symlink_is_fail_soft_null(self, tmp_path: Path) -> None:
        current = tmp_path / "current"
        current.symlink_to(tmp_path / "releases" / "missing")
        assert capture_release_id(current) is None

    def test_symlink_loop_is_fail_soft_null(self, tmp_path: Path) -> None:
        (tmp_path / "releases").mkdir()
        loop_a = tmp_path / "releases" / "loop-a"
        loop_b = tmp_path / "releases" / "loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        current = tmp_path / "current"
        current.symlink_to(loop_a)
        assert capture_release_id(current) is None

    def test_a_resolvable_release_always_names_itself(self, tmp_path: Path) -> None:
        """Even an unreadable *content* target still names the release the
        process exec'd through -- unreadability of the release body is not a
        failure to identify it."""
        (tmp_path / "releases" / "rel-x").mkdir(parents=True)
        current = tmp_path / "current"
        current.symlink_to(tmp_path / "releases" / "rel-x")
        (tmp_path / "releases" / "rel-x").chmod(0)
        try:
            assert capture_release_id(current) == "rel-x"
        finally:
            (tmp_path / "releases" / "rel-x").chmod(0o755)


class TestAcceptancePhaseHeartbeat:
    """R0d hotfix: the acceptance step heartbeats; a phase enum that lags the
    graph is a deterministic crash loop (checkpoint resumes into the raiser)."""

    def test_acceptance_is_a_valid_heartbeat_phase(self, tmp_path):
        artifacts = RunArtifacts(tmp_path, run_id="r", folder_id="wf-x")
        assert artifacts.heartbeat(1, "acceptance", force=True)


class TestHeartbeatPeriodicTick:
    """A-类 fix: phase 内周期 tick 持续推进 updated_at / last_tick_at。

    The heartbeat.json mtime must keep moving through a long worker turn so
    fleet-sentinel stops false-alarming PumpHeartbeatStale on a live line --
    while `phase_started_at` stays put (a tick is not a phase change) and a
    tick never writes before a phase is decided.
    """

    def test_last_tick_at_is_in_the_exact_field_set(self, artifacts: RunArtifacts) -> None:
        assert "last_tick_at" in HEARTBEAT_FIELDS
        artifacts.heartbeat(1, "coordinator")
        assert set(read_json(artifacts.heartbeat_path)) == HEARTBEAT_FIELDS

    def test_tick_advances_last_tick_at_and_updated_at(
        self, artifacts: RunArtifacts, clock: FakeClock
    ) -> None:
        """Within a phase, a tick pushes updated_at and last_tick_at forward."""
        artifacts.heartbeat(1, "worker")
        before = read_json(artifacts.heartbeat_path)
        clock.advance(HEARTBEAT_INTERVAL_SECONDS + 1)
        assert artifacts.tick() is True
        after = read_json(artifacts.heartbeat_path)
        assert after["updated_at"] != before["updated_at"]
        assert after["last_tick_at"] != before["last_tick_at"]

    def test_tick_does_not_reset_phase_started_at(
        self, artifacts: RunArtifacts, clock: FakeClock
    ) -> None:
        """A tick is not a phase change: phase_started_at stays frozen."""
        artifacts.heartbeat(1, "worker")
        before = read_json(artifacts.heartbeat_path)["phase_started_at"]
        clock.advance(HEARTBEAT_INTERVAL_SECONDS + 1)
        artifacts.tick()
        after = read_json(artifacts.heartbeat_path)["phase_started_at"]
        assert after == before

    def test_tick_before_a_phase_is_decided_does_not_write(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        """The very first tick on a fresh run root has no phase to refresh: it
        must not fabricate a heartbeat with no round/phase."""
        artifacts = RunArtifacts(tmp_path / "run", run_id="run-1", folder_id="wf-x", clock=clock)
        clock.advance(HEARTBEAT_INTERVAL_SECONDS + 1)
        assert artifacts.tick() is False
        assert not artifacts.heartbeat_path.exists()

    def test_tick_write_failure_is_fail_soft(
        self, artifacts: RunArtifacts, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full disk degrades the tick to nothing; it must not raise, and a
        line mid-worker-turn must not crash because its heartbeat could not be
        refreshed."""

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        artifacts.heartbeat(1, "coordinator")
        monkeypatch.setattr(Path, "open", boom)
        assert artifacts.tick() is False

    def test_start_and_stop_ticker(self, tmp_path: Path) -> None:
        """The daemon starts and stops cleanly, and stop is idempotent."""
        artifacts = RunArtifacts(tmp_path / "run", run_id="run-1", folder_id="wf-x")
        artifacts.start_ticker()
        thread = artifacts._ticker_thread
        assert thread is not None and thread.is_alive()
        artifacts.stop_ticker()
        artifacts.stop_ticker()  # idempotent
        assert not thread.is_alive()
        assert artifacts._ticker_thread is None

    def test_ticker_daemon_writes_during_a_long_phase(self, tmp_path: Path) -> None:
        """End-to-end: once a phase is set, the daemon refreshes heartbeat.json
        within a bounded window (proves the wiring the graph relies on)."""
        artifacts = RunArtifacts(tmp_path / "run", run_id="run-1", folder_id="wf-x")
        artifacts.heartbeat(1, "worker")
        first_mtime = artifacts.heartbeat_path.stat().st_mtime_ns
        artifacts.start_ticker()
        try:
            deadline = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS * 3
            while time.monotonic() < deadline:
                if artifacts.heartbeat_path.stat().st_mtime_ns > first_mtime:
                    return
                time.sleep(0.05)
            pytest.fail("ticker daemon did not refresh heartbeat.json in time")
        finally:
            artifacts.stop_ticker()
