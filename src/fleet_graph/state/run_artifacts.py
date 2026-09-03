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
import traceback
from collections.abc import Callable
from dataclasses import dataclass
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
        "log_path",
        "release_id",
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
        "goal_revision",
        "line_state",
        "dd_development_id",
        "log_path",
    }
)

#: The machine-readable values of `waiting_on`. Anything else the coordinator
#: declares is preserved verbatim in `waiting_on_declared` and *normalised* to
#: "none" -- parking is an optimisation, not a judgement, so an unknown value
#: must never fault a line (R0c ruling). "dd" is the M1 addition: a dispatch
#: line waiting on the development it just created parks as `waiting_dd`.
WAITING_ON_VALUES = frozenset({"decision", "external", "dd", "none"})
WAITING_ON_DEFAULT = "none"

#: The closed externally-facing line-state vocabulary (design.md §6.3). The
#: projection lives in ``derive_line_state``; the constants are spelled out so a
#: line-state word is never invented prose.
LINE_STATE_WORKING = "working"
LINE_STATE_WAITING_DD = "waiting_dd"
LINE_STATE_WAITING_DECISION = "waiting_decision"
LINE_STATE_WAITING_EXTERNAL = "waiting_external"
LINE_STATE_DONE = "done"
LINE_STATE_FAILED = "failed"

LINE_STATE_VALUES = frozenset(
    {
        LINE_STATE_WORKING,
        LINE_STATE_WAITING_DD,
        LINE_STATE_WAITING_DECISION,
        LINE_STATE_WAITING_EXTERNAL,
        LINE_STATE_DONE,
        LINE_STATE_FAILED,
    }
)

#: `waiting_on` -> line-state word, when the terminal is `blocked`.
_WAITING_ON_LINE_STATE = {
    "dd": LINE_STATE_WAITING_DD,
    "decision": LINE_STATE_WAITING_DECISION,
    "external": LINE_STATE_WAITING_EXTERNAL,
}


def derive_line_state(terminal: Any, waiting_on: Any = None) -> str:
    """Project the mechanical ``terminal`` + ``waiting_on`` into the closed
    externally-facing vocabulary (design.md §6.3).

    The six words are a *semantic* status, the mechanical ``terminal`` field a
    separate truth. ``done`` and ``failed`` (self-judged, distinct from the
    mechanical ``fault``) project from the terminal alone; a ``blocked``
    terminal projects to ``waiting_*`` by the waiting reason its coordinator
    declared. A missing terminal or a mechanical terminal (``fault``/``bounds``/
    ``killed``) projects to ``working`` -- there is no semantic terminal to
    state, and ``fault`` is deliberately *not* merged into ``failed``.
    """
    term = str(terminal or "")
    if term == "done":
        return LINE_STATE_DONE
    if term == "failed":
        return LINE_STATE_FAILED
    if term == "blocked":
        waiting, _ = normalize_waiting_on(waiting_on)
        return _WAITING_ON_LINE_STATE.get(waiting, LINE_STATE_WORKING)
    return LINE_STATE_WORKING


#: Where the launcher sends a line's stdout/stderr (scheduler/launcher.py
#: `log_file`, defaulting to /data/fleet-graph/logs/{folder_id}.log). The run
#: root records it so the on-disk state names its own log instead of forcing an
#: operator to know a parallel path by heart.
LOG_ROOT = Path("/data/fleet-graph/logs")

#: The deploy root's `current` symlink that every line unit execs through
#: (`/data/apps/fleet-graph/current/.venv/bin/fleet-graph`). The process
#: resolves the symlink once at exec and never re-resolves it, so the release
#: this generation actually runs is frozen at startup -- never what the
#: symlink happens to point at later.
RELEASE_CURRENT_PATH = Path("/data/apps/fleet-graph/current")

#: The fault terminal keeps a traceback summary for a human, not forever: a
#: single badly-behaved agent can produce a multi-megabyte exception. The first
#: frames and the message are the useful part.
TRACEBACK_SUMMARY_LIMIT = 4000


def _one_line(message: str) -> str:
    """Collapse an exception message to a single line, bounded in length."""
    return " ".join(message.split())[:2000]


def normalize_waiting_on(raw: Any) -> tuple[str, str | None]:
    """(normalised, declared). Absent -> ("none", None); unknown -> ("none", raw)."""
    if raw is None:
        return WAITING_ON_DEFAULT, None
    declared = str(raw)
    value = declared.strip().lower()
    if value in WAITING_ON_VALUES:
        return value, declared
    return WAITING_ON_DEFAULT, declared


#: The scheduler's per-line stall-state directory, relative to the run root.
#: ``<run_root>/.scheduler/<folder_id>.json`` is the single authority for a
#: line's parked claim (see :func:`parked_decision_state`).
SCHEDULER_STALL_SUBDIR = ".scheduler"

#: The line-run artifact names the parked-state authority reads alongside the
#: stall snapshot. A line's live-run facts live in its own run folder.
HEARTBEAT_FILE = "heartbeat.json"
TERMINAL_ARTIFACT = "terminal.json"


def scheduler_stall_path(run_root: Path, folder_id: str) -> Path:
    """Where the scheduler persists one line's stall/park state."""
    return Path(run_root) / SCHEDULER_STALL_SUBDIR / f"{folder_id}.json"


def _read_json_object(path: Path) -> dict[str, Any]:
    """One JSON object artifact, or {} on missing/unreadable/malformed."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


@dataclass(frozen=True)
class ParkedDecisionState:
    """The parked-state authority's answer for one line.

    ``parked`` is the mechanical "parked waiting_on=decision" claim;
    ``state_word`` carries the line's *actual* current state as dynamically
    read, so a refusal can name what the line is really doing instead of a
    hardcoded word (M3.1 defect 4).
    """

    parked: bool
    state_word: str


def parked_decision_state(run_root: Path, folder_id: str) -> ParkedDecisionState:
    """The single authority for "this line is parked waiting_on=decision".

    M3.1 defect 5: the parked claim used to live in two places -- the
    scheduler's stall snapshot (``parked_run_id``/``parked_at``) and the
    line's own ``terminal.json`` declaration (``blocked`` +
    ``waiting_on``) -- and the two could disagree. This converges the read:
    **the stall snapshot is the authority** (it is the control surface the
    scheduler's park/wake/escape-hatch act on); the terminal declaration
    supplies the waiting reason and anchors run consistency. The delivery
    surface (decision MCP) and the fleet-state read model both derive from
    this one function, so a fork resolves identically on both sides:

    - no stall file at all -> nothing contradicts the terminal declaration,
      so a ``waiting_on=decision`` terminal stands (single truth, legacy
      semantics for a line the scheduler has not yet parked);
    - stall present but the snapshot cleared -> the park was retracted (the
      operator escape hatch or a wake); the declaration alone no longer
      reports parked;
    - snapshot present but for a superseded run (``terminal.json.run_id``
      names a different run) -> the stale park does not hold;
    - snapshot present, run-consistent, terminal absent or declaring
      ``waiting_on=decision`` -> parked. A dd dispatch park
      (``parked_dd_development_id``) is not a decision park.
    """
    run_root = Path(run_root)
    stall = _read_json_object(scheduler_stall_path(run_root, folder_id))
    terminal = _read_json_object(run_root / folder_id / TERMINAL_ARTIFACT)
    heartbeat = _read_json_object(run_root / folder_id / HEARTBEAT_FILE)

    waiting, declared = normalize_waiting_on(terminal.get("waiting_on"))
    term_word = str(terminal.get("terminal") or "") if terminal else ""

    def state_word_default() -> str:
        if terminal:
            word = f"terminal={term_word or 'unknown'}"
            # The line's own declared waiting reason, verbatim -- an unknown
            # value normalises to "none" for parking but is the actual state
            # word a refusal must carry.
            if declared:
                word += f", waiting_on={declared}"
            return word
        if heartbeat:
            return f"running (round {heartbeat.get('round')})"
        return "no park state"

    snapshot_run = str(stall.get("parked_run_id") or "")
    snapshot_live = bool(snapshot_run) and stall.get("parked_at") is not None
    if snapshot_live:
        term_run = str(terminal.get("run_id") or "") if terminal else ""
        if term_run and term_run != snapshot_run:
            word = (
                f"{state_word_default()} (park snapshot {snapshot_run!r} superseded "
                f"by run {term_run!r})"
            )
            return ParkedDecisionState(parked=False, state_word=word)
        if stall.get("parked_dd_development_id"):
            word = f"parked on dd development {stall['parked_dd_development_id']!r}"
            return ParkedDecisionState(parked=False, state_word=word)
        if terminal and waiting != "decision":
            return ParkedDecisionState(parked=False, state_word=state_word_default())
        return ParkedDecisionState(parked=True, state_word="parked waiting_on=decision")

    if not stall:
        # No scheduler state at all: the terminal declaration is the only
        # parked claim in existence, so nothing can fork against it.
        if waiting == "decision":
            return ParkedDecisionState(
                parked=True, state_word="parked waiting_on=decision (terminal declaration)"
            )
        return ParkedDecisionState(parked=False, state_word=state_word_default())

    # Scheduler state exists but the snapshot is cleared: the park was
    # retracted on the authority side; a terminal declaration alone no
    # longer reports a live park.
    return ParkedDecisionState(parked=False, state_word=state_word_default())


def iso(ts: float) -> str:
    """UTC, second precision. Matches pump.py `_iso` exactly."""
    return time.strftime(ISO_FORMAT, time.gmtime(ts))


def capture_release_id(path: str | Path = RELEASE_CURRENT_PATH) -> str | None:
    """The release_id this generation actually runs, frozen at startup.

    The line unit execs through ``/data/apps/fleet-graph/current/.venv/bin/
    fleet-graph``, and the process resolves that symlink exactly once at exec.
    This mirrors that freeze: read the symlink target's basename once and
    return it as the release_id. Missing/unreadable/unresolvable -> ``None``
    (fail-soft: a line that cannot name its release still runs; only the
    observable field goes null). The read model must never call this -- it
    only consumes the persisted value.
    """
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    basename = resolved.name
    return basename or None


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
        log_path: str | Path | None = None,
        release_id: str | None = None,
    ) -> None:
        self.run_root = Path(run_root)
        self.run_id = run_id
        self.folder_id = folder_id
        self._clock = clock
        self._pid = pid if pid is not None else os.getpid()
        self.started_at = started_at if started_at is not None else clock()
        self.log_path = (
            str(log_path) if log_path is not None else str(LOG_ROOT / f"{folder_id}.log")
        )
        #: Frozen once at construction (line process start). The caller passes
        #: the startup-captured value; it is never re-resolved here, so a
        #: re-pointed `current` symlink mid-generation leaves this unchanged.
        self.release_id = release_id

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
            "log_path": self.log_path,
            "release_id": self.release_id,
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

    # --- worker turn report ----------------------------------------------

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> Path:
        """Persist the validated structured worker turn report (E4a).

        This is the canonical turn-result record: the only place the worker's
        structured control fields and its optional prose attachment live,
        written before any downstream accounting or transition reads it. The
        prose attachment rides inside the record as a non-control child field
        with the report's own identity -- inspection-only, never a control
        surface. Overwrites the previous turn's record; the canonical record is
        the latest validated report. Unlike heartbeat/rounds, a lost write is
        failed loudly: an absent canonical record is indistinguishable from a
        turn that never validated.
        """
        path = self._worker_report_path()
        payload = {"round": round_no, "report": report}
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return path

    def _worker_report_path(self) -> Path:
        return self.run_root / "worker-report.json"

    @property
    def worker_report_path(self) -> Path:
        return self._worker_report_path()

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
        goal_revision: str | None = None,
        dd_development_id: str | None = None,
    ) -> Path:
        """Record the terminal event locally. Call this *before* any publish.

        Unlike the other two writers this does not swallow errors. Losing the
        heartbeat degrades observability; losing the terminal record means a
        line that ended looks identical to one that disappeared, and that is
        worth failing loudly over.

        `waiting_on` is the machine field the scheduler's parking reads: for a
        `blocked` terminal, "decision" means only a human ruling can unblock
        this line, and "dd" (M1) means the line parked on the development it
        just dispatched. Always written (default "none") so the field set stays
        exact; `waiting_on_declared` preserves whatever raw value the
        coordinator actually declared, unknown values included.

        `goal_revision` is the goal.md ``content_revision`` the line actually
        *consumed* at its last coordinator round (G1) -- never the revision
        current at the moment the terminal is written. It is a mechanical hash,
        never prose, and it is optional: a terminal written without a consumed
        revision (an old terminal, or a read that failed) simply carries
        ``None``, which the scheduler reads as "no reliable parking baseline"
        and fails open rather than locking the line.

        `dd_development_id` is the development id the line dispatched and is
        now parked on (`waiting_on: "dd"`). It is the scheduler's anchor for
        the two dd wake facts; absent for every other terminal.

        `line_state` is the closed externally-facing line-state word derived
        from ``terminal`` + ``waiting_on`` (design.md §6.3). Always written so
        the field set stays exact.
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
            "goal_revision": goal_revision,
            "line_state": derive_line_state(terminal, waiting_on),
            "dd_development_id": dd_development_id,
            "log_path": self.log_path,
        }
        with self._terminal_path.open("w", encoding="utf-8") as handle:
            json.dump(event, handle, ensure_ascii=False, indent=2)
        return self._terminal_path

    # --- fault terminal --------------------------------------------------

    def write_fault_terminal(self, *, exception: BaseException, rounds: int = 0) -> Path:
        """Record a crash that escaped the graph as a `fault` terminal.

        The `finalise` node only ever runs on a well-formed terminal, so an
        unexpected node exception otherwise leaves no terminal.json at all --
        and any stale terminal from a previous generation then masquerades as
        the current state. This is the exception boundary's counterpart: it
        writes `terminal: "fault"` plus the exception class, a one-line message
        and a truncated traceback, so a crash is distinguishable from a clean
        stop and from a line that simply vanished.
        """
        message = str(exception).strip() or type(exception).__name__
        summary = "".join(
            traceback.format_exception(type(exception), exception, exception.__traceback__)
        )
        event = {
            "run_id": self.run_id,
            "folder_id": self.folder_id,
            "terminal": "fault",
            "pump_fault": True,
            "rounds": rounds,
            "reason": _one_line(message),
            "at": iso(self._clock()),
            "pid": self._pid,
            "waiting_on": WAITING_ON_DEFAULT,
            "waiting_on_declared": None,
            "line_state": derive_line_state("fault", WAITING_ON_DEFAULT),
            "dd_development_id": None,
            "log_path": self.log_path,
            "exception_class": type(exception).__name__,
            "message": _one_line(message),
            "traceback": summary[:TRACEBACK_SUMMARY_LIMIT],
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
    "HEARTBEAT_FILE",
    "HEARTBEAT_INTERVAL_SECONDS",
    "ISO_FORMAT",
    "LINE_STATE_DONE",
    "LINE_STATE_FAILED",
    "LINE_STATE_VALUES",
    "LINE_STATE_WAITING_DD",
    "LINE_STATE_WAITING_DECISION",
    "LINE_STATE_WAITING_EXTERNAL",
    "LINE_STATE_WORKING",
    "RELEASE_CURRENT_PATH",
    "SCHEDULER_STALL_SUBDIR",
    "TERMINAL_ARTIFACT",
    "TERMINAL_FIELDS",
    "WAITING_ON_DEFAULT",
    "WAITING_ON_VALUES",
    "ParkedDecisionState",
    "RunArtifacts",
    "capture_release_id",
    "derive_line_state",
    "iso",
    "normalize_waiting_on",
    "parked_decision_state",
    "scheduler_stall_path",
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
