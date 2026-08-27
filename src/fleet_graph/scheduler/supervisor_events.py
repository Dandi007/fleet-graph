"""The supervisor's event observer, parasitic on the scheduler's own tick.

There is deliberately **no second while-sleep here** (D9, r4-design §5). The
scheduler already wakes every 60s; this module is a set of read-only scans it
performs while awake, plus a launcher call when a scan finds something worth
an audit. The supervisor graph itself runs as yet another transient unit --
a *scheduled* thing, never a second scheduler.

Four scans, one per event (r4-design §1):

- **E1** board question with no decision referencing it -- incremental pull
  over `board:work-notes`, cursor persisted next to the stall-state files.
- **E2** terminal `blocked` + `waiting_on: "decision"` -- reads the same
  terminal.json the tick already reads. Parking (R0c) is untouched: the line
  stays parked exactly as before; this observer only hands the *fact* to the
  supervisor graph so a human gets an audit report next to the question.
- **E3** terminal `fault` / `pump_fault: true` -- same file.
- **E4** `TickResult.refusal == TOTAL_CAP_REACHED` -- in-process, straight
  from the tick's own results; deduped per cap window so a breaker that
  holds for an hour is one audit, not sixty.

Two budgets, both plain counters (absorbed from the old supervisor's action
window -- the one part of its self-restraint worth keeping): at most
`max_launches_per_tick` supervisor runs per tick, and at most
`max_attempts_per_key` lifetime launches per event key. A supervisor that can
flood is a supervisor someone will turn off.

Failure discipline is the parking one: every scan fails open. A bus outage
or an unreadable cursor costs observation, never scheduling -- `after_tick`
cannot raise.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from fleet_graph.bus.board import NOTE_KIND, WORK_NOTES, Board, GateTicket
from fleet_graph.bus.client import BusClient
from fleet_graph.scheduler.ignition import DEFAULT_CAP_WINDOW_SECONDS, Refusal
from fleet_graph.scheduler.launcher import TransientLauncher
from fleet_graph.supervise.events import (
    SupervisorEvent,
    blocked_decision_event,
    board_question_event,
    cap_breaker_event,
    line_fault_event,
)

DEFAULT_SUPERVISOR_STATE_ROOT = Path("/data/fleet-graph/supervisor")
DEFAULT_UNIT_PREFIX = "fleet-graph-supervisor"

#: How many board messages one tick will page through at most.
BOARD_PAGE_LIMIT = 200

#: 与 supervise/decision_publisher.DECISION_TOKEN_ENV 同值（测试钉死相等）。
#: 不 import 那个模块——Guard C 规定唯一 importer 是 supervisor act 节点，
#: 调度层要的只是这个名字，不是发布入口。
DECISION_TOKEN_ENV = "FLEET_GRAPH_DECISION_TOKEN_FILE"


def observer_environment(
    line_environment: dict[str, str], daemon_environ: Mapping[str, str]
) -> dict[str, str]:
    """The env a supervisor transient unit gets: the lines' env, plus the
    decision credential -- and only here.

    The credential comes from the daemon's own environment (the systemd
    EnvironmentFile), never from the config's line_environment: putting it
    there would hand every line pump the key the fourth gate exists to keep
    away from lines. agent children are scrubbed either way
    (executors/agent_run.py), but a pump process has no business holding it
    at all.
    """
    env = dict(line_environment)
    token_file = daemon_environ.get(DECISION_TOKEN_ENV, "")
    if token_file:
        env[DECISION_TOKEN_ENV] = token_file
    return env


@dataclass(frozen=True)
class SupervisorLaunchSpec:
    """argv for one short-run `fleet-graph supervisor run` transient unit.

    Duck-typed against TransientLauncher's LaunchSpec surface (`argv()`,
    `unit_name`, `log_file`) rather than subclassing it: the launcher stays
    exactly as reviewed, and this spec cannot accidentally inherit line
    semantics like generations or acceptance declarations.
    """

    event: SupervisorEvent
    run_root: Path
    state_root: Path = DEFAULT_SUPERVISOR_STATE_ROOT
    unit_prefix: str = DEFAULT_UNIT_PREFIX
    working_directory: str = "/data/apps/fleet-graph/current"
    executable: str = "/data/apps/fleet-graph/current/.venv/bin/fleet-graph"
    environment: dict[str, str] = field(default_factory=dict)
    log_path: Path | None = None

    @property
    def unit_name(self) -> str:
        # Stable per event key -- a re-launch while the previous attempt is
        # still running collides on the unit name and fails loudly instead of
        # double-running the same audit.
        return f"{self.unit_prefix}-{self.event.key}"

    @property
    def log_file(self) -> Path:
        return self.log_path or Path(f"/data/fleet-graph/logs/supervisor-{self.event.key}.log")

    def argv(self) -> list[str]:
        argv = [
            "systemd-run",
            "--user",
            "--collect",
            "--unit",
            self.unit_name,
            f"--working-directory={self.working_directory}",
        ]
        for key, value in sorted(self.environment.items()):
            argv += [f"--setenv={key}={value}"]
        argv += [
            f"--property=StandardOutput=append:{self.log_file}",
            f"--property=StandardError=append:{self.log_file}",
            self.executable,
            "supervisor",
            "run",
            "--event-json",
            json.dumps(self.event.as_dict(), ensure_ascii=False, sort_keys=True),
            "--run-root",
            str(self.run_root),
            "--state-root",
            str(self.state_root),
        ]
        return argv


@dataclass
class ObserverConfig:
    run_root: Path
    supervisor_state_root: Path = DEFAULT_SUPERVISOR_STATE_ROOT
    #: Cursor + attempt counters, next to the scheduler's stall-state files
    #: and under the same discipline: it must survive a daemon restart, and
    #: deleting it is the documented reset.
    cursor_path: Path | None = None
    max_launches_per_tick: int = 2
    max_attempts_per_key: int = 3
    cap_window_seconds: float = DEFAULT_CAP_WINDOW_SECONDS
    unit_prefix: str = DEFAULT_UNIT_PREFIX
    working_directory: str = "/data/apps/fleet-graph/current"
    executable: str = "/data/apps/fleet-graph/current/.venv/bin/fleet-graph"
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def resolved_cursor_path(self) -> Path:
        return self.cursor_path or (self.run_root / ".scheduler" / "supervisor-cursor.json")


class SupervisorObserver:
    def __init__(
        self,
        config: ObserverConfig,
        *,
        launcher: TransientLauncher,
        bus: BusClient | None = None,
        units: Any = None,
        observe: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.launcher = launcher
        self.bus = bus
        #: UnitProbe-shaped; None skips the liveness check. An audit already
        #: in flight must not burn a lifetime attempt on a name collision.
        self.units = units
        self.observe = observe
        self.clock = clock

    # --- persisted cursor state ------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        empty: dict[str, Any] = {"board_seq": None, "attempts": {}}
        try:
            raw = json.loads(self.config.resolved_cursor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        if not isinstance(raw, dict):
            return empty
        attempts = raw.get("attempts")
        return {
            "board_seq": raw.get("board_seq"),
            "attempts": dict(attempts) if isinstance(attempts, dict) else {},
        }

    def _write_state(self, state: dict[str, Any]) -> None:
        path = self.config.resolved_cursor_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        except OSError:
            # Losing the cursor costs re-observation, not correctness: event
            # keys are idempotent all the way down (thread id, unit name,
            # receipt file), so a replayed event re-adopts and no-ops.
            pass

    # --- the tick hook ----------------------------------------------------

    def after_tick(
        self,
        *,
        now: float,
        folder_ids: Iterable[str],
        terminal_reader: Callable[[str], dict[str, Any] | None],
        tick_results: Iterable[Any],
    ) -> list[dict[str, Any]]:
        """Scan, budget, launch. Never raises; returns what it did for the log."""
        actions: list[dict[str, Any]] = []
        try:
            state = self._load_state()
            launched = 0

            # Terminal-derived events first: they are re-derivable every tick,
            # so deferring them to the next tick is free, whereas the board
            # cursor should only advance past questions we actually handled.
            events: list[SupervisorEvent] = []
            try:
                events.extend(self._terminal_events(folder_ids, terminal_reader))
            except Exception as exc:  # fail open
                actions.append({"source": "terminals", "error": repr(exc)[:200]})
            try:
                events.extend(self._cap_events(tick_results, now))
            except Exception as exc:  # fail open
                actions.append({"source": "cap", "error": repr(exc)[:200]})

            for event in events:
                if launched >= self.config.max_launches_per_tick:
                    actions.append({"event": event.key, "action": "deferred:tick_budget"})
                    continue
                action = self._consider(event, state)
                actions.append(action)
                if action["action"].startswith("launched"):
                    launched += 1

            # E1 last, with whatever budget remains. The cursor advances only
            # past messages that were handled (launched, skipped, or not an
            # event); a question deferred by the budget is re-read next tick.
            try:
                remaining = self.config.max_launches_per_tick - launched
                actions.extend(self._board_scan(state, remaining=remaining))
            except Exception as exc:  # fail open
                actions.append({"source": "board", "error": repr(exc)[:200]})

            self._write_state(state)
        except Exception as exc:  # the tick must survive us, whatever happens
            actions.append({"source": "observer", "error": repr(exc)[:200]})
        if self.observe is not None:
            for action in actions:
                with contextlib.suppress(Exception):  # telemetry must not bite
                    self.observe({"supervisor_observer": action})
        return actions

    # --- scans ------------------------------------------------------------

    def _terminal_events(
        self,
        folder_ids: Iterable[str],
        terminal_reader: Callable[[str], dict[str, Any] | None],
    ) -> list[SupervisorEvent]:
        events: list[SupervisorEvent] = []
        for folder_id in folder_ids:
            record = terminal_reader(folder_id)
            if record is None or record.get("run_id") is None:
                continue
            run_id = str(record["run_id"])
            terminal = record.get("terminal")
            if terminal == "blocked" and record.get("waiting_on") == "decision":
                events.append(blocked_decision_event(folder_id, run_id))
            elif terminal == "fault" or record.get("pump_fault") is True:
                events.append(line_fault_event(folder_id, run_id))
        return events

    def _cap_events(self, tick_results: Iterable[Any], now: float) -> list[SupervisorEvent]:
        tripped = [
            result
            for result in tick_results
            if getattr(result.decision, "refusal", None) is Refusal.TOTAL_CAP_REACHED
        ]
        if not tripped:
            return []
        bucket = int(now // self.config.cap_window_seconds)
        detail = str(tripped[0].decision.detail or "")
        return [cap_breaker_event(bucket, detail, [r.folder_id for r in tripped])]

    def _board_scan(self, state: dict[str, Any], *, remaining: int) -> list[dict[str, Any]]:
        if self.bus is None:
            return []
        actions: list[dict[str, Any]] = []
        board = Board(self.bus)
        cursor = state.get("board_seq")

        if cursor is None:
            # First run adopts the current head as its baseline, the same
            # honest reading account_last_run gives an unwitnessed terminal:
            # questions from before we were watching are the human's existing
            # backlog (`inbox list` shows them), not events we observed.
            _, head_seq = self.bus.messages(WORK_NOTES, limit=1)
            state["board_seq"] = head_seq
            return [{"source": "board", "action": f"cursor_adopted:head_seq={head_seq}"}]

        messages, _head = self.bus.messages(
            WORK_NOTES, after_seq=int(cursor), limit=BOARD_PAGE_LIMIT
        )
        for message in messages:
            seq = int(message["channel_seq"])
            payload = message.get("payload") or {}
            is_question = (
                message.get("kind") == NOTE_KIND and payload.get("note_type") == "question"
            )
            if not is_question:
                state["board_seq"] = seq
                continue
            ticket = GateTicket(
                question_note_id=message["message_id"],
                card_entity_id=str(payload.get("card_entity_id") or ""),
            )
            if board.decision_for(ticket) is not None:
                state["board_seq"] = seq
                continue
            if remaining <= 0:
                # Out of launches this tick: leave the cursor *before* this
                # question so the next tick re-reads it. Board questions are
                # not re-derivable the way terminals are.
                actions.append({"source": "board", "action": "deferred:tick_budget"})
                break
            event = board_question_event(ticket.question_note_id, ticket.card_entity_id)
            action = self._consider(event, state)
            action["source"] = "board"
            actions.append(action)
            if action["action"].startswith("launched"):
                remaining -= 1
            state["board_seq"] = seq
        return actions

    # --- budget + launch --------------------------------------------------

    def _receipt_path(self, event: SupervisorEvent) -> Path:
        return self.config.supervisor_state_root / "reports" / f"{event.key}.json"

    def _consider(self, event: SupervisorEvent, state: dict[str, Any]) -> dict[str, Any]:
        base = {"event": event.key, "type": event.type}
        if self._receipt_path(event).exists():
            return {**base, "action": "skipped:receipt_exists"}

        attempts = int(state["attempts"].get(event.key, 0))
        if attempts >= self.config.max_attempts_per_key:
            return {**base, "action": f"skipped:attempts_exhausted:{attempts}"}

        # The attempt number rides into the event and therefore into the
        # thread identity (`supervisor:{key}:a{n}`) -- each observer launch is
        # a fresh generation with its own checkpoint thread, so a re-run never
        # needs surgery on the shared sqlite. The unit name stays keyed on the
        # event alone: two attempts cannot run concurrently.
        spec = self._spec_for(replace(event, attempt=attempts + 1))
        if self.units is not None:
            try:
                if self.units.is_active(spec.unit_name):
                    # An audit for this event is still running; do not burn a
                    # lifetime attempt on a guaranteed name collision.
                    return {**base, "action": "skipped:audit_in_flight"}
            except Exception:  # fail open: worst case the launch collides
                pass

        # Counted on the attempt, not on success -- the same reading as the
        # scheduler's breaker: a launch that fails every time must still
        # exhaust its budget rather than retry forever.
        state["attempts"][event.key] = attempts + 1
        result = self.launcher.launch(spec)
        return {
            **base,
            "action": "launched" if result.started else "launch_failed",
            "unit": result.unit_name,
            "detail": result.detail[:200],
            "attempt": attempts + 1,
        }

    def _spec_for(self, event: SupervisorEvent) -> SupervisorLaunchSpec:
        return SupervisorLaunchSpec(
            event=event,
            run_root=self.config.run_root,
            state_root=self.config.supervisor_state_root,
            unit_prefix=self.config.unit_prefix,
            working_directory=self.config.working_directory,
            executable=self.config.executable,
            environment=dict(self.config.environment),
        )

    def describe(self, event: SupervisorEvent) -> str:
        return shlex.join(self._spec_for(event).argv())


# --- documented reset -------------------------------------------------------


def reset_supervisor_event(
    key: str,
    *,
    state_root: Path,
    cursor_path: Path,
    board_seq: int | None = None,
    bus: Any = None,
) -> dict[str, Any]:
    """Reset one event key's supervisor-side state so the observer re-fires it.

    Replaces the four-step surgery of 2026-08-28 (delete receipt, sqlite rows,
    cursor attempts, rewind board_seq, restart daemon) with the two steps that
    are still real under attempt-in-thread-identity:

    - delete the receipt (`reports/<key>.json`) -- the observer's "done" mark.

    The cursor's `attempts[<key>]` is deliberately **kept**: the attempt
    counter is exactly what makes the next launch a fresh thread
    (`supervisor:{key}:a{n+1}`). Clearing it re-derives the same `a{n}` and
    the relaunch lands on the old thread's terminal checkpoint as
    `resumed:already_complete` -- observed live on
    e1-msg_01M12MRW680AJZJH40182FXYW1 the very first time this command was
    used in production. The checkpoint db is untouched for the same reason:
    old threads' rows are inert once the attempt moves on. The budget cost is
    honest: a reset consumes lifetime attempts; raise max_attempts_per_key if
    an event legitimately needs many reruns.

    `board_seq` rewinding only matters for E1 (E2/E3 re-derive from terminals
    every tick, E4 from tick results): an explicit value wins; otherwise an
    `e1-<note_id>` key is located mechanically on the bus (message ->
    channel_seq -> cursor lands just before it), and when neither is possible
    the summary says so instead of guessing. The cursor is only ever moved
    *backwards* on the mechanical path -- re-running the reset is a no-op.

    Idempotent, and touches nothing but the supervisor's own state surface.
    No daemon restart is required: the observer reloads the cursor file at the
    start of every tick (`after_tick` -> `_load_state`).
    """
    summary: dict[str, Any] = {"key": key}

    receipt = state_root / "reports" / f"{key}.json"
    try:
        receipt.unlink()
        summary["receipt"] = f"deleted:{receipt}"
    except FileNotFoundError:
        summary["receipt"] = "absent"

    try:
        raw = json.loads(cursor_path.read_text(encoding="utf-8"))
        state = raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    attempts = state.get("attempts")
    if not isinstance(attempts, dict):
        attempts = {}
    summary["attempts"] = (
        f"kept:{attempts[key]} (next launch is a{attempts[key] + 1})"
        if key in attempts
        else "absent"
    )
    state["attempts"] = attempts

    current_seq = state.get("board_seq")
    if board_seq is not None:
        state["board_seq"] = int(board_seq)
        summary["board_seq"] = f"set:{int(board_seq)}"
    elif not key.startswith("e1-"):
        summary["board_seq"] = "not_applicable:terminal/cap events re-derive every tick"
    elif bus is None:
        summary["board_seq"] = "not_rewound:no bus client; pass --board-seq explicitly"
    else:
        question_note_id = key[len("e1-") :]
        try:
            message = bus.message(WORK_NOTES, question_note_id)
        except Exception as exc:
            message = None
            summary["board_seq"] = (
                f"not_rewound:bus lookup failed ({type(exc).__name__}); pass --board-seq"
            )
        if message is not None:
            target = int(message["channel_seq"]) - 1
            if isinstance(current_seq, int) and current_seq > target:
                state["board_seq"] = target
                summary["board_seq"] = f"rewound:{current_seq}->{target}"
            else:
                summary["board_seq"] = f"already_at_or_before:{current_seq}"
        elif "board_seq" not in summary:
            summary["board_seq"] = (
                f"not_rewound:note {question_note_id!r} not found in {WORK_NOTES}; pass --board-seq"
            )

    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    summary["cursor_path"] = str(cursor_path)
    return summary


__all__ = [
    "BOARD_PAGE_LIMIT",
    "DEFAULT_SUPERVISOR_STATE_ROOT",
    "ObserverConfig",
    "SupervisorLaunchSpec",
    "SupervisorObserver",
    "observer_environment",
    "reset_supervisor_event",
]
