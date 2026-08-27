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
import json
import os
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

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

DEFAULT_MAINTENANCE_STOP = Path("/data/fleet-graph/maintenance-stop")
DEFAULT_INTERVAL_SECONDS = 60.0


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


@dataclass
class TickResult:
    folder_id: str
    decision: IgnitionDecision
    launch: LaunchResult | None = None
    probe_detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "folder_id": self.folder_id,
            "ignited": self.decision.ignite,
            "refusal": self.decision.refusal.value if self.decision.refusal else None,
            "detail": self.decision.detail,
        }
        if self.probe_detail is not None:
            record["probe_detail"] = self.probe_detail
        if self.launch is not None:
            record["unit"] = self.launch.unit_name
            record["launched"] = self.launch.started
            record["launch_detail"] = self.launch.detail
        return record


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

    @classmethod
    def from_json(cls, path: Path) -> SchedulerConfig:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            lines=[LineSpec(**entry) for entry in raw.get("lines", [])],
            run_root=Path(raw.get("run_root", "/data/fleet-graph/runs")),
            maintenance_stop_path=Path(raw.get("maintenance_stop", str(DEFAULT_MAINTENANCE_STOP))),
            interval_seconds=float(raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)),
            cooldown_seconds=float(raw.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
            total_cap=int(raw.get("total_cap", DEFAULT_TOTAL_CAP)),
            cap_window_seconds=float(raw.get("cap_window_seconds", DEFAULT_CAP_WINDOW_SECONDS)),
            # Was missing: the field existed with a default but the file was
            # never read, so setting it in config had no effect at all.
            launch_stagger_seconds=float(raw.get("launch_stagger_seconds", 45.0)),
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
    ) -> None:
        self.config = config
        self.prober = prober
        self.launcher = launcher or TransientLauncher()
        self.units = units or SystemdUnitProbe()
        self.clock = clock or time.time
        self.observe = observe
        self.sleep = sleep or time.sleep
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
        """The three mechanical fields of the last terminal, or None.

        `terminal`, `rounds` and `run_id` only -- all three are written by this
        engine's own pump. `reason` is deliberately not read: it is an agent's
        prose, and a scheduler that acted on it would be judging the work.
        """
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
        }

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
        empty = {"streak": 0, "accounted_run_id": None, "last_start_at": None, "generation": None}
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

    def status_of(self, line: LineSpec) -> LineStatus:
        return LineStatus(
            folder_id=line.folder_id,
            seat=line.seat,
            running=self.units.is_active(self.spec_for(line).unit_name),
            terminal=self.terminal_of(line.folder_id),
            last_start_at=self.last_start_of(line.folder_id),
        )

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
            seat=line.seat,
            generation=self.generation_of(line),
            max_rounds=line.max_rounds,
            run_root=self.config.run_root / line.folder_id,
            environment=self.line_environment(),
        )

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

        for line in self.config.lines:
            # Accounting runs first: a terminal observed this tick must bump
            # the generation before status_of probes and spec_for launches.
            streak = self.account_last_run(line.folder_id, base_generation=line.generation)
            decision = decide(
                self.status_of(line),
                now=now,
                enabled=line.enabled,
                maintenance_stop=stopped,
                zero_progress_streak=streak,
                gateway_healthy=self.gateway_healthy(line.seat),
                unproductive_recent=self.unproductive_recent(now),
                cooldown_seconds=self.config.cooldown_seconds,
                total_cap=self.config.total_cap,
                cap_window_seconds=self.config.cap_window_seconds,
                backoff_cap_seconds=self.config.backoff_cap_seconds,
            )
            result = TickResult(line.folder_id, decision)
            if decision.refusal is Refusal.NO_PROBE:
                result.probe_detail = self.probe_reasons.get(line.seat)
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


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MAINTENANCE_STOP",
    "LineSpec",
    "Scheduler",
    "SchedulerConfig",
    "SystemdUnitProbe",
    "TickResult",
    "UnitProbe",
    "lines_from",
]
