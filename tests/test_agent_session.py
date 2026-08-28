"""Seat behaviour: adopt an existing session, start at most one, keep prompts off argv."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fleet_graph.executors.agent_session import (
    AgentSessionError,
    AgentSessionSeat,
    AgentSessionTimeout,
    SeatHandle,
    SeatSpec,
    _envelope,
    derive_seat_key,
    find_session_id,
)

FAKE = str(Path(__file__).parent / "fakes" / "fake_agent_session.py")


FAKE_SESSION_ID = "sess-fake-0001"


@pytest.fixture
def keepalive() -> subprocess.Popen:
    """Stands in for a session daemon.

    Its argv carries the session id because the real one does: agent-runtime
    spawns the daemon with `--session-id <id>` (src/session/client.ts), which
    is exactly what the liveness check confirms identity against.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", "--session-id", FAKE_SESSION_ID]
    )
    yield proc
    proc.kill()
    proc.wait()


@pytest.fixture
def seat(tmp_path: Path, keepalive: subprocess.Popen) -> AgentSessionSeat:
    wrapper = tmp_path / "agent-session"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    os.environ["FAKE_DAEMON_PID"] = str(keepalive.pid)
    return AgentSessionSeat(bin_path=str(wrapper), state_root=str(tmp_path / "state"))


def start_count(seat: AgentSessionSeat, seat_key: str) -> int:
    ledger = seat.session_root_for(seat_key) / "start.log"
    if not ledger.exists():
        return 0
    return len([ln for ln in ledger.read_text().splitlines() if ln.strip()])


class TestSeatKey:
    def test_is_stable(self) -> None:
        assert derive_seat_key("t", "worker") == derive_seat_key("t", "worker")

    def test_separates_threads_and_seats(self) -> None:
        base = derive_seat_key("t", "worker")
        assert derive_seat_key("t2", "worker") != base
        assert derive_seat_key("t", "coordinator") != base

    def test_has_no_attempt_dimension(self) -> None:
        """A seat is re-entered, not retried.

        If the key moved per restart, every restart would leak a worker.
        """
        import inspect

        assert list(inspect.signature(derive_seat_key).parameters) == ["thread_id", "seat"]


class TestOpen:
    def test_starts_a_session_and_returns_its_id(self, seat: AgentSessionSeat) -> None:
        key = derive_seat_key("t", "worker")
        handle = seat.open(SeatSpec(agent="opencode-gpt-terra"), key)
        assert handle.session_id == FAKE_SESSION_ID
        assert handle.adopted is False
        assert start_count(seat, key) == 1

    def test_reopen_adopts_the_live_session(self, seat: AgentSessionSeat) -> None:
        key = derive_seat_key("t", "worker")
        spec = SeatSpec(agent="opencode-gpt-terra")
        first = seat.open(spec, key)

        restarted = AgentSessionSeat(bin_path=seat.bin_path, state_root=str(seat.state_root))
        second = restarted.open(spec, key)

        assert second.adopted is True
        assert second.session_id == first.session_id
        assert start_count(seat, key) == 1, "a live seat was started twice"

    def test_dead_daemon_is_replaced_not_adopted(self, seat: AgentSessionSeat) -> None:
        key = derive_seat_key("t", "worker")
        spec = SeatSpec(agent="opencode-gpt-terra")
        seat.open(spec, key)

        # Point the recorded daemon at a pid that cannot be alive.
        meta_path = seat.session_root_for(key) / "sessions" / FAKE_SESSION_ID / "session.json"
        meta = json.loads(meta_path.read_text())
        meta["daemon_pid"] = 4_194_303
        meta_path.write_text(json.dumps(meta))

        again = seat.open(spec, key)
        assert again.adopted is False
        assert start_count(seat, key) == 2

    def test_start_argv_carries_seat_options(self) -> None:
        spec = SeatSpec(
            agent="opencode-gpt-terra",
            isolate_hooks=True,
            mcp_allow=("katana-work-folder-mcp",),
            labels={"work_folder": "wf-3f30cd"},
        )
        argv = spec.start_argv(bin_path="/bin/agent-session", session_root="/tmp/seat")
        assert argv[:3] == ["/bin/agent-session", "start", "--agent"]
        assert "--isolate-hooks" in argv
        assert argv[argv.index("--mcp-allow") + 1] == "katana-work-folder-mcp"
        assert "work_folder=wf-3f30cd" in argv
        assert argv[argv.index("--session-root") + 1] == "/tmp/seat"


class TestSend:
    def test_prompt_goes_over_stdin_not_argv(self, seat: AgentSessionSeat) -> None:
        """argv is world-readable via /proc; prompts carry context we would not publish."""
        key = derive_seat_key("t", "worker")
        handle = seat.open(SeatSpec(agent="a"), key)
        secret = "do not put me in argv"
        result = seat.send(handle, secret)
        assert result["text"] == secret

    def test_envelope_survives_runtime_chatter_on_stdout(self, seat: AgentSessionSeat) -> None:
        key = derive_seat_key("t", "worker")
        handle = seat.open(SeatSpec(agent="a"), key)
        assert seat.send(handle, "hi")["ok"] is True

    def test_failure_envelope_raises(self, seat: AgentSessionSeat) -> None:
        key = derive_seat_key("t", "worker")
        handle = seat.open(SeatSpec(agent="a"), key)
        os.environ["FAKE_SEND_FAILS"] = "1"
        try:
            with pytest.raises(AgentSessionError, match="TURN_FAILED"):
                seat.send(handle, "hi")
        finally:
            del os.environ["FAKE_SEND_FAILS"]

    def test_status_and_stop_round_trip(self, seat: AgentSessionSeat) -> None:
        key = derive_seat_key("t", "worker")
        handle = seat.open(SeatSpec(agent="a"), key)
        assert seat.status(handle)["state"] == "status"
        assert seat.stop(handle)["state"] == "stop"


class TestEnvelope:
    def test_last_json_line_wins(self) -> None:
        assert _envelope('noise\n{"ok": true, "a": 1}\n', context="c") == {"ok": True, "a": 1}

    def test_ok_false_raises_with_the_error_code(self) -> None:
        payload = '{"ok": false, "error": {"code": "E_X", "message": "boom"}}'
        with pytest.raises(AgentSessionError, match="E_X"):
            _envelope(payload, context="c")

    def test_no_envelope_raises(self) -> None:
        with pytest.raises(AgentSessionError, match="no JSON envelope"):
            _envelope("just logs\n", context="c")


class TestTypedTimeout:
    """TURN_TIMEOUT is a timeout, not a generic seat failure.

    The goal_line worker-turn guard catches TimeoutError; a TURN_TIMEOUT raised
    as a bare AgentSessionError (RuntimeError) would sail past it and crash the
    line, which is exactly the incident this tests against. The typed
    `AgentSessionTimeout` must be both an AgentSessionError (so seat callers
    keep catching it) and a TimeoutError (so the timeout path runs).
    """

    def test_turn_timeout_envelope_raises_the_typed_timeout(self) -> None:
        payload = '{"ok": false, "error": {"code": "TURN_TIMEOUT", "message": "exceeded"}}'
        with pytest.raises(AgentSessionTimeout):
            _envelope(payload, context="c")

    def test_the_typed_timeout_is_a_timeout_error(self) -> None:
        payload = '{"ok": false, "error": {"code": "TURN_TIMEOUT", "message": "exceeded"}}'
        with pytest.raises(TimeoutError):
            _envelope(payload, context="c")

    def test_the_typed_timeout_is_still_a_session_error(self) -> None:
        assert issubclass(AgentSessionTimeout, AgentSessionError)

    def test_a_non_timeout_error_is_not_the_typed_timeout(self) -> None:
        payload = '{"ok": false, "error": {"code": "TURN_FAILED", "message": "boom"}}'
        with pytest.raises(AgentSessionError) as caught:
            _envelope(payload, context="c")
        assert not isinstance(caught.value, AgentSessionTimeout)

    def test_subprocess_timeout_is_mapped_to_the_typed_timeout(
        self, seat: AgentSessionSeat, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """subprocess.TimeoutExpired is not a TimeoutError either; map it too."""
        import subprocess as sp

        def boom(*_args, **_kwargs):
            raise sp.TimeoutExpired(cmd="agent-session", timeout=360)

        monkeypatch.setattr("fleet_graph.executors.agent_session.subprocess.run", boom)
        handle = SeatHandle("key", "sess", "/root")
        with pytest.raises(AgentSessionTimeout):
            seat.send(handle, "hi")


class TestHandle:
    def test_round_trips_through_a_checkpoint(self) -> None:
        handle = SeatHandle("key", "sess", "/root", adopted=True)
        assert SeatHandle.from_dict(json.loads(json.dumps(handle.to_dict()))) == handle

    def test_find_session_id_tolerates_a_missing_root(self, tmp_path: Path) -> None:
        assert find_session_id(tmp_path / "nope") is None
