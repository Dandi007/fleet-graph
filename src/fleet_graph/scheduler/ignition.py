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
#: The window the global cap counts over. The cap asks "are this many launches
#: plausibly independent faults?", and that question only has an answer over a
#: span of time -- a cumulative count on a long-lived daemon reaches any cap
#: eventually, which makes it a timer rather than a detector.
DEFAULT_CAP_WINDOW_SECONDS = 3600.0

# A line that keeps terminating without advancing a single round is not going
# to be fixed by starting it again five minutes later. Each repeat doubles the
# wait, so a genuinely stuck line costs a handful of attempts a day instead of
# 288, while a blocker that does clear still gets picked up on its own.
DEFAULT_BACKOFF_CAP_SECONDS = 6 * 3600


class Refusal(StrEnum):
    LINE_DISABLED = "line_disabled"
    NO_PROGRESS = "no_progress"
    MAINTENANCE_STOP = "maintenance_stop"
    ALREADY_RUNNING = "already_running"
    TERMINAL_DONE = "terminal_done"
    PARKED_AWAITING_DECISION = "parked_awaiting_decision"
    COOLING_DOWN = "cooling_down"
    TOTAL_CAP_REACHED = "total_cap_reached"
    GATEWAY_RED = "gateway_red"
    NO_PROBE = "no_probe"


def backoff_seconds(
    base: float,
    zero_progress_streak: int,
    cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
) -> float:
    """How long to wait before the next attempt, given a stall streak.

    Doubling rather than latching, deliberately. A hard stop after N repeats
    would also stop the case this has to keep working for: a blocker that
    clears on its own (a service comes back, someone answers the question).
    A latched line needs a human to notice and unlatch it, which is the same
    manual bookkeeping the old fleet did by hand. Backoff needs nobody.
    """
    if zero_progress_streak <= 0:
        return base
    return min(base * (2**zero_progress_streak), cap)


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
    enabled: bool,
    maintenance_stop: bool,
    gateway_healthy: bool | None,
    unproductive_recent: int,
    zero_progress_streak: int,
    parked: bool = False,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    total_cap: int = DEFAULT_TOTAL_CAP,
    cap_window_seconds: float = DEFAULT_CAP_WINDOW_SECONDS,
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
    #: M5: a valid revoke record matching this line's `done` terminal cleared
    #: the done latch for this tick. The done branch is the *only* branch this
    #: flag touches: with it set, `done` falls through to the normal gates
    #: instead of refusing, so the line can cold-start on a fresh generation.
    #: The daemon is the only caller that sets it, and only after the mechanical
    #: revoke match described in scheduler/revive.py. Without it, `done` stays
    #: final (Refusal.TERMINAL_DONE) -- there is no "silent pass" path.
    revived: bool = False,
) -> IgnitionDecision:
    """Decide whether to ignite `status`, in the babysitter's order.

    Order is not arbitrary: the cheap, certain refusals come first so a stopped
    fleet or an already-running line never costs a gateway probe.

    `gateway_healthy` is None when no probe could be run for this seat. That is
    a refusal, not a pass -- see scheduler/probe.py.

    `enabled` has no default on purpose. Whether a line may run is the one
    thing no call site should be able to leave unsaid.
    """
    if not enabled:
        # A line runs because the config says this one runs, not because the
        # fleet as a whole is unpaused. Batched rollout is the immediate
        # reason (one canary, then five, then all), but the durable one is
        # that a roster which opts each line in has no state where "nobody
        # decided" means "everybody runs".
        return IgnitionDecision(
            False, Refusal.LINE_DISABLED, f"{status.folder_id} is not enabled in the line config"
        )

    if maintenance_stop:
        return IgnitionDecision(False, Refusal.MAINTENANCE_STOP, "maintenance-stop is in effect")

    if status.running:
        # The pgrep guard. Igniting a second pump for one line gives two
        # processes the same work folder and the same worker seat.
        return IgnitionDecision(
            False, Refusal.ALREADY_RUNNING, f"{status.folder_id} is already running"
        )

    if status.terminal == "done" and not revived:
        # A finished line stays finished. Restarting it would re-run work that
        # already passed acceptance.
        return IgnitionDecision(False, Refusal.TERMINAL_DONE, f"{status.folder_id} terminated done")
    # M5: a legitimate revoke (see scheduler/revive.py) can clear the done
    # latch for this tick (revived=True), so a `done` terminal falls through to
    # the remaining gates below and the line cold-starts on a fresh generation.
    # Every other refusal below still applies in the unchanged order.

    if parked:
        # Before the backoff branch on purpose. A line whose last terminal was
        # blocked waiting on a *human decision* is not going to be unblocked by
        # trying again on a timer: every backoff-paced relaunch re-derives the
        # same blockage at full coordinator cost. The scheduler computes
        # `parked` from mechanical facts only (terminal waiting_on=decision and
        # no wake fact -- see daemon.py), and any failure to probe those facts
        # comes in here as parked=False, falling through to plain backoff:
        # parking saves money, it must never be able to lock a line shut.
        return IgnitionDecision(
            False,
            Refusal.PARKED_AWAITING_DECISION,
            f"{status.folder_id} is blocked waiting on a human decision; "
            "parked until a wake fact appears (inbox message, goal.md change, "
            "or the parked fields are cleared from its stall-state file)",
        )

    wait = backoff_seconds(cooldown_seconds, zero_progress_streak, backoff_cap_seconds)
    if status.last_start_at is not None and now - status.last_start_at < wait:
        remaining = wait - (now - status.last_start_at)
        if zero_progress_streak > 0:
            # A separate label on purpose. "cooling down" reads as "it just
            # ran"; this line has run and got nowhere several times, and an
            # operator scanning the log should be able to tell those apart
            # without doing arithmetic on timestamps.
            return IgnitionDecision(
                False,
                Refusal.NO_PROGRESS,
                f"{status.folder_id} ended {zero_progress_streak} run(s) in a row without "
                f"advancing a round; backing off, {remaining:.0f}s left of {wait:.0f}s",
            )
        return IgnitionDecision(False, Refusal.COOLING_DOWN, f"{remaining:.0f}s of cooldown left")

    if unproductive_recent >= total_cap:
        # Counts zero-progress launches only, and only within a window. The
        # breaker's own sentence is "this many *independent faults* is less
        # likely than one systemic fault" -- so a launch that advanced a round
        # is not evidence for it, and neither is one from six hours ago. The
        # first spelling counted every launch for the daemon's whole lifetime,
        # which made a healthy fleet trip it on a schedule: one line that ran
        # 25 rounds and reached its goal contributed 23 of the 60.
        return IgnitionDecision(
            False,
            Refusal.TOTAL_CAP_REACHED,
            f"{unproductive_recent} zero-progress launches in the last "
            f"{cap_window_seconds / 60:.0f}min reached the cap of {total_cap}; "
            f"a systemic fault is more likely than {total_cap} independent ones",
        )

    if gateway_healthy is None:
        # Deliberately cause-neutral. `None` means "could not ask", and there
        # are three ways to get there: no prober configured, no probe
        # registered for the seat, no credential for its lane. The old text
        # named only the middle one, which sent the reader looking at a
        # registry that was fine while an unloaded env file went unnoticed.
        # The caller knows which one it was; see Scheduler.gateway_healthy.
        return IgnitionDecision(
            False,
            Refusal.NO_PROBE,
            f"gateway health for seat {status.seat!r} could not be determined",
        )

    if not gateway_healthy:
        # Igniting into a dead upstream burns the line's bounds on failures
        # that have nothing to do with the work.
        return IgnitionDecision(
            False, Refusal.GATEWAY_RED, f"gateway probe red for seat {status.seat!r}"
        )

    return IgnitionDecision(True)


__all__ = [
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "DEFAULT_CAP_WINDOW_SECONDS",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_TOTAL_CAP",
    "IgnitionDecision",
    "LineStatus",
    "Refusal",
    "backoff_seconds",
    "decide",
]
