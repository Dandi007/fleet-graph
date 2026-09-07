"""X-6 M1: the work-folder timeout must bound the *whole* call, and a failed
goal probe must read as "no fact", never as "the goal changed".

Diagnosis (why the pre-existing 5s httpx bound did not cover the 2026-09-06
04:24 / 20:39 production hangs): fastmcp 3.4.7's StreamableHttpTransport only
forwards a timeout into the httpx client factory when the fastmcp ``Client``
itself was built with ``timeout=...`` (its ``read_timeout_seconds``). The old
FastMCPCaller injected a scalar timeout in the factory alone, so every single
transport-level read was bounded at 5s -- but the MCP session's request/response
wait (``ClientSession.send_request``) is armed by ``read_timeout_seconds``,
which stayed None. A server that keeps its stream alive (SSE keep-alive
comments, or any periodic byte) never trips the transport read bound, the
JSON-RPC response never arrives, and the session wait -- and so the whole
``asyncio.run`` -- hangs forever. That is the ``selectors.select`` stack py-spy
caught on katana's ``fs_stat goal.md``.

The fix is two layers: ``Client(timeout=...)`` arms the per-round-trip session
bound, and an outer ``asyncio.wait_for`` caps connect-through-stream-end so no
composition of bounded segments can exceed the caller's budget by more than
fastmcp's bounded teardown. On the wake side, ``LiveWakeSignals.goal_revision``
maps every probe failure to None ("no fact this tick") and the scheduler holds
the park instead of waking -- a probe failure is not a goal change.

The tests use in-process socket fakes and run the caller under a watchdog
thread: under a mutation that removes the bound, the caller simply never
returns, and the watchdog turns that into a red assertion instead of a hung
CI job.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig
from fleet_graph.scheduler.ignition import Refusal
from fleet_graph.scheduler.launcher import LaunchResult
from fleet_graph.scheduler.wake import LiveWakeSignals, parse_bus_timestamp
from fleet_graph.state.work_folder import FastMCPCaller, WorkFolderError

#: The probe timeout under test. The spec suggests 2~5s; 2s keeps the suite
#: quick while the slack below stays generous enough to be flake-free.
T = 2.0
#: Allowed overshoot over T. Must be >= T (spec) and must stay below the ~5s
#: an unbounded caller spends on httpx's built-in default timeout, so the
#: mutation (timeout made ineffective) turns case 1 and case 2 red: the
#: mutated caller is still running when the watchdog fires.
SLACK = 2.5

BLOCKED_AT = "2026-08-27T10:00:00Z"
BLOCKED_EPOCH = parse_bus_timestamp(BLOCKED_AT)
PRIME_EPOCH = BLOCKED_EPOCH - 1800.0
TICK_EPOCH = BLOCKED_EPOCH + 3600.0


def run_under_watchdog(
    fn: Callable[[], Any], budget: float
) -> tuple[threading.Thread, dict[str, Any]]:
    """Run ``fn`` in a daemon thread, give it ``budget`` seconds.

    Returns the thread and a box with ``result``/``error``/``elapsed`` when the
    call finished in time. A still-alive thread after the budget is the
    mutation detector: the bound is gone and the call would hang forever.
    """
    box: dict[str, Any] = {}

    def worker() -> None:
        start = time.monotonic()
        try:
            box["result"] = fn()
        except Exception as exc:  # the assertion, not the watchdog, judges the type
            box["error"] = exc
        box["elapsed"] = time.monotonic() - start

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(budget)
    return thread, box


class SilentServer:
    """A TCP server that accepts and then says nothing. Ever.

    The spec's case-1 shape: no HTTP response bytes, no close -- the exact
    "should-answer-never-does" hole the whole-call bound exists for.
    """

    def __init__(self) -> None:
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        #: Accepted connections are kept alive (referenced) until the test ends.
        self._conns: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp/"

    def _serve(self) -> None:
        while not self._stop.is_set():
            self._srv.settimeout(0.5)
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            # Held open, never read, never answered, never closed: the
            # connection is the trap.
            self._conns.append(conn)

    def close(self) -> None:
        self._stop.set()
        for conn in self._conns:
            with contextlib.suppress(OSError):
                conn.close()
        self._srv.close()


class KeepAliveMcpServer:
    """The 0906 production hang, replayed in miniature.

    Answers the MCP initialize handshake, 202s the initialized notification,
    and then serves every further request (the tools/call POST, the GET event
    stream) as an SSE stream that only ever emits keep-alive comments -- bytes
    that defeat any per-read timeout while the JSON-RPC response never comes.
    """

    def __init__(self) -> None:
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp/"

    def _read_request(self, conn: socket.socket) -> bytes | None:
        conn.settimeout(5)
        buf = b""
        while b"\r\n\r\n" not in buf:
            try:
                data = conn.recv(65536)
            except (TimeoutError, OSError):
                return None
            if not data:
                return None
            buf += data
        headers, _, rest = buf.partition(b"\r\n\r\n")
        length = 0
        for line in headers.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        body = rest
        while len(body) < length:
            try:
                data = conn.recv(65536)
            except (TimeoutError, OSError):
                return None
            if not data:
                return None
            body += data
        return body

    def _handle(self, conn: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                body = self._read_request(conn)
                if body is None:
                    return
                if b'"initialize"' in body:
                    payload = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 0,
                            "result": {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "x6-fake", "version": "1"},
                            },
                        }
                    ).encode()
                    head = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                    )
                    conn.sendall(head + payload)
                    continue
                if b"notifications/initialized" in body:
                    conn.sendall(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")
                    continue
                # Tool call or GET stream: an SSE response whose only content
                # is keep-alive comments, forever. No response event, no close.
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
                conn.settimeout(None)
                while not self._stop.is_set():
                    try:
                        conn.sendall(b": keepalive\n\n")
                    except OSError:
                        return
                    time.sleep(0.2)
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            self._srv.settimeout(0.5)
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self._srv.close()


# --- case 1: the caller is bounded -------------------------------------------


def test_caller_returns_within_the_bound_when_the_server_never_answers() -> None:
    """A server that accepts and never answers must cost T, not forever.

    The 0906 incident: the tick loop sat in selectors.select on fs_stat for
    good. With the whole-call bound the same shape raises WorkFolderError in
    about T seconds. The watchdog turns a removed bound into a red assertion
    instead of a hung CI run.
    """
    server = SilentServer()
    try:
        caller = FastMCPCaller(url=server.url, timeout=T)
        thread, box = run_under_watchdog(
            lambda: caller.call("fs_stat", {"folder_id": "wf-x", "filename": "goal.md"}),
            budget=T + SLACK,
        )
        assert not thread.is_alive(), (
            "FastMCPCaller.call outlived its whole-call bound: the timeout "
            "mechanism is not covering connect-to-stream-end"
        )
        error = box.get("error")
        assert isinstance(error, WorkFolderError), box
        assert box["elapsed"] < T + SLACK
    finally:
        server.close()


# --- case 2: a failed goal probe is "no fact", and the tick holds the park ----


def test_goal_probe_failure_reads_as_no_fact_and_the_tick_holds_the_park(tmp_path: Path) -> None:
    """Two halves of one contract.

    Live half: LiveWakeSignals over a hung work-folder MCP maps the timeout to
    None -- "no fact this tick" -- instead of raising into the tick loop.

    Scheduler half: one tick with a failed goal probe scans the roster to
    completion, raises nothing, wakes nothing, and keeps the park (and the
    next tick, with the probe recovered, still holds it -- the line is not
    locked shut on a stale "no fact").
    """
    server = KeepAliveMcpServer()
    try:
        signals = LiveWakeSignals(wf_caller=FastMCPCaller(url=server.url, timeout=T))
        thread, box = run_under_watchdog(lambda: signals.goal_revision("wf-x"), budget=T + SLACK)
        assert not thread.is_alive(), (
            "goal_revision outlived the probe bound: it can never deliver 'no fact' in time"
        )
        assert "result" in box and box["result"] is None, box
        assert box["elapsed"] < T + SLACK
    finally:
        server.close()

    scheduler, clock, launcher, wake = _parked_line(tmp_path)
    wake.fail = RuntimeError("mcp probe down")
    clock.now += 60.0
    result = scheduler.tick()[0]
    assert result.park_event == "parked:no_goal_fact:RuntimeError"
    assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
    assert result.decision.ignite is False
    assert launcher.launched == []

    clock.now += 60.0
    wake.fail = None
    result = scheduler.tick()[0]
    assert result.park_event is None
    assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
    assert launcher.launched == []


# --- case 3: timeout=None keeps the historical unbounded semantics -----------


def test_timeout_none_stays_constructible_with_unbounded_semantics() -> None:
    """Callers that opted out of the bound (pump paths) keep exactly that."""
    assert FastMCPCaller(timeout=None).timeout is None
    assert FastMCPCaller().timeout is None
    assert FastMCPCaller("http://127.0.0.1:5602/mcp/", timeout=None).url == (
        "http://127.0.0.1:5602/mcp/"
    )


# --- scheduler harness (mirrors tests/test_parking.py, kept self-contained) --


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakeUnits:
    def is_active(self, unit_name: str) -> bool:
        return False


class _FakeProber:
    def check(self, seat: str) -> bool:
        return True


class _FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        return LaunchResult(spec.unit_name, True, "")


class _ProbeFailingWake:
    """Scripted wake double: healthy goal probe until ``fail`` is set."""

    def __init__(self, revision: str = "sha256:rev-1") -> None:
        self.revision = revision
        self.fail: Exception | None = None
        self.goal_calls = 0

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        return False

    def goal_revision(self, folder_id: str) -> str | None:
        self.goal_calls += 1
        if self.fail is not None:
            raise self.fail
        return self.revision

    def decision_landed(self, question_note_id: str, after_epoch: float) -> bool:
        return False


def _parked_line(tmp_path: Path) -> tuple[Scheduler, _Clock, _FakeLauncher, _ProbeFailingWake]:
    """A decision-parked line, ready for wake-failure ticks."""
    wake = _ProbeFailingWake()
    clock = _Clock(PRIME_EPOCH)
    launcher = _FakeLauncher()
    scheduler = Scheduler(
        SchedulerConfig(
            lines=[
                LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", alias="canary", enabled=True)
            ],
            run_root=tmp_path / "runs",
            maintenance_stop_path=tmp_path / "maintenance-stop",
        ),
        prober=_FakeProber(),
        launcher=launcher,
        units=_FakeUnits(),
        clock=clock,
        sleep=lambda _s: None,
        wake=wake,
    )
    assert scheduler.tick()[0].decision.ignite  # the priming launch
    launcher.launched.clear()
    record = {
        "terminal": "blocked",
        "rounds": 0,
        "run_id": "run-b1",
        "at": BLOCKED_AT,
        "reason": "等监督面拍板",
        "waiting_on": "decision",
        "goal_revision": wake.revision,
    }
    terminal = tmp_path / "runs" / "wf-1" / "terminal.json"
    terminal.parent.mkdir(parents=True, exist_ok=True)
    terminal.write_text(json.dumps(record), encoding="utf-8")
    clock.now = TICK_EPOCH
    assert scheduler.tick()[0].park_event == "established"
    launcher.launched.clear()
    return scheduler, clock, launcher, wake
