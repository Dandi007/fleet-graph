"""The on-disk contract of a running line: heartbeat, rounds, terminal.

fleet-sentinel reads these files directly and is explicitly *not* being changed
as part of this refactor, so the shapes here are not ours to redesign. Every
field name, the timestamp format, and the write ordering are transcribed from
goal-agent's pump (`pump.py` `_write_heartbeat` / `_append_round` /
`_write_terminal`). The tests assert exact field sets so drift fails here
rather than silently blinding the monitoring.

Three rules carried over verbatim, each for a reason:

- **Heartbeat and rounds writes never block the loop.** A full disk must not
  stop a line from working; it degrades observability, nothing else.
- **terminal.json is written locally *before* the bypass publish.** If the
  process dies between the two, the local record is the one that survives, and
  a terminal with no trace is indistinguishable from a line that vanished.
- **rounds.jsonl is appended per round and flushed, never rewritten at the
  end.** An earlier implementation rewrote it wholesale on termination, which
  meant a killed line lost its entire history.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

Phase = Literal["coordinator", "worker"]
# "acceptance" joined in the R0d hotfix: the acceptance step heartbeats like any
# other phase, and a phase enum that lags the graph is a crash loop -- the
# checkpoint resumes straight back into the raising node on every relaunch
# (observed in production on wf-a08949, 2026-08-27 19:18-22:14).
VALID_PHASES: frozenset[str] = frozenset({"coordinator", "worker", "acceptance"})

# The pump refreshes at least this often; fleet-sentinel's staleness check is
# calibrated against it, so slowing it down would create false alarms.
HEARTBEAT_INTERVAL_SECONDS = 5.0

HEARTBEAT_FIELDS = frozenset(
    {
        "run_id",
        "folder_id",
        "round",
        "phase",
        "pid",
        "started_at",
        "phase_started_at",
        "updated_at",
    }
)

TERMINAL_FIELDS = frozenset(
    {
        "run_id",
        "folder_id",
        "terminal",
        "pump_fault",
        "rounds",
        "reason",
        "at",
        "pid",
        "waiting_on",
        "waiting_on_declared",
    }
)

#: The machine-readable values of `waiting_on`. Anything else the coordinator
#: declares is preserved verbatim in `waiting_on_declared` and *normalised* to
#: "none" -- parking is an optimisation, not a judgement, so an unknown value
#: must never fault a line (R0c ruling).
WAITING_ON_VALUES = frozenset({"decision", "external", "none"})
WAITING_ON_DEFAULT = "none"


def normalize_waiting_on(raw: Any) -> tuple[str, str | None]:
    """(normalised, declared). Absent -> ("none", None); unknown -> ("none", raw)."""
    if raw is None:
        return WAITING_ON_DEFAULT, None
    declared = str(raw)
    value = declared.strip().lower()
    if value in WAITING_ON_VALUES:
        return value, declared
    return WAITING_ON_DEFAULT, declared


def iso(ts: float) -> str:
    """UTC, second precision. Matches pump.py `_iso` exactly."""
    return time.strftime(ISO_FORMAT, time.gmtime(ts))


class RunArtifacts:
    def __init__(
        self,
        run_root: str | Path,
        *,
        run_id: str,
        folder_id: str,
        started_at: float | None = None,
        clock: Callable[[], float] = time.time,
        pid: int | None = None,
    ) -> None:
        self.run_root = Path(run_root)
        self.run_id = run_id
        self.folder_id = folder_id
        self._clock = clock
        self._pid = pid if pid is not None else os.getpid()
        self.started_at = started_at if started_at is not None else clock()

        self._rounds_path = self.run_root / "rounds.jsonl"
        self._heartbeat_path = self.run_root / "heartbeat.json"
        self._terminal_path = self.run_root / "terminal.json"

        self._hb_round: int | None = None
        self._hb_phase: str | None = None
        self._hb_phase_started_at = self.started_at
        self._hb_last_write = 0.0
        self._hb_warned = False

        self.run_root.mkdir(parents=True, exist_ok=True)

    # --- heartbeat -------------------------------------------------------

    def heartbeat(self, round_no: int, phase: Phase, *, force: bool = False) -> bool:
        """Refresh the heartbeat if the phase changed or the interval elapsed.

        Returns whether a write happened. `updated_at` must advance on every
        write: it is the only thing standing between a SIGKILLed pump and
        looking alive forever.
        """
        if phase not in VALID_PHASES:
            raise ValueError(f"phase must be one of {sorted(VALID_PHASES)}, got {phase!r}")

        now = self._clock()
        changed = round_no != self._hb_round or phase != self._hb_phase
        if changed:
            self._hb_phase_started_at = now
        elif not force and now - self._hb_last_write < HEARTBEAT_INTERVAL_SECONDS:
            return False

        self._hb_round = round_no
        self._hb_phase = phase
        payload = {
            "run_id": self.run_id,
            "folder_id": self.folder_id,
            "round": round_no,
            "phase": phase,
            "pid": self._pid,
            "started_at": iso(self.started_at),
            "phase_started_at": iso(self._hb_phase_started_at),
            "updated_at": iso(now),
        }
        try:
            with self._heartbeat_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            self._hb_last_write = now
            self._hb_warned = False
            return True
        except OSError as exc:
            # One warning per outage, not one per beat -- a full disk would
            # otherwise drown the log in identical lines.
            if not self._hb_warned:
                log.warning("heartbeat write failed (not blocking the loop): %s", exc)
                self._hb_warned = True
            return False

    # --- rounds ----------------------------------------------------------

    def append_round(self, line: dict[str, Any]) -> bool:
        """Append one round record and flush. Never rewrites earlier lines."""
        try:
            with self._rounds_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
                handle.flush()
            return True
        except OSError as exc:
            log.warning("rounds.jsonl append failed (not blocking the loop): %s", exc)
            return False

    def read_rounds(self) -> list[dict[str, Any]]:
        """Read back what was recorded. For tests and for resume."""
        if not self._rounds_path.exists():
            return []
        rounds: list[dict[str, Any]] = []
        for raw in self._rounds_path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                rounds.append(json.loads(raw))
        return rounds

    # --- terminal --------------------------------------------------------

    def write_terminal(
        self,
        *,
        terminal: str,
        rounds: int,
        reason: str | None = None,
        pump_fault: bool = False,
        waiting_on: str = WAITING_ON_DEFAULT,
        waiting_on_declared: str | None = None,
    ) -> Path:
        """Record the terminal event locally. Call this *before* any publish.

        Unlike the other two writers this does not swallow errors. Losing the
        heartbeat degrades observability; losing the terminal record means a
        line that ended looks identical to one that disappeared, and that is
        worth failing loudly over.

        `waiting_on` is the machine field the scheduler's parking reads: for a
        `blocked` terminal, "decision" means only a human ruling can unblock
        this line. Always written (default "none") so the field set stays
        exact; `waiting_on_declared` preserves whatever raw value the
        coordinator actually declared, unknown values included.
        """
        event = {
            "run_id": self.run_id,
            "folder_id": self.folder_id,
            "terminal": terminal,
            "pump_fault": pump_fault,
            "rounds": rounds,
            "reason": reason,
            "at": iso(self._clock()),
            "pid": self._pid,
            "waiting_on": waiting_on,
            "waiting_on_declared": waiting_on_declared,
        }
        with self._terminal_path.open("w", encoding="utf-8") as handle:
            json.dump(event, handle, ensure_ascii=False, indent=2)
        return self._terminal_path

    @property
    def heartbeat_path(self) -> Path:
        return self._heartbeat_path

    @property
    def rounds_path(self) -> Path:
        return self._rounds_path

    @property
    def terminal_path(self) -> Path:
        return self._terminal_path


def signal_terminal_name(signum: int) -> str:
    """SIGTERM/SIGINT end a line as `killed`, exiting 128+signum."""
    import signal as signal_module

    try:
        return signal_module.Signals(signum).name
    except ValueError:
        return f"SIG{signum}"


__all__ = [
    "HEARTBEAT_FIELDS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "ISO_FORMAT",
    "TERMINAL_FIELDS",
    "WAITING_ON_DEFAULT",
    "WAITING_ON_VALUES",
    "RunArtifacts",
    "iso",
    "normalize_waiting_on",
    "signal_terminal_name",
    "write_json_durable",
]


def write_json_durable(path: str | Path, obj: Any) -> Path:
    """Write JSON and make it durable before returning.

    The flush-then-fsync is the whole point: without it the data sits in the
    page cache, and a machine that loses power after the caller acked its
    inbox deliveries comes back with the messages gone from both sides. This
    is what `Inbox.drain_then_ack` requires of its persist callback.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    return target
