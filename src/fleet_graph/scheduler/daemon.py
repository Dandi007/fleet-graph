"""The resident scheduler: the babysitter, in git and under test.

`ignition.decide` already holds the judgement and `launcher` already knows how
to start a line in its own cgroup. What was missing is the part that runs all
day: look at each line, ask, and either start it or record why not.

Two things it deliberately does not do.

**It does not decide.** Every refusal comes from `decide()`, in the order that
function fixes. A scheduler that grew its own conditions would be a second
description of when a line may run, and the first thing to drift.

**It does not interpret a line's work.** It reads observable facts -- is a
unit up, what terminal did the line write, how many rounds did it advance,
when did we last start it -- and nothing else. Whether the work is going well
is the coordinator's business, and reading round *contents* here is how an
orchestrator turns into a second, unaccountable judge (INV-3).

The line between the two is counts versus prose. `rounds` is a number this
engine's own pump increments; `reason` is text an agent wrote. The stall guard
below is built entirely on the count, and never looks at the text -- which
matters, because the text is not stable: the same canary blocked twice on the
same missing data source and worded it differently enough that character
bigram similarity came out at 0.28. Anything keyed on that prose would have
seen two unrelated problems.

**Two gates, for two different urgencies.**

`LineSpec.enabled` is the roster: which lines this scheduler is allowed to
start at all. It defaults to *off*, so a line runs only because a reviewed
config says it runs. That default is the point. The gate it replaces --
`/data/ronin/maintenance-stop`, an external flag file that held the whole
fleet down -- had the opposite shape: every line was live and one file stood
in the way. That file carries a mandatory `expires_at` and goes inert when it
passes (babysitter v23, a 2026-08-23 ruling), which makes "the fleet ignites
because nobody renewed a file" a reachable state. A safety mechanism should
not have an edge where unattended means go. Retired on a 2026-08-26 ruling
("原来的那一整套全部退役"); the roster replaces it.

The maintenance-stop file survives as the *other* gate: the emergency stop.
Changing the roster means a PR and a release; stopping a burning fleet must
take seconds. So the file stays, with its v23 expiry semantics intact, but at
a path fleet-graph owns rather than one inherited from the retired stack.

One deliberate divergence there: a flag we cannot parse gates the fleet rather
than opening it. The old gate treated a malformed file as absent; a gate file
that is broken is an operator error worth stopping on, not worth silently
ignoring.
"""

from __future__ import annotations

import calendar
import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from fleet_graph.acceptance import AcceptanceSpec
from fleet_graph.bus.board import goal_line_card_key, goal_line_card_payload
from fleet_graph.scheduler.checkpoint_terminal import CheckpointTerminal
from fleet_graph.scheduler.ignition import (
    DEFAULT_BACKOFF_CAP_SECONDS,
    DEFAULT_CAP_WINDOW_SECONDS,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_TOTAL_CAP,
    IgnitionDecision,
    LineStatus,
    Refusal,
    decide,
)
from fleet_graph.scheduler.launcher import LaunchResult, LaunchSpec, TransientLauncher
from fleet_graph.scheduler.probe import (
    GatewayProber,
    MissingProbeCredential,
    UnknownSeat,
)
from fleet_graph.scheduler.seat_override import (
    ReconcileResult,
    SeatOverrideStore,
    effective_seat,
    render_drift_line,
    roster_seat_from,
)
from fleet_graph.scheduler.wake import WakeSignals, parse_bus_timestamp, probe_error_tag

DEFAULT_MAINTENANCE_STOP = Path("/data/fleet-graph/maintenance-stop")
DEFAULT_INTERVAL_SECONDS = 60.0

#: Parking bookkeeping, kept in the stall-state file. `park_considered_run_id`
#: marks a terminal the parking logic already looked at, so an operator who
#: clears the `parked_*` fields (the runbook escape hatch) is not re-parked on
#: the very next tick by the same blocked terminal. The other three are the
#: parking snapshot itself.
_EMPTY_PARK_FIELDS: dict[str, Any] = {
    "park_considered_run_id": None,
    "parked_run_id": None,
    "parked_at": None,
    "parked_goal_revision": None,
    "parked_inbox_available": None,
}


@dataclass(frozen=True)
class LineSpec:
    """One line the scheduler is responsible for."""

    folder_id: str
    seat: str
    max_rounds: int = 10
    alias: str | None = None
    generation: int = 1
    #: Off unless the config says otherwise. A line that appears here without
    #: being switched on is a line someone staged and has not released; the
    #: tick logs it as `line_disabled` every interval, so it is visible rather
    #: than silent.
    enabled: bool = False
    #: The acceptance declaration (R0d): argv lists the line's acceptance step
    #: runs mechanically after each worker turn. Declared *here* -- a
    #: PR-reviewed file -- and never in goal.md or the work folder, because
    #: anything an agent can write is an improper control input for what gets
    #: executed on this host (wf-13ff9e findings §31c).
    acceptance: list[list[str]] = field(default_factory=list)
    #: Where those commands run. Deliberately not defaulted to the engine's
    #: own cwd: an undeclared directory means the step refuses to run and
    #: records `skipped:no_cwd` instead of inheriting ambient state.
    acceptance_cwd: str | None = None
    #: Per command, not for the batch.
    acceptance_timeout_seconds: int = 300
    #: Optional streak-breaker bounds, forwarded to the line as
    #: `--noop-limit` / `--timeout-limit` only when the roster declares them.
    #: None means absent; the line then keeps its own defaults, so a bound that
    #: was never reviewed is never passed down.
    noop_limit: int | None = None
    timeout_limit: int | None = None


@dataclass
class TickResult:
    folder_id: str
    decision: IgnitionDecision
    launch: LaunchResult | None = None
    probe_detail: str | None = None
    #: True on every tick the line is held parked (blocked waiting on a human
    #: decision, no wake fact yet).
    parked: bool = False
    #: What the parking machinery did this tick, when anything happened:
    #: "established", "woken:inbox", "woken:goal_revision", "woken:probe_failed".
    park_event: str | None = None
    #: The blocked terminal's reason, truncated. Display only -- an operator
    #: scanning the log should see *what* the line is waiting on without
    #: opening the run root. Never an input to any decision here (INV-3).
    blocker: str | None = None
    #: Outcome of the best-effort board question on the parking tick.
    board_question: str | None = None
    #: C4: the three seats of one line -- the roster (SSoT), the runtime
    #: override (when one exists), and the effective one the line actually
    #: runs on. The triple is filled from the seat-override surface on every
    #: tick so an operator can always tell *which* seat a line is on and why.
    seat_roster: str | None = None
    seat_override: str | None = None
    seat_effective: str | None = None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "folder_id": self.folder_id,
            "ignited": self.decision.ignite,
            "refusal": self.decision.refusal.value if self.decision.refusal else None,
            "detail": self.decision.detail,
        }
        if self.seat_effective is not None:
            record["seat_roster"] = self.seat_roster
            record["seat_override"] = self.seat_override
            record["seat_effective"] = self.seat_effective
        if self.probe_detail is not None:
            record["probe_detail"] = self.probe_detail
        if self.parked:
            record["parked"] = True
        if self.park_event is not None:
            record["park_event"] = self.park_event
        if self.blocker is not None:
            record["blocker"] = self.blocker
        if self.board_question is not None:
            record["board_question"] = self.board_question
        if self.launch is not None:
            record["unit"] = self.launch.unit_name
            record["launched"] = self.launch.started
            record["launch_detail"] = self.launch.detail
        return record


@dataclass(frozen=True)
class ParkOutcome:
    """What the parking machinery concluded about one line this tick."""

    parked: bool = False
    event: str | None = None
    blocker: str | None = None
    board_question: str | None = None


class UnitProbe(Protocol):
    """Is the transient unit for this line currently up?"""

    def is_active(self, unit_name: str) -> bool: ...


class SystemdUnitProbe:
    def is_active(self, unit_name: str) -> bool:
        completed = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0


@dataclass
class SchedulerConfig:
    lines: list[LineSpec] = field(default_factory=list)
    run_root: Path = Path("/data/fleet-graph/runs")
    maintenance_stop_path: Path = DEFAULT_MAINTENANCE_STOP
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    total_cap: int = DEFAULT_TOTAL_CAP
    #: See DEFAULT_CAP_WINDOW_SECONDS -- the span `total_cap` is counted over.
    cap_window_seconds: float = DEFAULT_CAP_WINDOW_SECONDS
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS
    #: Extra environment handed to every line, on top of the scheduler's PATH.
    extra_line_environment: dict[str, str] = field(default_factory=dict)
    #: Seconds to wait between two launches in the same tick. The old
    #: babysitter slept 45s here, walking its roster rather than starting it.
    #: Nine lines starting in the same second means nine gateway probes, nine
    #: opencode sessions and nine bun processes arriving together -- a burst
    #: the fleet never had to survive before, on a gateway shared with
    #: everything else on this host. Steady-state load is unchanged either
    #: way; this is only about the shape of the start.
    launch_stagger_seconds: float = 45.0
    #: R3 step 2 canary switch: probe through `agent-run probe` (CliGatewayProber)
    #: instead of the direct-HTTP GatewayProber. Default off; flipped per
    #: instance by config PR during the canary window, removed with the old
    #: prober in R3 step 3.
    probe_via_runtime: bool = False
    #: R4-2 rollout switch: run the supervisor event observer inside this
    #: scheduler's tick (E1-E4 -> short-run `fleet-graph supervisor run`
    #: units). Default off; enabling it is a reviewed config PR, exactly like
    #: probe_via_runtime.
    supervisor_events: bool = False

    @classmethod
    def from_json(cls, path: Path) -> SchedulerConfig:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            # Keys starting with "_" are the file's comment convention
            # (_comment/_provenance at the root); allowing them inside a line
            # entry lets a declaration carry its provenance next to itself.
            lines=[
                LineSpec(**{k: v for k, v in entry.items() if not k.startswith("_")})
                for entry in raw.get("lines", [])
            ],
            run_root=Path(raw.get("run_root", "/data/fleet-graph/runs")),
            maintenance_stop_path=Path(raw.get("maintenance_stop", str(DEFAULT_MAINTENANCE_STOP))),
            interval_seconds=float(raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)),
            cooldown_seconds=float(raw.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
            total_cap=int(raw.get("total_cap", DEFAULT_TOTAL_CAP)),
            cap_window_seconds=float(raw.get("cap_window_seconds", DEFAULT_CAP_WINDOW_SECONDS)),
            # Was missing: the field existed with a default but the file was
            # never read, so setting it in config had no effect at all.
            launch_stagger_seconds=float(raw.get("launch_stagger_seconds", 45.0)),
            # Read the file, not just the field: launch_stagger_seconds above
            # once existed as a dataclass default the loader never read, and
            # setting it in config silently did nothing.
            probe_via_runtime=bool(raw.get("probe_via_runtime", False)),
            supervisor_events=bool(raw.get("supervisor_events", False)),
            backoff_cap_seconds=float(raw.get("backoff_cap_seconds", DEFAULT_BACKOFF_CAP_SECONDS)),
            extra_line_environment=dict(raw.get("line_environment", {})),
        )


class Scheduler:
    def __init__(
        self,
        config: SchedulerConfig,
        *,
        prober: GatewayProber | None = None,
        launcher: TransientLauncher | None = None,
        units: UnitProbe | None = None,
        clock: Any = None,
        observe: Any = None,
        sleep: Any = None,
        wake: WakeSignals | None = None,
        board: Any = None,
        supervisor: Any = None,
        checkpoints: Any = None,
        seat_overrides: SeatOverrideStore | None = None,
    ) -> None:
        self.config = config
        self.prober = prober
        self.launcher = launcher or TransientLauncher()
        self.units = units or SystemdUnitProbe()
        self.clock = clock or time.time
        self.observe = observe
        self.sleep = sleep or time.sleep
        #: The seat-override surface (step 7). Defaults to the scheduler's own
        #: persistent area; tests inject a store pointed at their run root.
        #: The roster seat resolver reads the same config's lines, so reconcile
        #: can tell a converged override from real drift.
        self.seat_overrides = seat_overrides or SeatOverrideStore(self.config.run_root)
        self._roster_seat = roster_seat_from(config)
        #: Wake facts for parking. None disables parking outright -- the same
        #: fail-open reading as a probe failure: no way to observe wake facts
        #: means no parking, and the line stays on plain backoff.
        self.wake = wake
        #: bus.board.Board for the best-effort question note on parking.
        #: None means parking is log-visible only.
        self.board = board
        #: The supervisor event observer (scheduler/supervisor_events.py),
        #: parasitic on this tick. None means no supervision events -- the
        #: fleet schedules exactly as before. Duck-typed on `after_tick`.
        self.supervisor = supervisor
        #: The durable checkpoint terminal reader (E3). None keeps the legacy
        #: terminal.json decision path unchanged; non-None makes checkpoint
        #: state authoritative for terminal/account/parking decisions.
        self.checkpoints = checkpoints
        #: Observability for checkpoint-read faults, keyed by folder: when a
        #: checkpoint cannot be read the scheduler falls back to terminal.json
        #: and records why, so the fault is not silently treated as "no
        #: terminal" (which would read as a completed line).
        self.checkpoint_fault_reasons: dict[str, str] = {}
        self.total_started = 0
        #: Timestamps of launches that followed a run which advanced no round.
        #: The global cap counts these, not every launch -- see `decide`.
        self.unproductive_launches: list[float] = []
        self.last_start_at: dict[str, float] = {}
        #: Why a seat's probe could not answer, keyed by seat. `decide` only
        #: learns that we could not ask; the reason belongs in the log line an
        #: operator actually reads.
        self.probe_reasons: dict[str, str] = {}

    # --- observation ------------------------------------------------------

    def maintenance_stop(self) -> bool:
        """Is the fleet-wide gate holding right now?

        Present and unexpired -> gated. Expired -> inert, per the v23 ruling.
        Unparseable -> gated, and the reason is in the refusal detail.
        """
        path = self.config.maintenance_stop_path
        if not path.exists():
            return False
        return not self._gate_expired(path)

    def _gate_expired(self, path: Path) -> bool:
        try:
            flag = json.loads(path.read_text(encoding="utf-8"))
            expires_at = flag["expires_at"]
            stamp = str(expires_at).replace("+00:00", "Z")[:19] + "Z"
            deadline = calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
        except (OSError, ValueError, TypeError, KeyError):
            # Cannot tell -> keep holding. See the module docstring.
            return False
        return self._now() >= deadline

    def _now(self) -> float:
        return float(self.clock())

    def terminal_record(self, folder_id: str) -> dict[str, Any] | None:
        """The mechanical fields of the last terminal, or None.

        `terminal`, `rounds` and `run_id` only -- all three are written by this
        engine's own pump. `reason` is deliberately not read: it is an agent's
        prose, and a scheduler that acted on it would be judging the work.

        E3: the durable checkpoint is the decision source. When a checkpoint
        reader is configured, its `get_state` result is authoritative -- an
        absent, stale, or conflicting terminal.json cannot change the answer.
        `terminal.json` survives only as the fallback: a checkpoint that was
        never written, or one that cannot be read (see
        `checkpoint_fault_reason`).
        """
        if self.checkpoints is not None:
            reading = self._checkpoint_terminal_reading(folder_id)
            if reading is not None:
                if reading.record is not None:
                    return reading.record
                if reading.authoritative:
                    # The checkpoint answered, but holds no terminal. The one
                    # case where terminal.json still has the truth here is the
                    # fault path: a crash escapes the graph before finalise, so
                    # the checkpoint never records it, and write_fault_terminal
                    # leaves the only trace (fault-path supplementation). Honour
                    # that; otherwise a stale terminal.json must not stand in
                    # for a line that is merely running, so report "no terminal".
                    fallback = self._terminal_json_record(folder_id)
                    if fallback is not None and (
                        fallback.get("terminal") == "fault" or fallback.get("pump_fault")
                    ):
                        return fallback
                    return None
                if reading.fault is not None:
                    # An actual checkpoint-read fault: fall back to the derived
                    # terminal.json and keep the reason observable, never treat
                    # an unreadable checkpoint as a completed terminal.
                    self.checkpoint_fault_reasons[folder_id] = reading.fault
                    return self._terminal_json_record(folder_id)
                # No checkpoint for this generation: ordinary terminal.json.
        return self._terminal_json_record(folder_id)

    def _terminal_json_record(self, folder_id: str) -> dict[str, Any] | None:
        """The derived terminal.json view (fault fallback / no-checkpoint path)."""
        path = self.config.run_root / folder_id / "terminal.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict):
            return None
        return {
            "terminal": record.get("terminal"),
            "rounds": record.get("rounds"),
            "run_id": record.get("run_id"),
            # Both written by this engine's own finalise, both mechanical:
            # `waiting_on` is a normalised enum (never free text -- see
            # normalize_waiting_on), `at` a timestamp. Parking reads them.
            "waiting_on": record.get("waiting_on"),
            "at": record.get("at"),
            # Also finalise's own bool, never agent prose. The supervisor
            # observer's E3 scan reads it off this same record, so the fault
            # event costs no extra file read.
            "pump_fault": record.get("pump_fault"),
        }

    def _line_for(self, folder_id: str) -> LineSpec | None:
        for line in self.config.lines:
            if line.folder_id == folder_id:
                return line
        return None

    def _checkpoint_terminal_reading(self, folder_id: str) -> CheckpointTerminal | None:
        """The checkpoint answer for the latest terminal, or None if unsourced.

        Reads the current generation's thread first, then the one before it:
        a non-`done` accounted terminal bumps the generation, so the terminal
        it belongs to sits one generation back. A checkpoint that is present
        but holds no terminal (the line is running) is authoritative and stops
        the walk; a missing checkpoint falls through to the previous
        generation, and finally to the terminal.json fallback.
        """
        line = self._line_for(folder_id)
        if line is None or self.checkpoints is None:
            return None
        generation = self.generation_of(line)
        for candidate in (generation, generation - 1):
            if candidate < 1:
                continue
            reading = self.checkpoints.read(folder_id, candidate)
            if reading.fault is not None:
                return reading
            if reading.authoritative:
                return reading
        return None

    def checkpoint_fault_reason(self, folder_id: str) -> str | None:
        """Why this line's checkpoint could not be read, when it happened."""
        return self.checkpoint_fault_reasons.get(folder_id)

    def blocker_summary(self, folder_id: str) -> str | None:
        """The blocked terminal's `reason`, truncated, for display only.

        This is the one place the scheduler touches an agent's prose, and it
        flows exclusively into the observe log and the board question -- a
        human's eyes -- never into a decision. `decide` sees only the
        mechanical `parked` bool.
        """
        path = self.config.run_root / folder_id / "terminal.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        reason = record.get("reason") if isinstance(record, dict) else None
        if not isinstance(reason, str) or not reason:
            return None
        return reason[:200]

    def terminal_of(self, folder_id: str) -> str | None:
        """What the line last wrote, or None if it never terminated."""
        record = self.terminal_record(folder_id)
        if record is None:
            return None
        value = record.get("terminal")
        return str(value) if value else None

    # --- stall bookkeeping ------------------------------------------------

    def _stall_path(self, folder_id: str) -> Path:
        return self.config.run_root / ".scheduler" / f"{folder_id}.json"

    def stall_state(self, folder_id: str) -> dict[str, Any]:
        empty = {
            "streak": 0,
            "accounted_run_id": None,
            "last_start_at": None,
            "generation": None,
            "board_card_entity_id": None,
            "board_question_note_id": None,
            **_EMPTY_PARK_FIELDS,
        }
        try:
            state = json.loads(self._stall_path(folder_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        if not isinstance(state, dict):
            return empty
        return {
            "streak": int(state.get("streak") or 0),
            "accounted_run_id": state.get("accounted_run_id"),
            "last_start_at": state.get("last_start_at"),
            "generation": state.get("generation"),
            # Parking lives in this same file, under this same discipline:
            # it must survive a daemon restart, and deleting the file is the
            # documented "retry now" escape hatch for parking too.
            "park_considered_run_id": state.get("park_considered_run_id"),
            "parked_run_id": state.get("parked_run_id"),
            "parked_at": state.get("parked_at"),
            "parked_goal_revision": state.get("parked_goal_revision"),
            "parked_inbox_available": state.get("parked_inbox_available"),
            # The line's board card entity (`work.card.v1` on board:work-index),
            # materialised by the first escalation. Per line, not per parking:
            # it survives new terminals and re-parkings, so later question
            # notes ref the same entity instead of re-publishing the card.
            "board_card_entity_id": state.get("board_card_entity_id"),
            # The question note the line's last escalation published on the
            # board (``work.note.v1`` question). Persisted per line, like the
            # card, so the decision event bridge can map a ``work.decision.v1``
            # back to the exact parked line that asked it.
            "board_question_note_id": state.get("board_question_note_id"),
        }

    def _write_stall_state(self, folder_id: str, state: dict[str, Any]) -> None:
        path = self._stall_path(folder_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        except OSError:
            # Losing the counter costs an extra attempt, not correctness.
            pass

    def generation_of(self, line: LineSpec) -> int:
        """The generation the next launch of this line must run as.

        thread_id is `{folder_id}:g{generation}`, and a thread whose checkpoint
        already terminated is spent: relaunching it routes straight to finalise
        and the line never works again (pinned in
        tests/test_line_restart.py). So each accounted terminal advances a
        persisted per-line counter, and this returns it.

        Persisted in the stall-state file for the same reason the streak is:
        a counter that reset on deploy would relaunch a spent thread on every
        release. `max` with the roster value so raising LineSpec.generation in
        config still takes effect immediately.
        """
        value = self.stall_state(line.folder_id)["generation"]
        try:
            persisted = int(value)
        except (TypeError, ValueError):
            return line.generation
        return max(persisted, line.generation)

    def _next_generation(self, current: int, terminal: Any) -> int:
        """One accounted terminal -> the generation the *next* launch needs.

        `done` deliberately does not bump: a finished line never re-ignites
        (Refusal.TERMINAL_DONE), so moving its counter would be bookkeeping
        with no launch to serve. Every other terminal (blocked/bounds/fault/
        killed) leaves a spent thread behind, and the next ignition needs a
        fresh one.
        """
        return current if terminal == "done" else current + 1

    def account_last_run(self, folder_id: str, *, base_generation: int = 1) -> int:
        """Fold the newest terminal into the stall streak, once.

        Persisted rather than kept in memory because the daemon restarts on
        every release, and a counter that resets on deploy would let a stuck
        line go back to full speed each time we ship -- exactly when someone is
        least likely to be watching it.

        A terminal is accounted at most once, keyed on the run id that wrote
        it, so re-reading the same file on every 60s tick does not inflate the
        streak. The per-line generation advances on exactly the same event and
        the same key: an accounted terminal means that thread is spent, so the
        next launch needs `{folder}:g{n+1}` (see generation_of).
        """
        record = self.terminal_record(folder_id)
        if not self._stall_path(folder_id).exists():
            # No bookkeeping yet: whatever terminal is lying there was not
            # written by a run this file witnessed. Adopt it as the baseline
            # instead of counting it.
            #
            # This is what makes the documented escape hatch true. The runbook
            # says "delete the counter file to retry now"; without this, the
            # very next tick re-reads the same old terminal -- already counted
            # once, before the delete -- and the streak comes back as 1 rather
            # than 0. Observed after clearing the canary's backoff by hand.
            #
            # It is also the honest reading in general: one terminal is not a
            # streak, and a failure from before we were watching is not ours
            # to count.
            if record is not None and record.get("run_id") is not None:
                # The baseline terminal still proves its thread is spent, so
                # the generation is initialised *past* it -- without this, the
                # runbook's "delete the counter file to retry now" would
                # relaunch the spent thread and no-op straight to finalise.
                self._write_stall_state(
                    folder_id,
                    {
                        "streak": 0,
                        "accounted_run_id": record["run_id"],
                        "last_start_at": None,
                        "generation": self._next_generation(
                            base_generation, record.get("terminal")
                        ),
                        # An adopted terminal is marked park-considered without
                        # parking it: "delete the counter file to retry now" is
                        # the runbook hatch, and a delete that re-parked the
                        # line on the next tick would make the hatch a no-op.
                        **{**_EMPTY_PARK_FIELDS, "park_considered_run_id": record["run_id"]},
                    },
                )
            return 0

        state = self.stall_state(folder_id)
        if record is None:
            return int(state["streak"])
        run_id = record.get("run_id")
        if run_id is None or run_id == state["accounted_run_id"]:
            return int(state["streak"])

        advanced = int(record.get("rounds") or 0) > 0
        finished = record.get("terminal") == "done"
        streak = 0 if (advanced or finished) else int(state["streak"]) + 1
        try:
            current_generation = max(int(state["generation"]), base_generation)
        except (TypeError, ValueError):
            current_generation = base_generation
        self._write_stall_state(
            folder_id,
            {
                "streak": streak,
                "accounted_run_id": run_id,
                "last_start_at": state["last_start_at"],
                "generation": self._next_generation(current_generation, record.get("terminal")),
                # The board card is per *line*, not per parking: a new terminal
                # must not orphan it, or every re-parking would publish a
                # duplicate card.
                "board_card_entity_id": state["board_card_entity_id"],
                # A new terminal supersedes any parking of the previous run:
                # its snapshot is cleared, and this run is not yet considered,
                # so the parking logic gets to look at it exactly once.
                **_EMPTY_PARK_FIELDS,
            },
        )
        return streak

    def last_start_of(self, folder_id: str) -> float | None:
        """When this line was last started, across daemon restarts.

        Kept on disk with the streak, and for the same reason. The streak
        surviving a restart buys nothing on its own: `decide` skips the whole
        cooldown branch when there is no start time to measure from, so a
        daemon that forgot the timestamp hands the line a free launch the
        moment we ship -- which is exactly when the backoff is longest and
        nobody is watching. Observed on the real fleet: a release at 22:13
        re-ignited a line with an already-earned streak of 2.

        The global cap's evidence is deliberately *not* persisted either. That
        breaker exists for a systemic fault -- a dead gateway, a bad release --
        and shipping new code is a plausible remedy for exactly those, so
        letting it reset on deploy is the right reading. Shipping unrelated
        code is not a remedy for one line's missing data source.

        It now counts zero-progress launches inside a window rather than every
        launch ever. Restarting to clear it is still valid, but no longer the
        routine it had become: a healthy fleet used to trip the cap roughly
        every six hours purely by working.
        """
        value = self.stall_state(folder_id)["last_start_at"]
        if value is None:
            return self.last_start_at.get(folder_id)
        try:
            return float(value)
        except (TypeError, ValueError):
            return self.last_start_at.get(folder_id)

    def record_start(self, folder_id: str, when: float) -> None:
        self.last_start_at[folder_id] = when
        state = self.stall_state(folder_id)
        state["last_start_at"] = when
        self._write_stall_state(folder_id, state)

    # --- parking ----------------------------------------------------------
    #
    # A line whose last terminal is `blocked` with `waiting_on: "decision"` is
    # waiting for a human, and no amount of backoff-paced relaunching changes
    # that -- each relaunch re-derives the same blockage at full coordinator
    # cost. So the scheduler *parks* it: refuses ignition until a mechanical
    # wake fact appears. Three wake sources, all facts and no prose:
    #
    #   1. a message arrived in the line's inbox after the blocked terminal;
    #   2. goal.md's content_revision changed since the parking snapshot;
    #   3. an operator cleared the `parked_*` fields from the stall-state file
    #      (the runbook escape hatch -- `park_considered_run_id` stays behind
    #      so the same terminal is not immediately re-parked).
    #
    # Failure discipline: parking is an optimisation, never a judgement. Any
    # failure to observe a wake fact -- no bus token, MCP down, timeout --
    # fails *open*: the line is treated as not parked and falls back to the
    # ordinary backoff behaviour. A probe outage may cost money; it must never
    # be able to lock a line shut.

    def park_state(self, line: LineSpec, now: float) -> ParkOutcome:
        """Is this line parked right now? Establishes, holds, or wakes."""
        record = self.terminal_record(line.folder_id)
        if (
            record is None
            or record.get("terminal") != "blocked"
            or record.get("waiting_on") != "decision"
            or record.get("run_id") is None
        ):
            return ParkOutcome()
        run_id = record["run_id"]
        state = self.stall_state(line.folder_id)

        if state["parked_run_id"] == run_id and state["parked_at"] is not None:
            return self._check_wake(line, record, state)

        if state["park_considered_run_id"] == run_id:
            # Considered once and not parked: probes failed open then, or the
            # operator released it, or a wake already fired. Plain backoff.
            return ParkOutcome()

        return self._establish_park(line, record, state, now)

    def _terminal_epoch(self, record: dict[str, Any]) -> float:
        return parse_bus_timestamp(record.get("at"))

    def _establish_park(
        self, line: LineSpec, record: dict[str, Any], state: dict[str, Any], now: float
    ) -> ParkOutcome:
        run_id = record["run_id"]
        # Considered exactly once, whatever happens next: fail-open and
        # already-present wake facts must not be re-probed every tick forever.
        state["park_considered_run_id"] = run_id

        # The two sources degrade *independently*. The first fleet rollout
        # wrapped both in one try, and a structural inbox failure -- the
        # service token had no read ACL on `agent:*` channels, every GET a
        # 403 -- failed the whole establish open, forever: parking never
        # happened and the goal.md source was taken down by a fault that had
        # nothing to do with it. So: an inbox probe failure marks that one
        # source unavailable and parking proceeds on the goal.md anchor
        # alone; only losing the goal.md anchor -- the one source every line
        # has -- keeps the whole thing fail-open.
        inbox_available = False
        inbox_note: str | None = None
        if self.wake is not None and line.alias:
            try:
                if self.wake.inbox_message_after(line.alias, self._terminal_epoch(record)):
                    # The wake fact predates the parking: something already
                    # arrived for this line. Never park it -- backoff will
                    # pick the message up on its own schedule.
                    self._write_stall_state(line.folder_id, state)
                    return ParkOutcome(event="not_parked:inbox_already_has_mail")
                inbox_available = True
            except Exception as exc:  # degrade this source, keep parking
                inbox_note = f"inbox_unavailable:{probe_error_tag(exc)}"

        try:
            if self.wake is None:
                raise RuntimeError("scheduler has no wake signals configured")
            revision = self.wake.goal_revision(line.folder_id)
        except Exception as exc:  # fail open, by design: no anchor, no parking
            self._write_stall_state(line.folder_id, state)
            return ParkOutcome(event=f"not_parked:probe_failed:{probe_error_tag(exc)}")

        state["parked_run_id"] = run_id
        state["parked_at"] = now
        state["parked_goal_revision"] = revision
        state["parked_inbox_available"] = inbox_available
        self._write_stall_state(line.folder_id, state)

        blocker = self.blocker_summary(line.folder_id)
        return ParkOutcome(
            parked=True,
            event="established" if inbox_note is None else f"established:{inbox_note}",
            blocker=blocker,
            board_question=self._ask_board(line, record, blocker, state),
        )

    def _check_wake(
        self, line: LineSpec, record: dict[str, Any], state: dict[str, Any]
    ) -> ParkOutcome:
        # The inbox source is consulted only if the establishment probe found
        # it usable (`parked_inbox_available`), and its availability is *not*
        # re-assessed during the park: a source that was down when parking
        # began (a 403 is an ACL gap, not a blip) coming back mid-park is a
        # case not worth a per-tick probe against a known-broken endpoint --
        # the next parked terminal re-assesses it at establishment. A probe
        # that was available and errors mid-park skips this tick's inbox
        # check rather than waking: waking on a transient inbox error would
        # re-ignite a line whose goal.md anchor is still perfectly checkable.
        # Only the goal.md anchor failing -- the one fact parking stands on --
        # wakes conservatively.
        if line.alias and state["parked_inbox_available"] is not False:
            try:
                if self.wake is not None and self.wake.inbox_message_after(
                    line.alias, self._terminal_epoch(record)
                ):
                    return self._wake(line, state, "woken:inbox")
            except Exception:  # skip this source, the goal.md anchor still holds
                pass

        try:
            if self.wake is None:
                raise RuntimeError("scheduler has no wake signals configured")
            if self.wake.goal_revision(line.folder_id) != state["parked_goal_revision"]:
                return self._wake(line, state, "woken:goal_revision")
        except Exception as exc:  # fail open, by design
            return self._wake(line, state, f"woken:probe_failed:{probe_error_tag(exc)}")
        return ParkOutcome(parked=True, blocker=self.blocker_summary(line.folder_id))

    def _wake(self, line: LineSpec, state: dict[str, Any], event: str) -> ParkOutcome:
        """Clear the parking snapshot; the normal decide order takes over.

        `park_considered_run_id` deliberately survives: this terminal has had
        its one parking, and re-parking it (with, say, a freshly snapshotted
        goal revision) would swallow the very wake that just fired.
        """
        cleared = {
            **state,
            "parked_run_id": None,
            "parked_at": None,
            "parked_goal_revision": None,
            "parked_inbox_available": None,
        }
        self._write_stall_state(line.folder_id, cleared)
        return ParkOutcome(parked=False, event=event)

    def _ask_board(
        self,
        line: LineSpec,
        record: dict[str, Any],
        blocker: str | None,
        state: dict[str, Any],
    ) -> str | None:
        """Best-effort question note on the work board, once per parking.

        `work.note.v1` requires a ref to an *existing* board entity, and a
        goal line historically had none -- every ask 422'd with
        DERIVATION_ERROR ("ref target entity 'wf-…' not found"). So the first
        escalation *materialises* the line's card: one `work.card.v1` on
        board:work-index. The kind is entity_role='root' on the bus, which
        rejects a caller-chosen entity_id on the first publish (entity_id
        without supersedes is itself a DERIVATION_ERROR), so the entity id is
        whatever the bus derives -- the card message's own id. That id is
        persisted in the stall-state file (`board_card_entity_id`) and every
        later ask refs it. The card publish carries a stable idempotency key,
        so a lost state file re-yields the same card instead of a duplicate.

        Failure discipline is unchanged (#89): the board is telemetry and
        must not bite. A failed card publish degrades to log visibility and
        the question is not attempted -- it would 422 against the same
        missing entity. A failed question after a good card costs only the
        note. Parking itself is never affected either way.
        """
        if self.board is None:
            return None
        card_entity_id = state.get("board_card_entity_id")
        if not card_entity_id:
            try:
                card = self.board.publish_card(
                    goal_line_card_payload(folder_id=line.folder_id, title=line.folder_id),
                    idempotency_key=goal_line_card_key(line.folder_id),
                )
            except Exception as exc:  # telemetry must not bite
                return f"card_failed:{type(exc).__name__}:{str(exc)[:160]}"
            card_entity_id = card.entity_id
            state["board_card_entity_id"] = card_entity_id
            self._write_stall_state(line.folder_id, state)
        question = (
            f"line {line.folder_id} parked: blocked waiting on a human decision "
            f"(run {record['run_id']}). blocker: {blocker or 'see terminal.json'}"
        )
        try:
            ticket = self.board.ask(
                card_entity_id=card_entity_id,
                question=question,
                idempotency_key=f"parked:{line.folder_id}:{record['run_id']}",
            )
            state["board_question_note_id"] = ticket.question_note_id
            self._write_stall_state(line.folder_id, state)
            return f"question_sent:{ticket.question_note_id}"
        except Exception as exc:  # telemetry must not bite
            return f"question_failed:{type(exc).__name__}:{str(exc)[:160]}"

    def status_of(self, line: LineSpec) -> LineStatus:
        effective = self.effective_seat_for(line)
        return LineStatus(
            folder_id=line.folder_id,
            seat=effective,
            running=self.units.is_active(self.spec_for(line).unit_name),
            terminal=self.terminal_of(line.folder_id),
            last_start_at=self.last_start_of(line.folder_id),
        )

    def effective_seat_for(self, line: LineSpec) -> str:
        """The seat this line actually runs on: override wins, else roster.

        C4: the effective seat is the override's target while an override
        exists for the line, and the roster seat otherwise. Both the gateway
        probe and the launch must speak of this seat -- probing the roster seat
        while launching on the override would check the wrong face, and a
        refusal message that names the wrong seat sends the operator hunting in
        the wrong place.
        """
        return effective_seat(line.seat, self.seat_overrides.get(line.folder_id))

    def seat_triple(self, line: LineSpec) -> tuple[str, str | None, str]:
        """The C4 triple: (roster seat, override seat, effective seat)."""
        override = self.seat_overrides.get(line.folder_id)
        return (
            line.seat,
            override.to if override is not None else None,
            effective_seat(line.seat, override),
        )

    def reconcile_overrides(self) -> ReconcileResult:
        """C2/C3: fold converged overrides and surface the remaining drift loudly.

        Runs at the top of every tick. An override whose target now equals the
        roster seat (the roster PR merged and deployed) is folded away
        automatically -- a temporary state that has become permanent is no
        longer an override (C2). Every override that still differs from the
        roster is printed to stderr with its diff facts, on every tick it
        persists, so a long-running switch cannot rot silently (C3).

        An override surface that cannot be read reads as empty: the lines fall
        back to their roster seats, which is the safe reading of not knowing.
        """
        try:
            result = self.seat_overrides.reconcile(self._roster_seat)
        except OSError:
            return ReconcileResult()
        if result.drifting:
            print("seat override drift (roster ≠ effective):", file=sys.stderr)
            for folder_id, override, roster in result.drifting:
                print("  " + render_drift_line(folder_id, override, roster), file=sys.stderr)
        return result

    def gateway_healthy(self, seat: str) -> bool | None:
        """None means no answer, which `decide` treats as a refusal.

        A seat with no registered probe must not borrow another seat's health:
        that is how a dead upstream on one face passes for a live one.
        """
        if self.prober is None:
            self.probe_reasons[seat] = "this scheduler was started without a gateway prober"
            return None
        try:
            healthy = bool(self.prober.check(seat))
        except (UnknownSeat, MissingProbeCredential) as exc:
            # A missing credential is "no answer", not "red": the seat may be
            # perfectly healthy and we simply cannot ask. Either way `decide`
            # refuses, which is the safe reading of not knowing.
            self.probe_reasons[seat] = str(exc)
            return None
        return healthy

    # --- acting -----------------------------------------------------------

    def spec_for(self, line: LineSpec) -> LaunchSpec:
        # The effective (persisted) generation, not the roster's static one.
        # status_of probes is_active on this same spec's unit_name, so the
        # liveness check and the ignition always speak of the same unit -- a
        # probe on g1 while igniting g2 would miss a still-running previous
        # generation and double-start the line.
        return LaunchSpec(
            folder_id=line.folder_id,
            seat=self.effective_seat_for(line),
            generation=self.generation_of(line),
            alias=line.alias,
            max_rounds=line.max_rounds,
            run_root=self.config.run_root / line.folder_id,
            environment=self.line_environment(),
            acceptance_json=self.acceptance_json_for(line),
            noop_limit=line.noop_limit,
            timeout_limit=line.timeout_limit,
            # The board card the parking escalation already materialised (per
            # line, in the stall-state file). Threaded into the line process so
            # the E2 interrupt runtime reuses the existing card instead of
            # publishing a second one -- the two producers converge on one card.
            board_card_entity_id=str(
                self.stall_state(line.folder_id)["board_card_entity_id"] or ""
            ),
        )

    def acceptance_json_for(self, line: LineSpec) -> str | None:
        """The roster's acceptance declaration, serialised for the launcher.

        A declaration with commands but no cwd is still passed down: the
        line's acceptance step is the one place that refuses it, and it does
        so out loud (`skipped:no_cwd`) where the coordinator can see the
        declaration is incomplete. Dropping it here would turn a reviewable
        config mistake into a silent `not_declared`.
        """
        if not line.acceptance:
            return None
        return AcceptanceSpec(
            argvs=tuple(tuple(str(part) for part in argv) for argv in line.acceptance),
            cwd=line.acceptance_cwd,
            timeout_seconds=line.acceptance_timeout_seconds,
        ).to_cli_json()

    def line_environment(self) -> dict[str, str]:
        """A line must be able to run the executables the scheduler can.

        Transient units start from the user manager's environment, not the
        scheduler's, so PATH does not carry across on its own. agent-run is a
        bun script; without `~/.bun/bin` every line dies with
        `env: 'bun': No such file or directory` before it does any work.
        The unit defines the fleet's PATH; this passes it down.
        """
        env = {"PATH": os.environ.get("PATH", "")}
        env.update(self.config.extra_line_environment)
        return {k: v for k, v in env.items() if v}

    def unproductive_recent(self, now: float) -> int:
        """Zero-progress launches still inside the cap window.

        Pruning here rather than on append keeps the list correct even when the
        daemon sits idle for longer than the window.
        """
        cutoff = now - self.config.cap_window_seconds
        self.unproductive_launches = [t for t in self.unproductive_launches if t >= cutoff]
        return len(self.unproductive_launches)

    def tick(self) -> list[TickResult]:
        now = self.clock()
        stopped = self.maintenance_stop()
        results: list[TickResult] = []
        launched_this_tick = 0
        # C2/C3: fold converged overrides and print any remaining drift loudly,
        # before status_of/spec_for read the effective seat this tick.
        self.reconcile_overrides()

        for line in self.config.lines:
            # Accounting runs first: a terminal observed this tick must bump
            # the generation before status_of probes and spec_for launches.
            streak = self.account_last_run(line.folder_id, base_generation=line.generation)
            # Disabled lines skip the parking probes: no point spending two
            # network calls on a line decide would refuse anyway.
            park = self.park_state(line, now) if line.enabled else ParkOutcome()
            decision = decide(
                self.status_of(line),
                now=now,
                enabled=line.enabled,
                maintenance_stop=stopped,
                zero_progress_streak=streak,
                parked=park.parked,
                gateway_healthy=self.gateway_healthy(self.effective_seat_for(line)),
                unproductive_recent=self.unproductive_recent(now),
                cooldown_seconds=self.config.cooldown_seconds,
                total_cap=self.config.total_cap,
                cap_window_seconds=self.config.cap_window_seconds,
                backoff_cap_seconds=self.config.backoff_cap_seconds,
            )
            seat_roster, seat_override, seat_effective = self.seat_triple(line)
            result = TickResult(
                line.folder_id,
                decision,
                parked=park.parked,
                park_event=park.event,
                blocker=park.blocker,
                board_question=park.board_question,
                seat_roster=seat_roster,
                seat_override=seat_override,
                seat_effective=seat_effective,
            )
            if decision.refusal is Refusal.NO_PROBE:
                result.probe_detail = self.probe_reasons.get(self.effective_seat_for(line))
            if decision.ignite:
                if launched_this_tick and self.config.launch_stagger_seconds > 0:
                    # Between launches only -- never before the first, never
                    # after the last. A tick that starts nothing must not
                    # sleep at all.
                    self.sleep(self.config.launch_stagger_seconds)
                launched_this_tick += 1
                result.launch = self.launcher.launch(self.spec_for(line))
                # Counted on the attempt, not on success. A launch that fails
                # every time would otherwise never reach the cap it exists to
                # trip.
                self.total_started += 1
                if not result.launch.started or streak > 0:
                    # Evidence toward "something is systemically wrong", in the
                    # two shapes that actually mean it: the unit would not start
                    # at all, or the line it starts has run and got nowhere.
                    #
                    # The first disjunct is not optional. A bad release is the
                    # breaker's main case, and there the launch fails outright
                    # on a line whose streak is still 0 -- dropping it would
                    # switch the breaker off for exactly what it is for.
                    #
                    # What is deliberately *not* counted: a launch of a line
                    # that advanced a round last time. That is the fleet
                    # working, however much of it there is.
                    self.unproductive_launches.append(now)
                self.record_start(line.folder_id, now)
            results.append(result)
            if self.observe is not None:
                self.observe(result)
        if self.supervisor is not None:
            # The observer rides this tick -- no second loop anywhere (D9).
            # It reads the same terminal records this tick just read and the
            # results above; whatever it does, scheduling must survive it.
            with contextlib.suppress(Exception):
                self.supervisor.after_tick(
                    now=now,
                    folder_ids=[line.folder_id for line in self.config.lines],
                    terminal_reader=self.terminal_record,
                    tick_results=results,
                )
        return results

    def run_forever(self, *, sleep: Any = time.sleep, ticks: int | None = None) -> None:
        """Tick until told otherwise. `ticks` bounds it for tests and dry runs."""
        remaining = ticks
        while remaining is None or remaining > 0:
            self.tick()
            if remaining is not None:
                remaining -= 1
                if remaining == 0:
                    return
            sleep(self.config.interval_seconds)


def lines_from(entries: Iterable[dict[str, Any]]) -> list[LineSpec]:
    return [LineSpec(**entry) for entry in entries]


def bump_line_generation(run_root: Path, folder_id: str, base_generation: int) -> int:
    """Persist a generation bump so the next launch is a fresh thread.

    The set-seat surface calls this next to writing the override: cold-starting
    the new seat on a *new* generation is what makes the switch a clean handoff
    instead of a mid-thread seat change (step 7: 新 generation 以 override 座冷
    启动). It reads the same per-line stall-state file the scheduler owns, so
    the bump survives a daemon restart exactly like the streak does.

    Returns the generation the next launch should use. A missing or unreadable
    stall-state file bumps the roster base; an unreadable file is never a
    reason to refuse the seat switch itself.
    """
    path = Path(run_root) / ".scheduler" / f"{folder_id}.json"
    state: dict[str, Any] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            state = raw
    except (OSError, json.JSONDecodeError):
        pass
    try:
        current = int(state.get("generation") or 0)
    except (TypeError, ValueError):
        current = 0
    next_generation = max(current, base_generation) + 1
    state["generation"] = next_generation
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    except OSError:
        # Losing the bump costs a thread-id collision on the next launch, not
        # correctness of the override; the switch itself already happened.
        pass
    return next_generation


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MAINTENANCE_STOP",
    "LineSpec",
    "ParkOutcome",
    "Scheduler",
    "SchedulerConfig",
    "SystemdUnitProbe",
    "TickResult",
    "UnitProbe",
    "bump_line_generation",
    "lines_from",
]
