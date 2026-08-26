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
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS
    #: Extra environment handed to every line, on top of the scheduler's PATH.
    extra_line_environment: dict[str, str] = field(default_factory=dict)

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
    ) -> None:
        self.config = config
        self.prober = prober
        self.launcher = launcher or TransientLauncher()
        self.units = units or SystemdUnitProbe()
        self.clock = clock or time.time
        self.observe = observe
        self.total_started = 0
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
        try:
            state = json.loads(self._stall_path(folder_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"streak": 0, "accounted_run_id": None}
        if not isinstance(state, dict):
            return {"streak": 0, "accounted_run_id": None}
        return {
            "streak": int(state.get("streak") or 0),
            "accounted_run_id": state.get("accounted_run_id"),
        }

    def _write_stall_state(self, folder_id: str, state: dict[str, Any]) -> None:
        path = self._stall_path(folder_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        except OSError:
            # Losing the counter costs an extra attempt, not correctness.
            pass

    def account_last_run(self, folder_id: str) -> int:
        """Fold the newest terminal into the stall streak, once.

        Persisted rather than kept in memory because the daemon restarts on
        every release, and a counter that resets on deploy would let a stuck
        line go back to full speed each time we ship -- exactly when someone is
        least likely to be watching it.

        A terminal is accounted at most once, keyed on the run id that wrote
        it, so re-reading the same file on every 60s tick does not inflate the
        streak.
        """
        state = self.stall_state(folder_id)
        record = self.terminal_record(folder_id)
        if record is None:
            return int(state["streak"])
        run_id = record.get("run_id")
        if run_id is None or run_id == state["accounted_run_id"]:
            return int(state["streak"])

        advanced = int(record.get("rounds") or 0) > 0
        finished = record.get("terminal") == "done"
        streak = 0 if (advanced or finished) else int(state["streak"]) + 1
        self._write_stall_state(folder_id, {"streak": streak, "accounted_run_id": run_id})
        return streak

    def status_of(self, line: LineSpec) -> LineStatus:
        return LineStatus(
            folder_id=line.folder_id,
            seat=line.seat,
            running=self.units.is_active(self.spec_for(line).unit_name),
            terminal=self.terminal_of(line.folder_id),
            last_start_at=self.last_start_at.get(line.folder_id),
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
        return LaunchSpec(
            folder_id=line.folder_id,
            seat=line.seat,
            generation=line.generation,
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

    def tick(self) -> list[TickResult]:
        now = self.clock()
        stopped = self.maintenance_stop()
        results: list[TickResult] = []

        for line in self.config.lines:
            decision = decide(
                self.status_of(line),
                now=now,
                enabled=line.enabled,
                maintenance_stop=stopped,
                zero_progress_streak=self.account_last_run(line.folder_id),
                gateway_healthy=self.gateway_healthy(line.seat),
                total_started=self.total_started,
                cooldown_seconds=self.config.cooldown_seconds,
                total_cap=self.config.total_cap,
                backoff_cap_seconds=self.config.backoff_cap_seconds,
            )
            result = TickResult(line.folder_id, decision)
            if decision.refusal is Refusal.NO_PROBE:
                result.probe_detail = self.probe_reasons.get(line.seat)
            if decision.ignite:
                result.launch = self.launcher.launch(self.spec_for(line))
                # Counted on the attempt, not on success. A launch that fails
                # every time would otherwise never reach the cap it exists to
                # trip.
                self.total_started += 1
                self.last_start_at[line.folder_id] = now
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
