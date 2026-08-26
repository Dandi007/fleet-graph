"""The resident scheduler: the babysitter, in git and under test.

`ignition.decide` already holds the judgement and `launcher` already knows how
to start a line in its own cgroup. What was missing is the part that runs all
day: look at each line, ask, and either start it or record why not.

Two things it deliberately does not do.

**It does not decide.** Every refusal comes from `decide()`, in the order that
function fixes. A scheduler that grew its own conditions would be a second
description of when a line may run, and the first thing to drift.

**It does not interpret a line's work.** It reads three observable facts --
is a unit up, did the line write a terminal, when did we last start it -- and
nothing else. Whether the work is going well is the coordinator's business,
and reading round contents here is how an orchestrator turns into a second,
unaccountable judge (INV-3).

The maintenance-stop gate keeps the switch `/data/ronin/maintenance-stop` has
been, **including its expiry**: babysitter v23 made `expires_at` mandatory and
an expired flag inert (a 2026-08-23 ruling). Reading only `path.exists()` looks
like the same gate and is not -- an expired flag nobody cleaned up would hold
the fleet down forever, and the operator's muscle memory ("it expired, so we
are free") is exactly what would break.

One deliberate divergence: a flag we cannot parse gates the fleet rather than
opening it. The old gate treated a malformed file as absent; a gate file that
is broken is an operator error worth stopping on, not worth silently ignoring.
"""

from __future__ import annotations

import calendar
import json
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from fleet_graph.scheduler.ignition import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_TOTAL_CAP,
    IgnitionDecision,
    LineStatus,
    decide,
)
from fleet_graph.scheduler.launcher import LaunchResult, LaunchSpec, TransientLauncher
from fleet_graph.scheduler.probe import (
    GatewayProber,
    MissingProbeCredential,
    UnknownSeat,
)

DEFAULT_MAINTENANCE_STOP = Path("/data/ronin/maintenance-stop")
DEFAULT_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class LineSpec:
    """One line the scheduler is responsible for."""

    folder_id: str
    seat: str
    max_rounds: int = 10
    alias: str | None = None
    generation: int = 1


@dataclass
class TickResult:
    folder_id: str
    decision: IgnitionDecision
    launch: LaunchResult | None = None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "folder_id": self.folder_id,
            "ignited": self.decision.ignite,
            "refusal": self.decision.refusal.value if self.decision.refusal else None,
            "detail": self.decision.detail,
        }
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

    def terminal_of(self, folder_id: str) -> str | None:
        """What the line last wrote, or None if it never terminated.

        Only the `terminal` field is read. Anything else in that file is the
        line's own account of its work, and reading it here would be the
        orchestrator forming an opinion it has no standing to form.
        """
        path = self.config.run_root / folder_id / "terminal.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = record.get("terminal")
        return str(value) if value else None

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
            return None
        try:
            return bool(self.prober.check(seat))
        except (UnknownSeat, MissingProbeCredential):
            # A missing credential is "no answer", not "red": the seat may be
            # perfectly healthy and we simply cannot ask. Either way `decide`
            # refuses, which is the safe reading of not knowing.
            return None

    # --- acting -----------------------------------------------------------

    def spec_for(self, line: LineSpec) -> LaunchSpec:
        return LaunchSpec(
            folder_id=line.folder_id,
            seat=line.seat,
            generation=line.generation,
            max_rounds=line.max_rounds,
            run_root=self.config.run_root / line.folder_id,
        )

    def tick(self) -> list[TickResult]:
        now = self.clock()
        stopped = self.maintenance_stop()
        results: list[TickResult] = []

        for line in self.config.lines:
            decision = decide(
                self.status_of(line),
                now=now,
                maintenance_stop=stopped,
                gateway_healthy=self.gateway_healthy(line.seat),
                total_started=self.total_started,
                cooldown_seconds=self.config.cooldown_seconds,
                total_cap=self.config.total_cap,
            )
            result = TickResult(line.folder_id, decision)
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
