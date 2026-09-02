"""M1 waiting-as-park: a dispatched line parks as ``waiting_dd``, zero LLM.

The spec's criteria, one test group each:

- **词表落地** -- the closed line-state vocabulary ``working / waiting_dd /
  waiting_decision / waiting_external / done / failed`` is derived from the
  mechanical ``terminal`` + ``waiting_on`` fields and landed in both
  authoritative files (``terminal.json`` and the scheduler's
  ``.scheduler/<id>.json``). ``failed`` (self-judged) is distinct from
  ``fault`` (mechanical).
- **派单即驻停、零点火** -- a line whose terminal is ``blocked`` +
  ``waiting_on: "dd"`` is parked: the scheduler refuses ignition with a
  ``parked_awaiting_dd`` refusal and the launcher is never called, however far
  past backoff the clock is.
- **唤醒事实点火** -- the two dd facts, ``dd_awaiting_gate`` and
  ``dd_terminal``, are the only thing that wakes the parked line: when the
  dispatched development reaches either, the next tick ignites.
- **阴性** -- deleting the wake-fact emission (the development never advances)
  leaves the line parked forever: it is *never* re-ignited by polling. This is
  the red-on-rollback guard -- introduce a poll loop and it ignites.

Everything runs against a scratch run_root, never production files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig
from fleet_graph.scheduler.ignition import Refusal
from fleet_graph.scheduler.launcher import LaunchResult
from fleet_graph.state.run_artifacts import (
    LINE_STATE_VALUES,
    RunArtifacts,
    derive_line_state,
)

CLOCK_START = 1_787_000_000.0
TICK_EPOCH = CLOCK_START + 3600.0


class Clock:
    def __init__(self, now: float = CLOCK_START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeUnits:
    def is_active(self, unit_name: str) -> bool:
        return False


class FakeProber:
    def check(self, seat: str) -> bool:
        return True


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        return LaunchResult(spec.unit_name, True, "")


class FakeDd:
    """Scriptable dd wake facts. ``fact`` is what every probe returns."""

    def __init__(self, fact: str | None = None) -> None:
        self.fact = fact
        self.calls: list[str] = []

    def dd_fact(self, development_id: str) -> str | None:
        self.calls.append(development_id)
        return self.fact


def make(
    tmp_path: Path, *, dd: FakeDd | None = None, clock: Clock | None = None
) -> tuple[Scheduler, Clock, FakeLauncher]:
    clock = clock or Clock()
    launcher = FakeLauncher()
    scheduler = Scheduler(
        SchedulerConfig(
            lines=[
                LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", alias="canary", enabled=True)
            ],
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd",
            maintenance_stop_path=tmp_path / "maintenance-stop",
        ),
        prober=FakeProber(),
        launcher=launcher,
        units=FakeUnits(),
        clock=clock,
        sleep=lambda _s: None,
        dd=dd,
    )
    return scheduler, clock, launcher


def write_dd_blocked(tmp_path: Path, *, run_id: str = "run-d1", dd_id: str = "dev-1") -> None:
    record = {
        "terminal": "blocked",
        "rounds": 0,
        "run_id": run_id,
        "at": "2026-08-27T10:00:00Z",
        "reason": "dispatched, waiting for the development",
        "waiting_on": "dd",
        "dd_development_id": dd_id,
    }
    path = tmp_path / "runs" / "wf-1" / "terminal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def dd_blocked_line(
    tmp_path: Path, *, dd: FakeDd | None = None, dd_id: str = "dev-1"
) -> tuple[Scheduler, Clock, FakeLauncher]:
    """A line the scheduler launched once, which then parked on a development."""
    scheduler, clock, launcher = make(tmp_path, dd=dd)
    assert scheduler.tick()[0].decision.ignite  # the priming launch
    launcher.launched.clear()
    write_dd_blocked(tmp_path, dd_id=dd_id)
    clock.now = TICK_EPOCH
    return scheduler, clock, launcher


def stall_file(tmp_path: Path) -> Path:
    return tmp_path / "runs" / ".scheduler" / "wf-1.json"


# --- 词表落地 ---------------------------------------------------------------


class TestClosedVocabulary:
    def test_the_six_words_are_exactly_the_vocabulary(self) -> None:
        assert {
            "working",
            "waiting_dd",
            "waiting_decision",
            "waiting_external",
            "done",
            "failed",
        } == LINE_STATE_VALUES

    def test_each_terminal_projects_to_its_word(self) -> None:
        assert derive_line_state(None) == "working"
        assert derive_line_state("done") == "done"
        assert derive_line_state("failed") == "failed"
        assert derive_line_state("blocked", "dd") == "waiting_dd"
        assert derive_line_state("blocked", "decision") == "waiting_decision"
        assert derive_line_state("blocked", "external") == "waiting_external"

    def test_fault_is_not_merged_into_failed(self) -> None:
        """Mechanical ``fault`` keeps its own semantics: it never reads as the
        self-judged ``failed`` word."""
        assert derive_line_state("fault") != "failed"
        assert derive_line_state("fault") == "working"
        assert derive_line_state("bounds") == "working"
        assert derive_line_state("killed") == "working"

    def test_a_blocked_terminal_with_no_reason_stays_working(self) -> None:
        assert derive_line_state("blocked", "none") == "working"
        assert derive_line_state("blocked", None) == "working"


class FakeClock:
    def __call__(self) -> float:
        return CLOCK_START


class TestVocabularyLanding:
    def test_terminal_json_carries_the_line_state_and_dd_anchor(self, tmp_path: Path) -> None:
        artifacts = RunArtifacts(
            tmp_path / "run", run_id="r-1", folder_id="wf-1", clock=FakeClock()
        )
        artifacts.write_terminal(
            terminal="blocked", rounds=1, waiting_on="dd", dd_development_id="dev-1"
        )
        event = json.loads(artifacts.terminal_path.read_text(encoding="utf-8"))
        assert event["line_state"] == "waiting_dd"
        assert event["dd_development_id"] == "dev-1"

    def test_a_done_terminal_lands_done(self, tmp_path: Path) -> None:
        artifacts = RunArtifacts(
            tmp_path / "run", run_id="r-1", folder_id="wf-1", clock=FakeClock()
        )
        artifacts.write_terminal(terminal="done", rounds=1)
        assert json.loads(artifacts.terminal_path.read_text())["line_state"] == "done"

    def test_the_scheduler_lands_the_word_in_the_stall_file(self, tmp_path: Path) -> None:
        scheduler, _, _ = dd_blocked_line(tmp_path, dd=FakeDd())
        scheduler.tick()
        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert state["line_state"] == "waiting_dd"


# --- 派单即驻停、零点火 -----------------------------------------------------


class TestDispatchParksWithoutIgniting:
    def test_a_waiting_dd_line_is_parked_with_the_dd_refusal(self, tmp_path: Path) -> None:
        """The rollback contrast: without the dd parked branch, this line --
        backoff long expired -- would ignite."""
        scheduler, _, launcher = dd_blocked_line(tmp_path, dd=FakeDd())

        results = scheduler.tick()

        assert results[0].decision.refusal is Refusal.PARKED_AWAITING_DD
        assert launcher.launched == []

    def test_it_stays_parked_across_ticks(self, tmp_path: Path) -> None:
        scheduler, clock, launcher = dd_blocked_line(tmp_path, dd=FakeDd())
        for _ in range(5):
            clock.now += 60.0
            assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DD
        assert launcher.launched == []

    def test_the_snapshot_records_the_dd_anchor(self, tmp_path: Path) -> None:
        scheduler, _, _ = dd_blocked_line(tmp_path, dd=FakeDd())
        scheduler.tick()
        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert state["parked_dd_development_id"] == "dev-1"
        assert state["parked_run_id"] == "run-d1"

    def test_a_waiting_dd_line_and_a_decision_line_have_distinct_refusals(
        self, tmp_path: Path
    ) -> None:
        """The two waits stay distinguishable in the per-tick refusal line:
        ``parked_awaiting_dd`` for a dispatched development, versus the legacy
        ``parked_awaiting_decision`` for a human ruling."""
        from fleet_graph.scheduler.daemon import ParkOutcome

        assert Refusal.PARKED_AWAITING_DD is not Refusal.PARKED_AWAITING_DECISION
        assert ParkOutcome(parked=True, kind="dd").kind == "dd"

    def test_an_adopted_baseline_dd_terminal_is_not_parked(self, tmp_path: Path) -> None:
        """No stall file means this scheduler never launched the line: the
        terminal is adopted, not accounted, so parking must not seize it."""
        write_dd_blocked(tmp_path)
        scheduler, _, _ = make(tmp_path, dd=FakeDd(), clock=Clock(TICK_EPOCH))
        assert scheduler.tick()[0].decision.ignite


# --- 唤醒事实点火 ------------------------------------------------------------


class TestDdWakeFacts:
    def test_dd_awaiting_gate_wakes_the_line(self, tmp_path: Path) -> None:
        dd = FakeDd()
        scheduler, clock, launcher = dd_blocked_line(tmp_path, dd=dd)
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DD

        dd.fact = "awaiting_gate"
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:dd_awaiting_gate"
        assert result.decision.ignite
        assert len(launcher.launched) == 1

    def test_dd_terminal_wakes_the_line(self, tmp_path: Path) -> None:
        dd = FakeDd()
        scheduler, clock, _launcher = dd_blocked_line(tmp_path, dd=dd)
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DD

        dd.fact = "terminal"
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:dd_terminal"
        assert result.decision.ignite

    def test_the_probe_asks_about_the_dispatched_development(self, tmp_path: Path) -> None:
        dd = FakeDd()
        scheduler, _, _ = dd_blocked_line(tmp_path, dd=dd, dd_id="dev-fg-abc")
        scheduler.tick()
        assert dd.calls == ["dev-fg-abc"]

    def test_a_wake_fact_already_present_prevents_parking(self, tmp_path: Path) -> None:
        dd = FakeDd(fact="awaiting_gate")
        scheduler, _, _ = dd_blocked_line(tmp_path, dd=dd)
        result = scheduler.tick()[0]
        assert result.parked is False
        assert result.park_event == "not_parked:dd_awaiting_gate"
        assert result.decision.ignite


# --- 阴性：删掉唤醒事实发射 → 永不点火 ---------------------------------------


class TestNoWakeFactMeansNoIgnition:
    def test_without_any_wake_fact_the_line_is_never_ignited(self, tmp_path: Path) -> None:
        """The spec's negative: the wake is a *fact*, never a poll. With the
        development never advancing (fact stays None), the line must never be
        re-ignited -- reintroduce a poll loop and this pathological case turns
        red because the line would relaunch and burn LLM."""
        scheduler, clock, launcher = dd_blocked_line(tmp_path, dd=FakeDd())
        for _ in range(10):
            clock.now += 60.0
            result = scheduler.tick()[0]
            assert result.decision.refusal is Refusal.PARKED_AWAITING_DD
        assert launcher.launched == []

    def test_a_missing_dd_source_fails_open_rather_than_locking(self, tmp_path: Path) -> None:
        """No dd source means no way to observe the wake fact: parking must
        fail open to plain backoff, never lock the line shut."""
        scheduler, _, launcher = dd_blocked_line(tmp_path, dd=None)
        result = scheduler.tick()[0]
        assert result.parked is False
        assert result.park_event == "not_parked:dd_probe_failed:RuntimeError"
        assert result.decision.ignite
        assert len(launcher.launched) == 1

    def test_a_waiting_dd_terminal_without_an_anchor_is_never_parked(self, tmp_path: Path) -> None:
        """No dd_development_id means there is no dd fact that can ever fire:
        fail open, never park (a poll would be worse -- it would loop on a fact
        that can never arrive)."""
        scheduler, clock, _ = dd_blocked_line(tmp_path, dd=FakeDd())
        clock.now += 60.0
        scheduler.tick()  # establish the park with a valid anchor first
        # Now replace the terminal with an anchorless waiting_dd terminal.
        write_dd_blocked(tmp_path, run_id="run-d2", dd_id="")
        clock.now += 3600.0
        result = scheduler.tick()[0]
        assert result.park_event == "not_parked:no_dd_development"
        assert result.parked is False
