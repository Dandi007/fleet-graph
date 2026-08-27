"""The resident scheduler: what it observes, and what it refuses to decide."""

from __future__ import annotations

import calendar
import json
import time
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.scheduler.daemon import (
    LineSpec,
    Scheduler,
    SchedulerConfig,
    lines_from,
)
from fleet_graph.scheduler.ignition import Refusal
from fleet_graph.scheduler.launcher import LaunchResult

_DEFAULT = object()


class FakeUnits:
    def __init__(self, active: set[str] | None = None) -> None:
        self.active = active or set()
        self.asked: list[str] = []

    def is_active(self, unit_name: str) -> bool:
        self.asked.append(unit_name)
        return unit_name in self.active


class FakeLauncher:
    def __init__(self, started: bool = True) -> None:
        self.started = started
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        return LaunchResult(spec.unit_name, self.started, "")


class FakeProber:
    def __init__(self, healthy: bool | Exception = True) -> None:
        self.healthy = healthy
        self.asked: list[str] = []

    def check(self, seat: str) -> bool:
        self.asked.append(seat)
        if isinstance(self.healthy, Exception):
            raise self.healthy
        return self.healthy


def make(
    tmp_path: Path,
    *,
    lines: list[LineSpec] | None = None,
    units: FakeUnits | None = None,
    launcher: FakeLauncher | None = None,
    prober: Any = _DEFAULT,
    now: float = 1000.0,
    slept: list[float] | None = None,
    **config: Any,
) -> Scheduler:
    return Scheduler(
        SchedulerConfig(
            lines=lines or [LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", enabled=True)],
            run_root=tmp_path / "runs",
            maintenance_stop_path=tmp_path / "maintenance-stop",
            **config,
        ),
        prober=FakeProber() if prober is _DEFAULT else prober,
        launcher=launcher or FakeLauncher(),
        units=units or FakeUnits(),
        clock=lambda: now,
        sleep=(slept.append if slept is not None else (lambda _s: None)),
    )


def write_terminal(tmp_path: Path, folder_id: str, terminal: str) -> None:
    path = tmp_path / "runs" / folder_id / "terminal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"terminal": terminal, "rounds": 3}), encoding="utf-8")


class TestItIgnitesWhenNothingRefuses:
    def test_a_clean_line_starts(self, tmp_path: Path) -> None:
        launcher = FakeLauncher()
        results = make(tmp_path, launcher=launcher).tick()

        assert [r.decision.ignite for r in results] == [True]
        assert len(launcher.launched) == 1
        assert launcher.launched[0].folder_id == "wf-1"

    def test_the_launch_gets_its_own_run_root(self, tmp_path: Path) -> None:
        launcher = FakeLauncher()
        make(tmp_path, launcher=launcher).tick()
        assert launcher.launched[0].run_root == tmp_path / "runs" / "wf-1"


class TestEveryRefusalComesFromDecide:
    def test_the_gate_file_stops_the_fleet(self, tmp_path: Path) -> None:
        (tmp_path / "maintenance-stop").write_text("")
        launcher = FakeLauncher()
        results = make(tmp_path, launcher=launcher).tick()

        assert results[0].decision.refusal is Refusal.MAINTENANCE_STOP
        assert launcher.launched == []

    def test_a_running_unit_is_not_started_twice(self, tmp_path: Path) -> None:
        scheduler = make(tmp_path)
        unit = scheduler.spec_for(scheduler.config.lines[0]).unit_name
        scheduler.units = FakeUnits({unit})

        assert scheduler.tick()[0].decision.refusal is Refusal.ALREADY_RUNNING

    def test_a_line_that_finished_stays_finished(self, tmp_path: Path) -> None:
        write_terminal(tmp_path, "wf-1", "done")
        assert make(tmp_path).tick()[0].decision.refusal is Refusal.TERMINAL_DONE

    def test_a_blocked_line_may_be_restarted(self, tmp_path: Path) -> None:
        """Only `done` is final; blocked lines are the ones a restart helps."""
        write_terminal(tmp_path, "wf-1", "blocked")
        assert make(tmp_path).tick()[0].decision.ignite

    def test_a_just_started_line_cools_down(self, tmp_path: Path) -> None:
        scheduler = make(tmp_path, cooldown_seconds=600)
        assert scheduler.tick()[0].decision.ignite
        assert scheduler.tick()[0].decision.refusal is Refusal.COOLING_DOWN

    def test_the_total_cap_trips(self, tmp_path: Path) -> None:
        """Launches that produce nothing trip it, and the daemon surfaces the
        refusal decide returned rather than inventing its own."""
        lines = [
            LineSpec(folder_id=f"wf-{i}", seat="opencode-dsv4pro", enabled=True) for i in range(4)
        ]
        scheduler = make(tmp_path, lines=lines, launcher=FakeLauncher(started=False), total_cap=2)
        refusals = [r.decision.refusal for r in scheduler.tick()]
        assert refusals == [None, None, Refusal.TOTAL_CAP_REACHED, Refusal.TOTAL_CAP_REACHED]

    def test_four_healthy_launches_do_not_trip_a_cap_of_two(self, tmp_path: Path) -> None:
        """The behaviour change, stated as a guarantee: the cap counts faults,
        so a fleet that is merely busy can never reach it. Before this, four
        working lines and a cap of two meant two of them were refused for
        looking like a restart storm."""
        lines = [
            LineSpec(folder_id=f"wf-{i}", seat="opencode-dsv4pro", enabled=True) for i in range(4)
        ]
        refusals = [r.decision.refusal for r in make(tmp_path, lines=lines, total_cap=2).tick()]
        assert refusals == [None, None, None, None]

    def test_a_failed_launch_still_counts_against_the_cap(self, tmp_path: Path) -> None:
        """Otherwise a launch that fails every time never trips the cap that
        exists to catch exactly that."""
        lines = [
            LineSpec(folder_id=f"wf-{i}", seat="opencode-dsv4pro", enabled=True) for i in range(3)
        ]
        results = make(
            tmp_path, lines=lines, launcher=FakeLauncher(started=False), total_cap=2
        ).tick()
        assert results[2].decision.refusal is Refusal.TOTAL_CAP_REACHED

    def test_a_red_gateway_stops_ignition(self, tmp_path: Path) -> None:
        results = make(tmp_path, prober=FakeProber(healthy=False)).tick()
        assert results[0].decision.refusal is Refusal.GATEWAY_RED


class TestNotKnowingIsARefusal:
    def test_no_prober_at_all_refuses(self, tmp_path: Path) -> None:
        assert make(tmp_path, prober=None).tick()[0].decision.refusal is Refusal.NO_PROBE

    def test_an_unregistered_seat_never_borrows_another_seats_health(self, tmp_path: Path) -> None:
        """A seat probing the wrong face is how a dead upstream passes for live."""
        from fleet_graph.scheduler.probe import UnknownSeat

        results = make(tmp_path, prober=FakeProber(healthy=UnknownSeat("nope"))).tick()
        assert results[0].decision.refusal is Refusal.NO_PROBE

    def test_a_missing_credential_is_no_answer_not_a_red_light(self, tmp_path: Path) -> None:
        """The seat may be perfectly healthy; we simply cannot ask."""
        from fleet_graph.scheduler.probe import MissingProbeCredential

        results = make(
            tmp_path, prober=FakeProber(healthy=MissingProbeCredential("no token"))
        ).tick()
        assert results[0].decision.refusal is Refusal.NO_PROBE


class TestItReadsOnlyWhatItNeeds:
    def test_a_missing_terminal_file_is_not_a_terminal(self, tmp_path: Path) -> None:
        assert make(tmp_path).terminal_of("wf-1") is None

    def test_an_unreadable_terminal_file_is_not_a_terminal(self, tmp_path: Path) -> None:
        path = tmp_path / "runs" / "wf-1" / "terminal.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert make(tmp_path).terminal_of("wf-1") is None

    def test_it_reads_counts_but_never_the_lines_prose(self, tmp_path: Path) -> None:
        """INV-3, restated where the real line is.

        This started as "only `terminal` is read". The stall guard needs
        `rounds` and `run_id` too, and widening the assertion to admit them is
        not a loosening: all three are numbers and ids this engine's own pump
        writes. What must stay out is `reason` -- prose an agent wrote. A
        scheduler that keyed on that would be judging the work, and would also
        be wrong: the canary blocked twice on the same missing data source and
        reworded it to a bigram similarity of 0.28.
        """
        import re

        from fleet_graph.scheduler import daemon

        source = Path(daemon.__file__).read_text(encoding="utf-8")
        reads = set(re.findall(r'record\.get\("([a-z_]+)"\)', source))
        assert reads <= {"terminal", "rounds", "run_id"}, reads
        assert "reason" not in reads


def write_terminal_record(
    tmp_path: Path, folder_id: str, terminal: str, rounds: int, run_id: str
) -> None:
    path = tmp_path / "runs" / folder_id / "terminal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "terminal": terminal,
                "rounds": rounds,
                "run_id": run_id,
                "reason": "prose the scheduler must not act on",
            }
        ),
        encoding="utf-8",
    )


class TestTheStallStreak:
    """These seed the bookkeeping with `record_start` before writing a
    terminal, because that is the real sequence: the scheduler writes the
    state file when it launches, so by the time a terminal from that launch
    appears the file always exists. Writing a terminal with no bookkeeping at
    all means something else produced it -- see
    `test_a_terminal_from_before_we_watched_is_not_our_streak`.
    """

    def test_a_fruitless_run_counts(self, tmp_path: Path) -> None:
        scheduler = make(tmp_path)
        scheduler.record_start("wf-1", 1000.0)
        write_terminal_record(tmp_path, "wf-1", "blocked", 0, "run-1")
        assert scheduler.account_last_run("wf-1") == 1

    def test_the_same_terminal_is_counted_once(self, tmp_path: Path) -> None:
        """The scheduler re-reads this file every 60s. Counting per read would
        put a line into a six-hour backoff within minutes of one bad run."""
        scheduler = make(tmp_path)
        scheduler.record_start("wf-1", 1000.0)
        write_terminal_record(tmp_path, "wf-1", "blocked", 0, "run-1")
        assert [scheduler.account_last_run("wf-1") for _ in range(5)] == [1, 1, 1, 1, 1]

    def test_consecutive_fruitless_runs_accumulate(self, tmp_path: Path) -> None:
        scheduler = make(tmp_path)
        scheduler.record_start("wf-1", 1000.0)
        for n in (1, 2, 3):
            write_terminal_record(tmp_path, "wf-1", "blocked", 0, f"run-{n}")
            assert scheduler.account_last_run("wf-1") == n

    def test_a_run_that_advanced_clears_the_streak(self, tmp_path: Path) -> None:
        """Progress is progress even if the line then blocked. The guard is
        about lines going nowhere, not about lines ending unhappily."""
        scheduler = make(tmp_path)
        scheduler.record_start("wf-1", 1000.0)
        write_terminal_record(tmp_path, "wf-1", "blocked", 0, "run-1")
        scheduler.account_last_run("wf-1")
        write_terminal_record(tmp_path, "wf-1", "blocked", 2, "run-2")
        assert scheduler.account_last_run("wf-1") == 0

    def test_finishing_clears_the_streak(self, tmp_path: Path) -> None:
        scheduler = make(tmp_path)
        scheduler.record_start("wf-1", 1000.0)
        write_terminal_record(tmp_path, "wf-1", "blocked", 0, "run-1")
        scheduler.account_last_run("wf-1")
        write_terminal_record(tmp_path, "wf-1", "done", 0, "run-2")
        assert scheduler.account_last_run("wf-1") == 0

    def test_the_streak_survives_a_restart(self, tmp_path: Path) -> None:
        """The daemon restarts on every release. A counter that reset on deploy
        would send a stuck line back to full speed exactly when we ship --
        which is when nobody is watching that line."""
        make(tmp_path).record_start("wf-1", 1000.0)
        write_terminal_record(tmp_path, "wf-1", "blocked", 0, "run-1")
        make(tmp_path).account_last_run("wf-1")
        write_terminal_record(tmp_path, "wf-1", "blocked", 0, "run-2")
        assert make(tmp_path).account_last_run("wf-1") == 2, "streak was kept in memory"

    def test_deleting_the_counter_file_really_resets(self, tmp_path: Path) -> None:
        """The runbook's escape hatch has to mean what it says.

        `docs/operating.md` tells an operator to delete the counter file to
        make a backed-off line retry now. Without a baseline step the very
        next tick re-reads the same terminal -- already counted once, before
        the delete -- and the streak comes back as 1 instead of 0. Observed
        for real after clearing the canary's backoff by hand.
        """
        scheduler = make(tmp_path)
        scheduler.record_start("wf-1", 1000.0)
        for n in (1, 2, 3):
            write_terminal_record(tmp_path, "wf-1", "blocked", 0, f"run-{n}")
            scheduler.account_last_run("wf-1")
        scheduler._stall_path("wf-1").unlink()
        assert scheduler.account_last_run("wf-1") == 0
        # And it stays 0: the adopted terminal must not be counted later either.
        assert scheduler.account_last_run("wf-1") == 0

    def test_a_terminal_from_before_we_watched_is_not_our_streak(self, tmp_path: Path) -> None:
        """One terminal is not a streak. A line whose last run failed long
        before this scheduler existed starts at zero, not at one."""
        write_terminal_record(tmp_path, "wf-1", "blocked", 0, "ancient")
        assert make(tmp_path).account_last_run("wf-1") == 0

    def test_the_baseline_does_not_swallow_the_next_failure(self, tmp_path: Path) -> None:
        """Adopting a terminal must not make the guard blind afterwards."""
        write_terminal_record(tmp_path, "wf-1", "blocked", 0, "ancient")
        scheduler = make(tmp_path)
        scheduler.account_last_run("wf-1")
        write_terminal_record(tmp_path, "wf-1", "blocked", 0, "next")
        assert scheduler.account_last_run("wf-1") == 1

    def test_no_terminal_yet_is_not_a_stall(self, tmp_path: Path) -> None:
        assert make(tmp_path).account_last_run("wf-1") == 0

    def test_the_streak_reaches_the_decision(self, tmp_path: Path) -> None:
        """A counter nothing consults is a counter that looks like a working
        guard right up until the line it should have held restarts anyway."""
        scheduler = make(tmp_path, now=1000.0)
        scheduler.record_start("wf-1", 1000.0 - 400)
        for n in (1, 2, 3):
            write_terminal_record(tmp_path, "wf-1", "blocked", 0, f"run-{n}")
            scheduler.account_last_run("wf-1")
        assert scheduler.tick()[0].decision.refusal is Refusal.NO_PROGRESS


class TestARestartDoesNotHandOutAFreeLaunch:
    """Observed on the real fleet, not derived from the code.

    The stall streak was persisted precisely because the daemon restarts on
    every release -- but `last_start_at` was left in memory, and `decide`
    skips the entire cooldown branch when there is no start time to measure
    from. So a release at 22:13 re-ignited a line that had already earned a
    streak of 2. The counter survived; the timestamp it multiplies did not.
    Half a fix looks exactly like a whole one until you watch the machine.
    """

    def test_the_start_time_outlives_the_process(self, tmp_path: Path) -> None:
        first = make(tmp_path, now=1000.0)
        first.tick()
        assert make(tmp_path).last_start_of("wf-1") == 1000.0

    def test_a_fresh_daemon_still_honours_the_cooldown(self, tmp_path: Path) -> None:
        make(tmp_path, now=1000.0).tick()
        launcher = FakeLauncher()
        after = make(tmp_path, now=1000.0 + 60, launcher=launcher)
        assert after.tick()[0].decision.refusal is Refusal.COOLING_DOWN
        assert launcher.launched == []

    def test_a_fresh_daemon_still_honours_the_backoff(self, tmp_path: Path) -> None:
        make(tmp_path, now=1000.0).tick()
        for n in (1, 2):
            write_terminal_record(tmp_path, "wf-1", "blocked", 0, f"run-{n}")
            make(tmp_path).account_last_run("wf-1")
        # 300 * 2**2 = 1200s of backoff earned; 700s in, a restart must not
        # shorten it.
        after = make(tmp_path, now=1000.0 + 700)
        assert after.tick()[0].decision.refusal is Refusal.NO_PROGRESS

    def test_accounting_a_terminal_does_not_erase_the_start_time(self, tmp_path: Path) -> None:
        """The two facts share one file; writing one must not drop the other."""
        make(tmp_path, now=1000.0).tick()
        write_terminal_record(tmp_path, "wf-1", "blocked", 0, "run-1")
        scheduler = make(tmp_path)
        scheduler.account_last_run("wf-1")
        assert scheduler.last_start_of("wf-1") == 1000.0

    def test_the_global_cap_is_deliberately_not_persisted(self, tmp_path: Path) -> None:
        """Not an oversight. The cap is a breaker for systemic faults -- a dead
        gateway, a bad release -- and shipping new code is a plausible remedy
        for those. Shipping code is not a remedy for one line's missing data
        source, which is why the per-line stall state is persisted and this is
        not."""
        first = make(tmp_path, now=1000.0)
        first.tick()
        first.unproductive_launches.append(1000.0)
        assert first.unproductive_recent(1000.0) == 1
        assert make(tmp_path).unproductive_recent(1000.0) == 0

    def test_a_productive_launch_is_not_evidence_of_a_fault(self, tmp_path: Path) -> None:
        """The bug this replaced: the cap counted every launch, so a line that
        ran 25 rounds and reached its goal pushed the fleet toward a breaker
        meant for dead gateways. Work is not a malfunction."""
        scheduler = make(tmp_path, now=1000.0, cooldown_seconds=0)
        scheduler.tick()
        assert scheduler.total_started == 1, "the launch still counts as a launch"
        assert scheduler.unproductive_recent(1000.0) == 0, "but not as evidence of a fault"

    def test_evidence_older_than_the_window_stops_counting(self, tmp_path: Path) -> None:
        """A cumulative count on a long-lived daemon reaches any cap eventually,
        which makes it a timer rather than a detector -- observed on the real
        fleet as a trip roughly every six hours of healthy operation."""
        scheduler = make(tmp_path, now=1000.0)
        window = scheduler.config.cap_window_seconds
        scheduler.unproductive_launches.extend([1000.0, 1000.0 + window / 2])
        assert scheduler.unproductive_recent(1000.0 + window / 2) == 2
        assert scheduler.unproductive_recent(1000.0 + window + 1) == 1, "the older one has aged out"


class TestItWalksTheRosterRatherThanStartingIt:
    """The old babysitter slept 45s between launches. We had nothing.

    Nine lines igniting in the same second means nine gateway probes, nine
    opencode sessions and nine bun processes arriving together -- a burst the
    fleet never had to survive, on a gateway shared with everything else here.
    Steady-state load is identical either way; only the start differs.
    """

    def _lines(self, n: int) -> list[LineSpec]:
        return [
            LineSpec(folder_id=f"wf-{i}", seat="opencode-dsv4pro", enabled=True) for i in range(n)
        ]

    def test_it_waits_between_launches(self, tmp_path: Path) -> None:
        slept: list[float] = []
        make(tmp_path, lines=self._lines(3), slept=slept).tick()
        assert slept == [45.0, 45.0], "three launches means two gaps"

    def test_it_does_not_wait_before_the_first(self, tmp_path: Path) -> None:
        slept: list[float] = []
        make(tmp_path, lines=self._lines(1), slept=slept).tick()
        assert slept == []

    def test_a_tick_with_no_enabled_lines_emits_no_observations_or_sleeps(
        self, tmp_path: Path
    ) -> None:
        """Disabled entries are outside the monitoring population."""
        slept: list[float] = []
        lines = [LineSpec(folder_id="wf-1", seat="s", enabled=False)]
        scheduler = make(tmp_path, lines=lines, slept=slept)
        assert scheduler.tick() == []
        assert slept == []

    def test_refusals_do_not_count_as_launches(self, tmp_path: Path) -> None:
        """Only real starts open a gap. A roster of mostly-refusing lines must
        not accumulate waits for launches that never happened."""
        slept: list[float] = []
        lines = [
            LineSpec(folder_id="wf-off", seat="s", enabled=False),
            LineSpec(folder_id="wf-on", seat="opencode-dsv4pro", enabled=True),
        ]
        make(tmp_path, lines=lines, slept=slept).tick()
        assert slept == []

    def test_the_gap_is_configurable_and_can_be_switched_off(self, tmp_path: Path) -> None:
        slept: list[float] = []
        make(tmp_path, lines=self._lines(3), slept=slept, launch_stagger_seconds=0).tick()
        assert slept == []


class TestTheExecutorIsPinnedToARelease:
    def test_agent_run_comes_from_the_release_symlink(self) -> None:
        """`agent-runtime` is a working tree that gets `git pull --ff-only`ed
        during normal deployment, and one migrated line has agent-runtime as
        its subject. `-current` points at an immutable snapshot instead. The
        old babysitter overrode the pump's default to exactly this path."""
        from fleet_graph.executors.agent_run import DEFAULT_AGENT_RUN_BIN
        from fleet_graph.executors.agent_session import DEFAULT_AGENT_SESSION_BIN

        for path in (DEFAULT_AGENT_RUN_BIN, DEFAULT_AGENT_SESSION_BIN):
            assert "/agent-runtime-current/" in path, path
            assert "/self/agent-runtime/" not in path, path


class TestTicking:
    def test_run_forever_stops_after_the_bounded_ticks(self, tmp_path: Path) -> None:
        scheduler = make(tmp_path, cooldown_seconds=0)
        slept: list[float] = []
        scheduler.run_forever(sleep=slept.append, ticks=3)
        assert scheduler.total_started == 3
        assert slept == [60.0, 60.0], "it sleeps between ticks, not after the last"

    def test_each_tick_is_observed_when_asked(self, tmp_path: Path) -> None:
        seen: list[Any] = []
        scheduler = make(tmp_path)
        scheduler.observe = seen.append
        scheduler.tick()
        assert [r.folder_id for r in seen] == ["wf-1"]

    def test_a_result_renders_for_a_log(self, tmp_path: Path) -> None:
        record = make(tmp_path).tick()[0].as_dict()
        assert record["folder_id"] == "wf-1"
        assert record["ignited"] is True
        assert json.dumps(record)


class TestConfig:
    def test_it_loads_from_json(self, tmp_path: Path) -> None:
        path = tmp_path / "scheduler.json"
        path.write_text(
            json.dumps(
                {
                    "lines": [{"folder_id": "wf-9", "seat": "opencode-gpt-terra"}],
                    "run_root": "/tmp/runs",
                    "total_cap": 7,
                }
            )
        )
        config = SchedulerConfig.from_json(path)
        assert config.lines == [LineSpec(folder_id="wf-9", seat="opencode-gpt-terra")]
        assert config.total_cap == 7
        assert config.run_root == Path("/tmp/runs")

    def test_lines_from_entries(self) -> None:
        assert lines_from([{"folder_id": "a", "seat": "b"}]) == [LineSpec("a", "b")]

    def test_the_emergency_stop_is_a_path_fleet_graph_owns(self) -> None:
        """It used to be /data/ronin/maintenance-stop, inherited from the
        stack P4 retired. Keeping that path would have left the new scheduler
        depending on a directory whose owner no longer exists -- and the old
        flag's expiry could still have ignited a fleet nobody was watching.
        The switch survives; the address is ours."""
        from fleet_graph.scheduler.daemon import DEFAULT_MAINTENANCE_STOP

        assert str(DEFAULT_MAINTENANCE_STOP) == "/data/fleet-graph/maintenance-stop"
        assert "ronin" not in str(DEFAULT_MAINTENANCE_STOP)


class TestARefusalNamesItsRealCause:
    """`no_probe` has three causes; the log line has to say which.

    The unit shipped with `-EnvironmentFile=` instead of `EnvironmentFile=-`.
    systemd calls that an unknown key, drops the line, and starts the service
    anyway -- so the daemon ran with no credentials and every line refused on
    `no_probe`. The refusal text said "no probe registered for seat", which is
    the one cause that was not true. It sent the reader to a registry that was
    perfectly fine.
    """

    def test_a_missing_credential_says_so(self, tmp_path: Path) -> None:
        from fleet_graph.scheduler.probe import MissingProbeCredential

        scheduler = make(tmp_path, prober=FakeProber(MissingProbeCredential("TOKEN_X is unset")))
        record = scheduler.tick()[0].as_dict()
        assert record["refusal"] == "no_probe"
        assert "TOKEN_X is unset" in record["probe_detail"]

    def test_an_unregistered_seat_says_so(self, tmp_path: Path) -> None:
        from fleet_graph.scheduler.probe import UnknownSeat

        scheduler = make(tmp_path, prober=FakeProber(UnknownSeat("no probe for seat 'wat'")))
        assert "no probe for seat" in scheduler.tick()[0].as_dict()["probe_detail"]

    def test_having_no_prober_at_all_says_so(self, tmp_path: Path) -> None:
        record = make(tmp_path, prober=None).tick()[0].as_dict()
        assert "without a gateway prober" in record["probe_detail"]

    def test_the_generic_refusal_no_longer_claims_a_cause(self, tmp_path: Path) -> None:
        """`decide` cannot know which of the three it was, so it must not say."""
        record = make(tmp_path, prober=None).tick()[0].as_dict()
        assert "registered" not in record["detail"]


class TestALineCanRunWhatTheSchedulerCanRun:
    """PATH does not cross into a transient unit on its own.

    agent-run is a bun script and `~/.bun/bin` is not on a systemd user
    manager's default PATH, so the first real line died with
    `env: 'bun': No such file or directory` before doing any work. The old
    babysitter never hit it: it ran from an interactive shell. Environment is
    part of migration equivalence, same as bounds and the gate path.
    """

    def test_the_launch_carries_the_schedulers_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", "/home/x/.bun/bin:/usr/bin")
        spec = make(tmp_path).spec_for(LineSpec(folder_id="wf-1", seat="s", enabled=True))
        assert spec.environment["PATH"] == "/home/x/.bun/bin:/usr/bin"
        assert "--setenv=PATH=/home/x/.bun/bin:/usr/bin" in spec.argv()

    def test_the_config_can_add_more(self, tmp_path: Path) -> None:
        scheduler = make(tmp_path, extra_line_environment={"AGENT_RUNTIME_ROOT": "/data/x"})
        env = scheduler.spec_for(LineSpec(folder_id="wf-1", seat="s", enabled=True)).environment
        assert env["AGENT_RUNTIME_ROOT"] == "/data/x"
        assert "PATH" in env

    def test_an_empty_value_is_not_passed_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--setenv=PATH=` would set an empty PATH, which is worse than
        inheriting nothing: execvp then finds nothing anywhere."""
        monkeypatch.delenv("PATH", raising=False)
        spec = make(tmp_path).spec_for(LineSpec(folder_id="wf-1", seat="s", enabled=True))
        assert "PATH" not in spec.environment


class TestTheRosterReachesTheDecision:
    """The config field and the refusal have to be the same fact.

    `LineSpec.enabled` is only worth anything if the scheduler actually hands
    it to `decide`. A field that is loaded, stored and never read would look
    exactly like a working rollout switch right up until the batch it was
    supposed to hold got started anyway.
    """

    def test_a_line_spec_is_off_unless_it_says_otherwise(self) -> None:
        """The default is the safety property. Staging a line in the config
        must not be the same act as releasing it."""
        assert LineSpec(folder_id="wf-1", seat="opencode-dsv4pro").enabled is False

    def test_a_disabled_line_is_not_observed_or_launched(self, tmp_path: Path) -> None:
        launcher = FakeLauncher()
        scheduler = make(
            tmp_path,
            lines=[LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", enabled=False)],
            launcher=launcher,
        )
        assert scheduler.tick() == []
        assert launcher.launched == []

    def test_a_disabled_line_is_not_checked_in_systemd(self, tmp_path: Path) -> None:
        units = FakeUnits()
        make(
            tmp_path,
            lines=[LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", enabled=False)],
            units=units,
        ).tick()
        assert units.asked == []

    def test_a_disabled_line_does_not_burn_the_global_cap(self, tmp_path: Path) -> None:
        """Otherwise holding eight lines back for a week would trip the
        circuit breaker that exists for restart storms."""
        scheduler = make(
            tmp_path,
            lines=[LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", enabled=False)],
        )
        scheduler.tick()
        assert scheduler.total_started == 0

    def test_one_disabled_line_does_not_hold_back_an_enabled_one(self, tmp_path: Path) -> None:
        """The batch has to be per line. A rollout switch that stopped the
        whole tick on the first `false` would make "one canary, then five"
        impossible to express."""
        scheduler = make(
            tmp_path,
            lines=[
                LineSpec(folder_id="wf-off", seat="opencode-dsv4pro", enabled=False),
                LineSpec(folder_id="wf-on", seat="opencode-dsv4pro", enabled=True),
            ],
        )
        results = {r.folder_id: r.decision for r in scheduler.tick()}
        assert "wf-off" not in results
        assert results["wf-on"].ignite is True

    def test_the_config_loader_carries_the_flag_through(self, tmp_path: Path) -> None:
        path = tmp_path / "lines.json"
        path.write_text(
            json.dumps(
                {
                    "lines": [
                        {"folder_id": "wf-on", "seat": "s", "enabled": True},
                        {"folder_id": "wf-off", "seat": "s"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        loaded = {line.folder_id: line.enabled for line in SchedulerConfig.from_json(path).lines}
        assert loaded == {"wf-on": True, "wf-off": False}


@pytest.mark.parametrize("terminal", ["blocked", "bounds", "fault", ""])
def test_only_done_is_final(tmp_path: Path, terminal: str) -> None:
    write_terminal(tmp_path, "wf-1", terminal)
    assert make(tmp_path).tick()[0].decision.ignite


class TestTheRealProbeTransport:
    def test_it_does_not_trust_the_proxy_environment(self) -> None:
        """The loopback trap, for the fourth time in this repo: this host
        exports a SOCKS proxy, and 127.0.0.1 would go through it and fail to
        connect at all."""
        from fleet_graph.scheduler.probe import HttpxProbeTransport

        transport = HttpxProbeTransport()
        assert transport._client.trust_env is False

    def test_it_does_not_wait_forever_for_an_answer(self) -> None:
        """Waiting a long time for "is the upstream answering right now" is
        itself the answer, and a scheduler blocked on a hung gateway stops
        scheduling everything else."""
        from fleet_graph.scheduler.probe import HttpxProbeTransport

        assert HttpxProbeTransport()._client.timeout.read is not None
        assert HttpxProbeTransport(timeout=5.0)._client.timeout.read == 5.0


class TestTheGateExpires:
    """babysitter v23 made `expires_at` mandatory and an expired flag inert
    (2026-08-23 ruling). Reading only `path.exists()` looks like the same gate
    and is not: an expired flag nobody cleaned up holds the fleet down forever.
    """

    def _scheduler(self, tmp_path: Path, flag: str | None, *, now: float) -> Scheduler:
        path = tmp_path / "maintenance-stop"
        if flag is not None:
            path.write_text(flag, encoding="utf-8")
        return Scheduler(
            SchedulerConfig(lines=[], maintenance_stop_path=path),
            clock=lambda: now,
        )

    def test_an_unexpired_flag_holds_the_fleet(self, tmp_path: Path) -> None:
        flag = json.dumps({"reason": "stop", "expires_at": "2026-08-26T05:00:00Z"})
        assert self._scheduler(tmp_path, flag, now=1787000000.0).maintenance_stop() is True

    def test_an_expired_flag_is_inert(self, tmp_path: Path) -> None:
        """The real one on this machine expired at 2026-08-26T05:03:37Z while
        the fleet was still down; `exists()` alone would gate forever."""
        flag = json.dumps({"reason": "stop", "expires_at": "2026-08-26T05:03:37Z"})
        # 2026-08-26T12:00:00Z, hours after the deadline.
        assert self._scheduler(tmp_path, flag, now=1787745600.0).maintenance_stop() is False

    def test_the_deadline_itself_is_already_past(self, tmp_path: Path) -> None:
        """`>=`, not `>`: a flag is not still holding at the instant it lapses."""
        flag = json.dumps({"expires_at": "2026-08-26T05:03:37Z"})
        exactly = calendar.timegm(time.strptime("2026-08-26T05:03:37Z", "%Y-%m-%dT%H:%M:%SZ"))
        assert self._scheduler(tmp_path, flag, now=float(exactly)).maintenance_stop() is False

    def test_no_flag_at_all_is_not_a_gate(self, tmp_path: Path) -> None:
        assert self._scheduler(tmp_path, None, now=1787745600.0).maintenance_stop() is False

    def test_an_offset_timestamp_is_understood(self, tmp_path: Path) -> None:
        """The file on this machine has been written both ways."""
        flag = json.dumps({"expires_at": "2026-08-27T00:00:00+00:00"})
        assert self._scheduler(tmp_path, flag, now=1787745600.0).maintenance_stop() is True

    @pytest.mark.parametrize(
        "flag",
        [
            "not json at all",
            "{}",
            json.dumps({"expires_at": "whenever"}),
            json.dumps({"expires_at": None}),
        ],
    )
    def test_a_flag_we_cannot_read_keeps_holding(self, tmp_path: Path, flag: str) -> None:
        """Deliberate divergence from the old gate, which treated a malformed
        file as absent. A broken gate file is an operator error to stop on,
        not to silently ignore -- opening the fleet on a parse failure is the
        one outcome nobody would have asked for."""
        assert self._scheduler(tmp_path, flag, now=1787745600.0).maintenance_stop() is True
