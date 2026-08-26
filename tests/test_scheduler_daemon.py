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
    **config: Any,
) -> Scheduler:
    return Scheduler(
        SchedulerConfig(
            lines=lines or [LineSpec(folder_id="wf-1", seat="opencode-dsv4pro")],
            run_root=tmp_path / "runs",
            maintenance_stop_path=tmp_path / "maintenance-stop",
            **config,
        ),
        prober=FakeProber() if prober is _DEFAULT else prober,
        launcher=launcher or FakeLauncher(),
        units=units or FakeUnits(),
        clock=lambda: now,
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
        lines = [LineSpec(folder_id=f"wf-{i}", seat="opencode-dsv4pro") for i in range(4)]
        results = make(tmp_path, lines=lines, total_cap=2).tick()
        refusals = [r.decision.refusal for r in results]
        assert refusals == [None, None, Refusal.TOTAL_CAP_REACHED, Refusal.TOTAL_CAP_REACHED]

    def test_a_failed_launch_still_counts_against_the_cap(self, tmp_path: Path) -> None:
        """Otherwise a launch that fails every time never trips the cap that
        exists to catch exactly that."""
        lines = [LineSpec(folder_id=f"wf-{i}", seat="opencode-dsv4pro") for i in range(3)]
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

    def test_it_reads_the_terminal_field_and_nothing_else(self, tmp_path: Path) -> None:
        """Reading a line's own account of its work would make the scheduler a
        second, unaccountable judge of it (INV-3)."""
        import re

        from fleet_graph.scheduler import daemon

        source = Path(daemon.__file__).read_text(encoding="utf-8")
        reads = set(re.findall(r'record\.get\("([a-z_]+)"\)', source))
        assert reads == {"terminal"}, reads


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

    def test_the_gate_path_keeps_the_operators_muscle_memory(self) -> None:
        from fleet_graph.scheduler.daemon import DEFAULT_MAINTENANCE_STOP

        assert str(DEFAULT_MAINTENANCE_STOP) == "/data/ronin/maintenance-stop"


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
