"""Should this line be started right now?

The babysitter's gate logic, lifted out of the bare script and made testable.
Every rule here was added because something went wrong without it, so each one
carries the reason it exists rather than just the condition.

The decision is a pure function of observable state. Nothing here starts a
process; `IgnitionDecision` is handed to whatever does, which keeps the policy
reviewable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Per-line cooldown: a line that just restarted is given room to fail slowly
# rather than being hammered.
DEFAULT_COOLDOWN_SECONDS = 300

# Global circuit breaker across all lines. Without it a systemic fault (a dead
# gateway, a bad release) turns into an unbounded restart storm.
DEFAULT_TOTAL_CAP = 60


class Refusal(StrEnum):
    MAINTENANCE_STOP = "maintenance_stop"
    ALREADY_RUNNING = "already_running"
    TERMINAL_DONE = "terminal_done"
    COOLING_DOWN = "cooling_down"
    TOTAL_CAP_REACHED = "total_cap_reached"
    GATEWAY_RED = "gateway_red"
    NO_PROBE = "no_probe"


@dataclass(frozen=True)
class LineStatus:
    """What the scheduler can observe about one line without starting it."""

    folder_id: str
    seat: str
    running: bool = False
    terminal: str | None = None
    last_start_at: float | None = None


@dataclass(frozen=True)
class IgnitionDecision:
    ignite: bool
    refusal: Refusal | None = None
    detail: str = ""

    @property
    def refused(self) -> bool:
        return not self.ignite


def decide(
    status: LineStatus,
    *,
    now: float,
    maintenance_stop: bool,
    gateway_healthy: bool | None,
    total_started: int,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    total_cap: int = DEFAULT_TOTAL_CAP,
) -> IgnitionDecision:
    """Decide whether to ignite `status`, in the babysitter's order.

    Order is not arbitrary: the cheap, certain refusals come first so a stopped
    fleet or an already-running line never costs a gateway probe.

    `gateway_healthy` is None when no probe could be run for this seat. That is
    a refusal, not a pass -- see scheduler/probe.py.
    """
    if maintenance_stop:
        return IgnitionDecision(False, Refusal.MAINTENANCE_STOP, "maintenance-stop is in effect")

    if status.running:
        # The pgrep guard. Igniting a second pump for one line gives two
        # processes the same work folder and the same worker seat.
        return IgnitionDecision(
            False, Refusal.ALREADY_RUNNING, f"{status.folder_id} is already running"
        )

    if status.terminal == "done":
        # A finished line stays finished. Restarting it would re-run work that
        # already passed acceptance.
        return IgnitionDecision(False, Refusal.TERMINAL_DONE, f"{status.folder_id} terminated done")

    if status.last_start_at is not None and now - status.last_start_at < cooldown_seconds:
        remaining = cooldown_seconds - (now - status.last_start_at)
        return IgnitionDecision(False, Refusal.COOLING_DOWN, f"{remaining:.0f}s of cooldown left")

    if total_started >= total_cap:
        return IgnitionDecision(
            False,
            Refusal.TOTAL_CAP_REACHED,
            f"global cap {total_cap} reached; a systemic fault is more likely than "
            f"{total_cap} independent ones",
        )

    if gateway_healthy is None:
        return IgnitionDecision(
            False, Refusal.NO_PROBE, f"no probe registered for seat {status.seat!r}"
        )

    if not gateway_healthy:
        # Igniting into a dead upstream burns the line's bounds on failures
        # that have nothing to do with the work.
        return IgnitionDecision(
            False, Refusal.GATEWAY_RED, f"gateway probe red for seat {status.seat!r}"
        )

    return IgnitionDecision(True)


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_TOTAL_CAP",
    "IgnitionDecision",
    "LineStatus",
    "Refusal",
    "decide",
]
