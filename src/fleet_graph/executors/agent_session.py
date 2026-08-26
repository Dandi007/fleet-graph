"""Long-lived worker seats, via `bin/agent-session`.

Where AgentRunNode is one-shot dispatch, a seat is a conversation that outlives
many turns -- the shape the ronin pump uses for its worker. The re-adopt
problem is the same one, and so is the answer: derive the key, give the seat
its own session root, and discovery needs no registry.

The one difference is that agent-session owns the session id, not us. So the
seat root is the stable handle and the session id is looked up inside it. A
crash between `start` and persisting the id therefore cannot orphan a seat:
the next open() finds the session by looking where it must be.

Every agent-session invocation prints exactly one JSON line envelope
(`ok:false` plus `error{code,message}` on failure), which is what `_envelope`
parses.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_graph.executors.agent_run import (
    DEFAULT_STATE_ROOT,
    _pid_is_our_run,
    _read_pid,
)

DEFAULT_AGENT_SESSION_BIN = "/data/code/self/agent-runtime/bin/agent-session"

SEAT_KEY_NAMESPACE = uuid.UUID("2a7c9e14-8d33-5b6f-a1c2-9e4d7b05f331")


class AgentSessionError(RuntimeError):
    """agent-session returned ok:false, or could not be parsed."""


def derive_seat_key(thread_id: str, seat: str) -> str:
    """Stable key for one logical seat of one graph thread.

    No `attempt` here, unlike run ids: a seat is meant to be re-entered, and a
    graph that re-derives a different key every restart would leak a worker
    process per restart.
    """
    return str(uuid.uuid5(SEAT_KEY_NAMESPACE, f"{thread_id}\x1f{seat}"))


@dataclass(frozen=True)
class SeatSpec:
    """A named seat from agents.yaml. `--agent` is the only way to start one."""

    agent: str
    cwd: str | None = None
    isolate_hooks: bool = False
    mcp_allow: tuple[str, ...] = ()
    mcp_none: bool = False
    labels: dict[str, str] = field(default_factory=dict)

    def start_argv(self, *, bin_path: str, session_root: str) -> list[str]:
        argv = [bin_path, "start", "--agent", self.agent, "--session-root", session_root]
        if self.cwd:
            argv += ["--cwd", self.cwd]
        if self.isolate_hooks:
            argv += ["--isolate-hooks"]
        if self.mcp_none:
            argv += ["--mcp-none"]
        for server in self.mcp_allow:
            argv += ["--mcp-allow", server]
        for key, value in sorted(self.labels.items()):
            argv += ["--label", f"{key}={value}"]
        return argv


@dataclass(frozen=True)
class SeatHandle:
    seat_key: str
    session_id: str
    session_root: str
    adopted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "seat_key": self.seat_key,
            "session_id": self.session_id,
            "session_root": self.session_root,
            "adopted": self.adopted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SeatHandle:
        return cls(
            seat_key=data["seat_key"],
            session_id=data["session_id"],
            session_root=data["session_root"],
            adopted=bool(data.get("adopted", False)),
        )


def _envelope(stdout: str, *, context: str) -> dict[str, Any]:
    """Parse the single JSON line agent-session prints.

    Tolerates leading noise: a runtime that logs to stdout before the envelope
    would otherwise take the whole seat down.
    """
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            if parsed.get("ok") is False:
                error = parsed.get("error") or {}
                raise AgentSessionError(
                    f"{context} failed: {error.get('code')}: {error.get('message')}"
                )
            return parsed
    raise AgentSessionError(f"{context}: no JSON envelope in output: {stdout[:300]!r}")


def find_session_id(session_root: str | Path) -> str | None:
    """The seat's session id, read from where agent-session must have put it."""
    sessions = Path(session_root) / "sessions"
    if not sessions.is_dir():
        return None
    for meta_path in sorted(sessions.glob("*/session.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        session_id = meta.get("session_id")
        if session_id:
            return str(session_id)
    return None


def read_session_meta(session_root: str | Path, session_id: str) -> dict[str, Any] | None:
    meta_path = Path(session_root) / "sessions" / session_id / "session.json"
    try:
        return json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


class AgentSessionSeat:
    def __init__(
        self,
        *,
        bin_path: str = DEFAULT_AGENT_SESSION_BIN,
        state_root: str = DEFAULT_STATE_ROOT,
    ) -> None:
        self.bin_path = bin_path
        self.state_root = Path(state_root)

    def session_root_for(self, seat_key: str) -> Path:
        return self.state_root / "seats" / seat_key

    def open(self, spec: SeatSpec, seat_key: str) -> SeatHandle:
        """Adopt the seat's live session, or start one.

        Idempotent for the same reason launch() is: the seat root is derived,
        so a restarted graph looks in exactly the same place.
        """
        session_root = self.session_root_for(seat_key)
        existing = find_session_id(session_root)
        if existing and self._daemon_alive(session_root, existing):
            return SeatHandle(seat_key, existing, str(session_root), adopted=True)

        session_root.mkdir(parents=True, exist_ok=True)
        argv = spec.start_argv(bin_path=self.bin_path, session_root=str(session_root))
        completed = subprocess.run(
            argv, capture_output=True, text=True, check=False, cwd=spec.cwd or None
        )
        envelope = _envelope(completed.stdout, context="agent-session start")
        session_id = envelope.get("session_id") or envelope.get("session", {}).get("id")
        if not session_id:
            raise AgentSessionError(f"agent-session start returned no session id: {envelope}")
        return SeatHandle(seat_key, str(session_id), str(session_root), adopted=False)

    def send(
        self, handle: SeatHandle, prompt: str, *, timeout_seconds: int = 300
    ) -> dict[str, Any]:
        """One turn. The prompt goes on stdin, never in argv.

        Keeping it off argv matters: argv is world-readable through /proc, and
        prompts routinely carry context we would not publish.
        """
        argv = [
            self.bin_path,
            "send",
            "--session",
            handle.session_id,
            "--session-root",
            handle.session_root,
            "--timeout-seconds",
            str(timeout_seconds),
        ]
        completed = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds + 60,
        )
        return _envelope(completed.stdout, context="agent-session send")

    def status(self, handle: SeatHandle) -> dict[str, Any]:
        return self._simple(handle, "status")

    def stop(self, handle: SeatHandle) -> dict[str, Any]:
        return self._simple(handle, "stop")

    def _simple(self, handle: SeatHandle, subcommand: str) -> dict[str, Any]:
        argv = [
            self.bin_path,
            subcommand,
            "--session",
            handle.session_id,
            "--session-root",
            handle.session_root,
        ]
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
        return _envelope(completed.stdout, context=f"agent-session {subcommand}")

    def _daemon_alive(self, session_root: Path, session_id: str) -> bool:
        meta = read_session_meta(session_root, session_id)
        if not meta:
            return False
        pid = meta.get("daemon_pid")
        if not isinstance(pid, int):
            pid = _read_pid(session_root / "sessions" / session_id / "daemon.pid")
        if pid is None:
            return False
        return _pid_is_our_run(pid, session_id)


__all__ = [
    "AgentSessionError",
    "AgentSessionSeat",
    "SeatHandle",
    "SeatSpec",
    "derive_seat_key",
    "find_session_id",
]
