"""Parking: a line blocked waiting on a human decision is not relaunched on a
timer.

The contract under test, in one line each:

- blocked + waiting_on=decision and no wake fact -> PARKED, zero ignitions;
- blocked + waiting_on=external/none -> today's backoff behaviour, unchanged;
- a wake fact (inbox mail, goal.md revision) clears the parking;
- any probe failure fails *open* to plain backoff -- parking must never be
  able to lock a line shut;
- the snapshot lives in the stall-state file, so it survives a daemon restart
  and clears with the same escape hatch.

Parking applies to *accounted* terminals only. A terminal adopted as baseline
(no stall file -- a new roster line, or the operator deleted the counter) is
marked considered without parking, for the same reason its failure is not
counted into the streak: a run this file did not witness is not ours to judge.
The tests therefore prime each line with one launch before blocking it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fleet_graph.bus.board import parked_question_key
from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig
from fleet_graph.scheduler.ignition import Refusal
from fleet_graph.scheduler.launcher import LaunchResult
from fleet_graph.scheduler.wake import LiveWakeSignals, parse_bus_timestamp

BLOCKED_AT = "2026-08-27T10:00:00Z"
BLOCKED_EPOCH = parse_bus_timestamp(BLOCKED_AT)
#: When the priming launch happens: half an hour before the line blocks.
PRIME_EPOCH = BLOCKED_EPOCH - 1800.0
#: When the tests tick: an hour after the block, past any streak-1 backoff, so
#: an unparked line ignites and the only thing standing in the way is parking.
TICK_EPOCH = BLOCKED_EPOCH + 3600.0


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeUnits:
    def is_active(self, unit_name: str) -> bool:
        return False


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        return LaunchResult(spec.unit_name, True, "")


class FakeProber:
    def check(self, seat: str) -> bool:
        return True


class FakeWake:
    """Scriptable wake facts. `error` makes every probe raise; `inbox_error`
    and `revision_error` fail one source at a time."""

    def __init__(
        self,
        *,
        revision: str = "sha256:rev-1",
        inbox: bool = False,
        error: Exception | None = None,
        inbox_error: Exception | None = None,
        revision_error: Exception | None = None,
        decision: bool = False,
        decision_error: Exception | None = None,
    ) -> None:
        self.revision = revision
        self.inbox = inbox
        self.error = error
        self.inbox_error = inbox_error
        self.revision_error = revision_error
        self.decision = decision
        self.decision_error = decision_error
        self.inbox_calls: list[tuple[str, float]] = []
        self.revision_calls: list[str] = []
        self.decision_calls: list[tuple[str, float]] = []

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        self.inbox_calls.append((alias, after_epoch))
        failure = self.error or self.inbox_error
        if failure is not None:
            raise failure
        return self.inbox

    def goal_revision(self, folder_id: str) -> str:
        self.revision_calls.append(folder_id)
        failure = self.error or self.revision_error
        if failure is not None:
            raise failure
        return self.revision

    def decision_landed(self, question_note_id: str, after_epoch: float) -> bool:
        self.decision_calls.append((question_note_id, after_epoch))
        failure = self.error or self.decision_error
        if failure is not None:
            raise failure
        return self.decision


class FakeTicket:
    question_note_id = "note-123"


class FakePublishResult:
    """The bus derives a root entity's id from its first message id."""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self.message_id = entity_id
        self.channel_seq = 1
        self.deduplicated = False


class FakeBoard:
    """Records every publish in order, so tests can pin card-before-question."""

    def __init__(
        self, *, error: Exception | None = None, card_error: Exception | None = None
    ) -> None:
        self.error = error
        self.card_error = card_error
        self.asked: list[dict[str, Any]] = []
        self.cards: list[dict[str, Any]] = []
        self.publishes: list[str] = []

    def publish_card(self, payload: dict[str, Any], idempotency_key: str) -> FakePublishResult:
        self.publishes.append("card")
        self.cards.append({"payload": payload, "idempotency_key": idempotency_key})
        if self.card_error is not None:
            raise self.card_error
        return FakePublishResult(f"msg-card-{len(self.cards)}")

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> FakeTicket:
        self.publishes.append("question")
        self.asked.append(
            {
                "card_entity_id": card_entity_id,
                "question": question,
                "idempotency_key": idempotency_key,
            }
        )
        if self.error is not None:
            raise self.error
        return FakeTicket()


def make(
    tmp_path: Path,
    *,
    wake: Any = None,
    board: Any = None,
    alias: str | None = "canary",
    launcher: FakeLauncher | None = None,
    clock: Clock | None = None,
) -> tuple[Scheduler, Clock, FakeLauncher]:
    clock = clock or Clock(PRIME_EPOCH)
    launcher = launcher or FakeLauncher()
    scheduler = Scheduler(
        SchedulerConfig(
            lines=[LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", alias=alias, enabled=True)],
            run_root=tmp_path / "runs",
            maintenance_stop_path=tmp_path / "maintenance-stop",
        ),
        prober=FakeProber(),
        launcher=launcher,
        units=FakeUnits(),
        clock=clock,
        sleep=lambda _s: None,
        wake=wake,
        board=board,
    )
    return scheduler, clock, launcher


def write_blocked(
    tmp_path: Path,
    *,
    waiting_on: str | None = "decision",
    run_id: str = "run-b1",
    at: str = BLOCKED_AT,
    reason: str = "等监督面拍板（L2-5）",
    goal_revision: str | None = "sha256:rev-1",
) -> None:
    record: dict[str, Any] = {
        "terminal": "blocked",
        "rounds": 0,
        "run_id": run_id,
        "at": at,
        "reason": reason,
    }
    if waiting_on is not None:
        record["waiting_on"] = waiting_on
    # G1: the parking baseline is the goal revision the line actually consumed,
    # written into its own terminal record -- not the live value at registration
    # time. The default matches FakeWake's live revision so an unedited goal
    # holds the line parked.
    if goal_revision is not None:
        record["goal_revision"] = goal_revision
    path = tmp_path / "runs" / "wf-1" / "terminal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def blocked_line(
    tmp_path: Path,
    *,
    waiting_on: str | None = "decision",
    wake: Any = None,
    board: Any = None,
    alias: str | None = "canary",
    goal_revision: str | None = "sha256:rev-1",
) -> tuple[Scheduler, Clock, FakeLauncher]:
    """A line the scheduler launched once, which then blocked.

    This is the production shape: the terminal is *accounted* (the stall file
    witnessed the launch), which is what makes it park-eligible. The clock ends
    up an hour past the block -- far beyond the streak-1 backoff -- so any
    refusal from here on is parking, not cooldown.

    ``goal_revision`` is the goal revision the line's own terminal record says
    it consumed (G1). It defaults to FakeWake's live revision so an unedited
    goal holds the line parked.
    """
    scheduler, clock, launcher = make(tmp_path, wake=wake, board=board, alias=alias)
    assert scheduler.tick()[0].decision.ignite  # the priming launch
    launcher.launched.clear()
    write_blocked(tmp_path, waiting_on=waiting_on, goal_revision=goal_revision)
    clock.now = TICK_EPOCH
    return scheduler, clock, launcher


def stall_file(tmp_path: Path) -> Path:
    return tmp_path / "runs" / ".scheduler" / "wf-1.json"


def clear_parked_fields(tmp_path: Path) -> None:
    state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
    state.update(parked_run_id=None, parked_at=None, parked_goal_revision=None)
    stall_file(tmp_path).write_text(json.dumps(state), encoding="utf-8")


class TestParkingHolds:
    def test_blocked_on_a_decision_is_parked_and_nothing_ignites(self, tmp_path: Path) -> None:
        """The rollback contrast, pinned: without the parked branch in
        `decide`, this line -- backoff long expired -- would ignite. Removing
        the branch turns this red."""
        scheduler, _, launcher = blocked_line(tmp_path, wake=FakeWake())

        results = scheduler.tick()

        assert results[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert launcher.launched == []

    def test_it_stays_parked_across_ticks(self, tmp_path: Path) -> None:
        scheduler, clock, launcher = blocked_line(tmp_path, wake=FakeWake())
        for _ in range(5):
            clock.now += 60.0
            assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert launcher.launched == []

    def test_parking_survives_a_daemon_restart(self, tmp_path: Path) -> None:
        """The snapshot is in the stall-state file, under the same discipline
        as the streak: a counter that reset on deploy would relaunch the line
        on every release -- exactly when nobody is watching."""
        scheduler, _, _ = blocked_line(tmp_path, wake=FakeWake())
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION

        reborn, _, _ = make(tmp_path, wake=FakeWake(), clock=Clock(TICK_EPOCH + 60.0))
        assert reborn.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION

    def test_the_snapshot_records_when_what_and_which_run(self, tmp_path: Path) -> None:
        scheduler, _, _ = blocked_line(tmp_path, wake=FakeWake(revision="sha256:rev-1"))
        scheduler.tick()

        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert state["parked_run_id"] == "run-b1"
        assert state["parked_at"] == TICK_EPOCH
        assert state["parked_goal_revision"] == "sha256:rev-1"

    def test_the_observe_record_names_the_blocker(self, tmp_path: Path) -> None:
        """An operator scanning the log sees *what* the line waits on without
        opening the run root. Display only -- decide saw a bool."""
        scheduler, _, _ = blocked_line(tmp_path, wake=FakeWake())
        record = scheduler.tick()[0].as_dict()

        assert record["parked"] is True
        assert record["park_event"] == "established"
        assert "等监督面拍板" in record["blocker"]

    def test_an_adopted_baseline_terminal_is_not_parked(self, tmp_path: Path) -> None:
        """No stall file means this scheduler never launched the line: the
        terminal is adopted, not accounted, and parking follows the streak's
        rule -- a run we did not witness is not ours to judge."""
        write_blocked(tmp_path)
        scheduler, _, _ = make(tmp_path, wake=FakeWake(), clock=Clock(TICK_EPOCH))
        assert scheduler.tick()[0].decision.ignite


class TestOnlyDecisionParks:
    def test_waiting_on_external_keeps_todays_backoff_behaviour(self, tmp_path: Path) -> None:
        """External blockers can clear on their own; backoff already handles
        them and parking would just delay the pickup."""
        scheduler, _, _ = blocked_line(tmp_path, waiting_on="external", wake=FakeWake())
        assert scheduler.tick()[0].decision.ignite

    def test_waiting_on_none_keeps_todays_backoff_behaviour(self, tmp_path: Path) -> None:
        scheduler, _, _ = blocked_line(tmp_path, waiting_on="none", wake=FakeWake())
        assert scheduler.tick()[0].decision.ignite

    def test_a_legacy_terminal_without_the_field_is_untouched(self, tmp_path: Path) -> None:
        """Every terminal written before R0c lacks `waiting_on`; they must all
        behave exactly as they did."""
        scheduler, _, _ = blocked_line(tmp_path, waiting_on=None, wake=FakeWake())
        assert scheduler.tick()[0].decision.ignite

    def test_within_backoff_the_refusal_is_still_backoff_not_parking(self, tmp_path: Path) -> None:
        """An external-blocked line inside its backoff window shows the same
        refusal it did before R0c."""
        scheduler, clock, _ = blocked_line(tmp_path, waiting_on="external", wake=FakeWake())
        # Backoff counts from the last *start*: streak 1 doubles the 300s
        # cooldown to 600s, and 400s after the priming launch is inside it.
        clock.now = PRIME_EPOCH + 400.0
        assert scheduler.tick()[0].decision.refusal is Refusal.NO_PROGRESS


class TestWakeFacts:
    def test_new_inbox_mail_wakes_the_line(self, tmp_path: Path) -> None:
        wake = FakeWake()
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake)
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION

        wake.inbox = True
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:inbox"
        assert result.decision.ignite

    def test_the_inbox_probe_asks_about_mail_after_the_terminal(self, tmp_path: Path) -> None:
        """Anything earlier was already drained by the run that blocked."""
        wake = FakeWake()
        scheduler, _, _ = blocked_line(tmp_path, wake=wake)
        scheduler.tick()
        assert wake.inbox_calls[0] == ("canary", BLOCKED_EPOCH)

    def test_a_goal_md_edit_wakes_the_line(self, tmp_path: Path) -> None:
        wake = FakeWake(revision="sha256:rev-1")
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake)
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION

        wake.revision = "sha256:rev-2"
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:goal_revision"
        assert result.decision.ignite

    def test_a_line_without_an_alias_skips_the_inbox_source(self, tmp_path: Path) -> None:
        wake = FakeWake()
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake, alias=None)
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION
        clock.now += 60.0
        scheduler.tick()
        assert wake.inbox_calls == []
        assert wake.revision_calls  # the goal.md source still runs

    def test_mail_that_predates_the_parking_prevents_it(self, tmp_path: Path) -> None:
        """A wake fact that already exists means there is nothing to wait for:
        never park, let the line pick its mail up."""
        scheduler, _, _ = blocked_line(tmp_path, wake=FakeWake(inbox=True))
        result = scheduler.tick()[0]
        assert result.parked is False
        assert result.park_event == "not_parked:inbox_already_has_mail"
        assert result.decision.ignite

    def test_a_woken_terminal_is_not_parked_again(self, tmp_path: Path) -> None:
        """Re-parking after a goal.md wake would snapshot the *new* revision
        and swallow the very wake that just fired."""
        wake = FakeWake(revision="sha256:rev-1")
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake)
        scheduler.tick()
        wake.revision = "sha256:rev-2"
        clock.now += 60.0
        assert scheduler.tick()[0].decision.ignite

        # The line is now cooling down from that ignition, but it is a
        # cooldown, not a parking: the same terminal never parks twice.
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.parked is False
        assert result.decision.refusal is not Refusal.PARKED_AWAITING_DECISION


class TestDecisionConsumedWake:
    """Wake fact 4: the decision bridge consumed a `work.decision.v1` for a dd
    development this line dispatched (`dispatched_by == folder_id`). The bridge
    writes `dispatched_decision_consumed_at` into the stall-state file; the
    scheduler reads it as a wake fact and, past the stall threshold, as a red
    missed-delivery annotation (spec判据 §3.1).

    Positive: the consumed fact wakes the line within the stall window --
    `parked_run_id` is cleared and decide ignites.
    Negative: a line parked awaiting a decision that has not landed must not
    be woken and must not raise the red signal; the mutation "wake on merely
    being parked" must stay red (the line stays parked).
    """

    def _park_and_write_consumed(self, tmp_path: Path, consumed_at: float) -> Any:
        """A parked line, then the bridge records the consumed-decision fact
        (as it would when resuming the dispatched dd single)."""
        scheduler, clock, launcher = blocked_line(tmp_path, wake=FakeWake())
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION
        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        state["dispatched_decision_consumed_at"] = consumed_at
        stall_file(tmp_path).write_text(json.dumps(state), encoding="utf-8")
        return scheduler, clock, launcher

    def test_a_consumed_dispatched_decision_wakes_the_line(self, tmp_path: Path) -> None:
        scheduler, clock, _ = blocked_line(tmp_path, wake=FakeWake())
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION
        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        state["dispatched_decision_consumed_at"] = TICK_EPOCH + 1.0
        stall_file(tmp_path).write_text(json.dumps(state), encoding="utf-8")

        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:decision_consumed"
        assert result.decision.ignite
        after = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert after["parked_run_id"] is None
        assert after["dispatched_decision_consumed_at"] is None

    def test_the_line_wakes_within_the_stall_window_without_a_red_annotation(
        self, tmp_path: Path
    ) -> None:
        """The wake fact is consumed just after the park: the line wakes on the
        next tick, well inside the 3-tick stall window, so no red annotation."""
        scheduler, clock, _ = self._park_and_write_consumed(tmp_path, consumed_at=TICK_EPOCH + 1.0)
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:decision_consumed"
        assert result.decision_wake_stall is None

    def test_a_decision_consumed_past_the_stall_threshold_is_red(self, tmp_path: Path) -> None:
        """The observability half: a decision that was consumed long ago (the
        bridge recorded it, the line stayed parked) must surface as a red
        `decision_wake_stall` annotation carrying the line id and wait duration."""
        scheduler, clock, _ = self._park_and_write_consumed(tmp_path, consumed_at=TICK_EPOCH + 1.0)
        clock.now += 400.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:decision_consumed"
        assert result.decision_wake_stall is not None
        assert result.decision_wake_stall["folder_id"] == "wf-1"
        assert result.decision_wake_stall["wait_seconds"] >= 300.0
        assert result.decision.ignite

    def test_the_red_annotation_appears_in_the_observe_record(self, tmp_path: Path) -> None:
        """`as_dict` exposes the stall annotation so an operator scanning the
        log sees what is red and how long the line has been waiting."""
        scheduler, clock, _ = self._park_and_write_consumed(tmp_path, consumed_at=TICK_EPOCH + 1.0)
        clock.now += 400.0
        record = scheduler.tick()[0].as_dict()
        assert record["decision_wake_stall"]["folder_id"] == "wf-1"
        assert record["decision_wake_stall"]["wait_seconds"] >= 300.0

    def test_a_parked_line_without_a_landed_decision_is_not_woken_or_red(
        self, tmp_path: Path
    ) -> None:
        """Negative: the line is parked awaiting a decision that has not
        landed (like wf-6475fd at the time of the incident). No wake fact
        means no wake and no red signal -- the mutation "wake whenever parked"
        would ignite here and this turns red."""
        scheduler, clock, launcher = blocked_line(tmp_path, wake=FakeWake())
        for _ in range(5):
            clock.now += 60.0
            result = scheduler.tick()[0]
            assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
            assert result.decision_wake_stall is None
        assert launcher.launched == []


class TestFailOpen:
    def test_a_probe_error_while_parked_falls_back_to_backoff(self, tmp_path: Path) -> None:
        """Parking saves money; a broken probe must never lock a line shut."""
        wake = FakeWake()
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake)
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION

        wake.error = RuntimeError("bus is down")
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:probe_failed:RuntimeError"
        assert result.decision.ignite

    def test_a_probe_error_at_establishment_means_no_parking(self, tmp_path: Path) -> None:
        scheduler, _, _ = blocked_line(tmp_path, wake=FakeWake(error=RuntimeError("mcp down")))
        result = scheduler.tick()[0]
        assert result.parked is False
        assert result.park_event == "not_parked:probe_failed:RuntimeError"
        assert result.decision.ignite

    def test_no_wake_signals_at_all_disables_parking(self, tmp_path: Path) -> None:
        """A scheduler that cannot observe wake facts must not park: it would
        have no way to ever unpark."""
        scheduler, _, _ = blocked_line(tmp_path, wake=None)
        assert scheduler.tick()[0].decision.ignite

    def test_an_unparseable_terminal_timestamp_degrades_only_the_inbox_source(
        self, tmp_path: Path
    ) -> None:
        """The terminal's `at` is consumed by the inbox comparison alone, so a
        corrupt stamp costs that one source; the goal.md anchor still parks
        the line. (Before the per-source hotfix this failed the whole
        establish open.)"""
        scheduler, clock, _ = blocked_line(tmp_path, wake=FakeWake())
        write_blocked(tmp_path, at="not a timestamp", run_id="run-b2")
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert result.park_event == "established:inbox_unavailable:ValueError"


class TestG1ConsumedRevisionBaseline:
    """G1: the parking baseline is the goal.md revision the line *consumed*
    (its own terminal record's ``goal_revision``), never the live value at
    registration time. A goal edit inside the race window -- after the line last
    read goal.md, before the scheduler registers the park -- must wake the line,
    never be absorbed into the baseline.

    The negative case is the spec's acceptance: line consumed rev=R0 (terminal
    goal_revision=R0, written earlier), goal.md later written to R1, and the
    scheduler only ``_establish_park``s after R1 exists. The next tick must be
    ``woken:goal_revision`` (current R1 != baseline R0). Unfixed -- baseline
    snapped to the live R1 -- the line parks forever and this turns red.
    """

    def test_a_goal_edit_before_parking_wakes_the_line(self, tmp_path: Path) -> None:
        # The line consumed rev-0; goal.md is already rev-1 by the time the
        # scheduler establishes the park (write happened inside the window).
        scheduler, clock, _ = blocked_line(
            tmp_path, wake=FakeWake(revision="sha256:rev-1"), goal_revision="sha256:rev-0"
        )

        # Establish happens now, after the goal write: the baseline must be the
        # consumed rev-0, not the live rev-1.
        result = scheduler.tick()[0]
        assert result.park_event == "established"
        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert state["parked_goal_revision"] == "sha256:rev-0"

        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:goal_revision"
        assert result.decision.ignite

    def test_the_baseline_never_takes_the_registration_moment_value(self, tmp_path: Path) -> None:
        """The rollback contrast, pinned: an establish that snapshotted the
        live revision (the pre-G1 behaviour) would record rev-1 here and the
        line would never wake. The snapshot must hold the consumed rev-0."""
        scheduler, _, _ = blocked_line(
            tmp_path, wake=FakeWake(revision="sha256:rev-1"), goal_revision="sha256:rev-0"
        )
        scheduler.tick()
        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert state["parked_goal_revision"] == "sha256:rev-0"
        assert state["parked_goal_revision"] != "sha256:rev-1"

    def test_no_repeat_wake_on_the_revision_already_consumed(self, tmp_path: Path) -> None:
        """Reverse, no jitter: the line woke, actually consumed R1, and blocked
        again with terminal goal_revision=R1. current==R1==baseline, so parking
        holds and nothing re-wakes."""
        scheduler, clock, _ = blocked_line(
            tmp_path, wake=FakeWake(revision="sha256:rev-1"), goal_revision="sha256:rev-1"
        )
        assert scheduler.tick()[0].park_event == "established"

        for _ in range(3):
            clock.now += 60.0
            result = scheduler.tick()[0]
            assert result.park_event is None
            assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION

    def test_a_terminal_without_a_consumed_revision_fails_open(self, tmp_path: Path) -> None:
        """Old/anomalous terminal (no goal_revision): no reliable baseline, so
        never park -- plain backoff keeps the line moving and the event names
        the reason."""
        scheduler, _, launcher = blocked_line(
            tmp_path, wake=FakeWake(revision="sha256:rev-1"), goal_revision=None
        )
        result = scheduler.tick()[0]
        assert result.parked is False
        assert result.park_event == "not_parked:no_consumed_revision"
        assert result.decision.ignite
        assert len(launcher.launched) == 1


class TestEscapeHatch:
    def test_clearing_the_parked_fields_reignites_immediately(self, tmp_path: Path) -> None:
        scheduler, clock, launcher = blocked_line(tmp_path, wake=FakeWake())
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION

        clear_parked_fields(tmp_path)
        clock.now += 60.0
        assert scheduler.tick()[0].decision.ignite
        assert len(launcher.launched) == 1

    def test_a_released_terminal_is_not_reparked(self, tmp_path: Path) -> None:
        """`park_considered_run_id` stays behind so the hatch is not a no-op."""
        scheduler, clock, _ = blocked_line(tmp_path, wake=FakeWake())
        scheduler.tick()

        clear_parked_fields(tmp_path)
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.parked is False
        assert result.park_event is None

    def test_deleting_the_whole_stall_file_also_releases(self, tmp_path: Path) -> None:
        """The pre-existing runbook hatch keeps working: the terminal comes
        back as an adopted baseline, considered but never parked."""
        scheduler, clock, _ = blocked_line(tmp_path, wake=FakeWake())
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION

        stall_file(tmp_path).unlink()
        clock.now += 60.0
        assert scheduler.tick()[0].decision.ignite

    def test_the_next_blocked_terminal_parks_again(self, tmp_path: Path) -> None:
        """Release is per-terminal: if the relaunched line blocks on the same
        decision again, the new terminal parks on its own accounting."""
        scheduler, clock, _ = blocked_line(tmp_path, wake=FakeWake())
        scheduler.tick()
        clear_parked_fields(tmp_path)
        clock.now += 60.0
        assert scheduler.tick()[0].decision.ignite  # released, relaunched

        write_blocked(tmp_path, run_id="run-b2")
        clock.now += 3600.0  # past the cooldown of that relaunch
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION


class TestEscalation:
    def test_the_parking_tick_asks_the_board_once_with_an_idempotency_key(
        self, tmp_path: Path
    ) -> None:
        board = FakeBoard()
        scheduler, clock, _ = blocked_line(tmp_path, wake=FakeWake(), board=board)
        result = scheduler.tick()[0]
        clock.now += 60.0
        scheduler.tick()

        assert len(board.asked) == 1
        assert board.asked[0]["idempotency_key"] == parked_question_key(
            folder_id="wf-1",
            run_id="run-b1",
            note_text=(
                "line wf-1 parked: blocked waiting on a human decision "
                "(run run-b1). blocker: 等监督面拍板（L2-5）"
            ),
        )
        assert "等监督面拍板" in board.asked[0]["question"]
        assert result.board_question == "question_sent:note-123"

    def test_a_refused_board_question_degrades_to_log_visibility(self, tmp_path: Path) -> None:
        """The known contract gap: work.note.v1 wants a ref to an existing
        card entity and a goal line has none. The bus refusing must cost
        nothing but the note."""
        board = FakeBoard(error=RuntimeError("ref target entity 'wf-1' not found"))
        scheduler, _, _ = blocked_line(tmp_path, wake=FakeWake(), board=board)
        result = scheduler.tick()[0]

        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert result.board_question.startswith("question_failed:RuntimeError")

    def test_no_board_still_parks(self, tmp_path: Path) -> None:
        scheduler, _, _ = blocked_line(tmp_path, wake=FakeWake(), board=None)
        result = scheduler.tick()[0]
        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert result.board_question is None


class TestCardMaterialisation:
    """The 422 fix: a goal line has no board card until its first escalation
    materialises one, and every question note refs that entity -- never the
    bare folder id the bus has no entity for."""

    def test_first_escalation_publishes_the_card_before_the_question(self, tmp_path: Path) -> None:
        board = FakeBoard()
        scheduler, _, _ = blocked_line(tmp_path, wake=FakeWake(), board=board)
        result = scheduler.tick()[0]

        assert board.publishes == ["card", "question"]
        assert board.cards[0]["idempotency_key"] == "goal-line-card:wf-1"
        payload = board.cards[0]["payload"]
        assert payload["title"] == "wf-1"
        assert payload["status"] == "doing"
        assert "wf-1" in payload["intent"]
        assert payload["work_folder_id"] == "wf-1"
        assert result.board_question == "question_sent:note-123"

    def test_the_question_refs_the_materialised_entity_not_the_folder_id(
        self, tmp_path: Path
    ) -> None:
        """Regression for the production 422: `DERIVATION_ERROR: ref target
        entity 'wf-…' not found`. The ask must ref the card entity the bus
        actually derived, and the folder id is not it."""
        board = FakeBoard()
        scheduler, _, _ = blocked_line(tmp_path, wake=FakeWake(), board=board)
        scheduler.tick()

        assert board.asked[0]["card_entity_id"] == "msg-card-1"
        assert board.asked[0]["card_entity_id"] != "wf-1"
        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert state["board_card_entity_id"] == "msg-card-1"

    def test_a_persisted_card_entity_is_not_republished(self, tmp_path: Path) -> None:
        """The card is per line: a second parking (new blocked terminal, new
        run) asks again but refs the card already in the stall-state file."""
        board = FakeBoard()
        scheduler, clock, _ = blocked_line(tmp_path, wake=FakeWake(), board=board)
        scheduler.tick()
        clear_parked_fields(tmp_path)
        clock.now += 60.0
        assert scheduler.tick()[0].decision.ignite  # released, relaunched

        write_blocked(tmp_path, run_id="run-b2")
        clock.now += 3600.0
        result = scheduler.tick()[0]

        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert board.publishes == ["card", "question", "question"]
        assert board.asked[1]["card_entity_id"] == "msg-card-1"
        assert board.asked[1]["idempotency_key"] == parked_question_key(
            folder_id="wf-1",
            run_id="run-b2",
            note_text=(
                "line wf-1 parked: blocked waiting on a human decision "
                "(run run-b2). blocker: 等监督面拍板（L2-5）"
            ),
        )

    def test_a_failed_card_publish_degrades_and_skips_the_question(self, tmp_path: Path) -> None:
        """Card materialisation failing must cost nothing but the escalation:
        the line still parks, and the question is not even attempted -- it
        would 422 against the same missing entity."""
        board = FakeBoard(card_error=RuntimeError("bus down"))
        scheduler, _, _ = blocked_line(tmp_path, wake=FakeWake(), board=board)
        result = scheduler.tick()[0]

        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert result.board_question.startswith("card_failed:RuntimeError")
        assert board.publishes == ["card"]
        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert state.get("board_card_entity_id") is None


PARKED_KEY_VARIANT_RE = re.compile(r"^parked:[^:]+:[^:]+:[0-9a-f]{12}$")


class TestParkQuestionKeyContentVariant:
    """#170 regression: the parked question key must carry a content-variant
    derived from the note body, so a re-park / retry that changes the blocker
    publishes under a *new* key instead of reusing the old one with a different
    intent (agent-bus 409 IDEMPOTENCY_CONFLICT, retryable=False)."""

    def test_changed_blocker_changes_the_key(self, tmp_path: Path) -> None:
        board = FakeBoard()
        scheduler, _, _ = make(tmp_path, wake=FakeWake(), board=board)
        line = LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", alias="canary", enabled=True)
        record: dict[str, Any] = {"run_id": "run-b1"}
        state: dict[str, Any] = {}

        scheduler._ask_board(line, record, "blocker A", state)
        scheduler._ask_board(line, record, "blocker B", state)

        keys = [asked["idempotency_key"] for asked in board.asked]
        assert len(keys) == 2
        assert keys[0] != keys[1]
        assert PARKED_KEY_VARIANT_RE.match(keys[0])
        assert PARKED_KEY_VARIANT_RE.match(keys[1])
        assert keys[0] != "parked:wf-1:run-b1"

    def test_unchanged_blocker_reuses_the_same_key(self, tmp_path: Path) -> None:
        board = FakeBoard()
        scheduler, _, _ = make(tmp_path, wake=FakeWake(), board=board)
        line = LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", alias="canary", enabled=True)
        record: dict[str, Any] = {"run_id": "run-b1"}
        state: dict[str, Any] = {}

        scheduler._ask_board(line, record, "same blocker", state)
        scheduler._ask_board(line, record, "same blocker", state)

        keys = [asked["idempotency_key"] for asked in board.asked]
        assert len(keys) == 2
        assert keys[0] == keys[1]

    def test_the_daemon_write_point_always_emits_the_content_variant(self, tmp_path: Path) -> None:
        """Write-point enumeration: the scheduler's `parked:` key construction
        must keep the content-variant. A regression to the invariant
        ``parked:<folder>:<run_id>`` turns this red."""
        board = FakeBoard()
        scheduler, _, _ = make(tmp_path, wake=FakeWake(), board=board)
        line = LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", alias="canary", enabled=True)
        scheduler._ask_board(line, {"run_id": "run-b1"}, None, {})

        key = board.asked[0]["idempotency_key"]
        assert key.startswith("parked:wf-1:run-b1:")
        assert PARKED_KEY_VARIANT_RE.match(key)
        assert key != "parked:wf-1:run-b1"


def hermetic_signals(tmp_path: Path, **kwargs: Any) -> LiveWakeSignals:
    """LiveWakeSignals whose line-token lookup can never hit the real
    /data/ronin/secrets on the test host -- it points into tmp_path."""
    kwargs.setdefault("line_token_template", str(tmp_path / "secrets" / "{alias}.token"))
    return LiveWakeSignals(**kwargs)


class TestLiveWakeSignals:
    """The production probes, over fakes of their transports."""

    def test_inbox_reads_the_newest_tail_not_the_oldest_page(self, tmp_path: Path) -> None:
        class FakeBus:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def messages(
                self, channel_id: str, *, limit: int = 100, after_seq: int = 0
            ) -> tuple[list[dict[str, Any]], int]:
                self.calls.append({"channel": channel_id, "limit": limit, "after_seq": after_seq})
                if after_seq == 0:
                    return [], 500
                return [{"created_at": "2026-08-27T12:00:00.123Z"}], 500

        bus = FakeBus()
        signals = hermetic_signals(tmp_path, bus_client=bus)
        assert signals.inbox_message_after("canary", BLOCKED_EPOCH) is True
        assert bus.calls[0]["channel"] == "agent:canary"
        assert bus.calls[1]["after_seq"] == 450

    def test_old_mail_does_not_wake(self, tmp_path: Path) -> None:
        class FakeBus:
            def messages(
                self, channel_id: str, *, limit: int = 100, after_seq: int = 0
            ) -> tuple[list[dict[str, Any]], int]:
                if after_seq == 0:
                    return [], 3
                return [{"created_at": "2026-08-27T09:00:00.000Z"}], 3

        signals = hermetic_signals(tmp_path, bus_client=FakeBus())
        assert signals.inbox_message_after("canary", BLOCKED_EPOCH) is False

    def test_an_empty_channel_does_not_wake(self, tmp_path: Path) -> None:
        class FakeBus:
            def messages(
                self, channel_id: str, *, limit: int = 100, after_seq: int = 0
            ) -> tuple[list[dict[str, Any]], int]:
                return [], 0

        signals = hermetic_signals(tmp_path, bus_client=FakeBus())
        assert signals.inbox_message_after("canary", BLOCKED_EPOCH) is False

    def test_goal_revision_comes_from_fs_stat(self) -> None:
        class FakeCaller:
            def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                assert tool == "fs_stat"
                assert arguments == {"folder_id": "wf-1", "filename": "goal.md"}
                return {"ok": True, "content_revision": "sha256:abc"}

        signals = LiveWakeSignals(wf_caller=FakeCaller())
        assert signals.goal_revision("wf-1") == "sha256:abc"

    def test_a_statless_answer_raises_rather_than_guessing(self) -> None:
        class FakeCaller:
            def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                return {"ok": True}

        signals = LiveWakeSignals(wf_caller=FakeCaller())
        try:
            signals.goal_revision("wf-1")
        except RuntimeError as exc:
            assert "content_revision" in str(exc)
        else:
            raise AssertionError("expected a RuntimeError")

    def test_timestamps_parse_at_both_precisions(self) -> None:
        assert parse_bus_timestamp("2026-08-27T10:00:00Z") == parse_bus_timestamp(
            "2026-08-27T10:00:00.999Z"
        )


class TestSourceDegradation:
    """The hotfix contract: the two wake sources degrade independently.

    The real fleet's inbox reads came back 403 -- the service token has no
    read ACL on `agent:*` channels, a structural gap, not a blip -- and the
    original one-try establish failed the whole parking open on it, every
    tick, forever (park_event `not_parked:probe_failed:BusError` on
    wf-7bc4d1). The goal.md anchor was perfectly checkable the whole time.
    """

    def test_an_inbox_403_still_parks_on_the_goal_anchor(self, tmp_path: Path) -> None:
        """The rollback contrast, pinned: restore the old combined try and
        this line never parks -- this test turns red."""
        from fleet_graph.bus.client import BusError

        wake = FakeWake(inbox_error=BusError(403, "Cannot read this channel"))
        scheduler, _, launcher = blocked_line(tmp_path, wake=wake)

        result = scheduler.tick()[0]

        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert result.park_event == "established:inbox_unavailable:BusError:403"
        assert launcher.launched == []

    def test_the_degraded_snapshot_records_the_source_as_unavailable(self, tmp_path: Path) -> None:
        from fleet_graph.bus.client import BusError

        wake = FakeWake(inbox_error=BusError(403, "Cannot read this channel"))
        scheduler, _, _ = blocked_line(tmp_path, wake=wake)
        scheduler.tick()

        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert state["parked_inbox_available"] is False
        assert state["parked_goal_revision"] == "sha256:rev-1"

    def test_a_degraded_park_never_probes_the_inbox_again(self, tmp_path: Path) -> None:
        """Availability is assessed once, at establishment. A 403 is an ACL
        gap that no per-tick probe will fix; the next parked terminal
        re-assesses at its own establishment. So an inbox recovering mid-park
        is deliberately not detected."""
        from fleet_graph.bus.client import BusError

        wake = FakeWake(inbox_error=BusError(403, "Cannot read this channel"))
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake)
        scheduler.tick()
        establish_probes = len(wake.inbox_calls)

        wake.inbox_error = None  # the ACL gets fixed mid-park
        wake.inbox = True
        for _ in range(3):
            clock.now += 60.0
            assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert len(wake.inbox_calls) == establish_probes

    def test_a_degraded_park_still_wakes_on_a_goal_edit(self, tmp_path: Path) -> None:
        from fleet_graph.bus.client import BusError

        wake = FakeWake(
            revision="sha256:rev-1", inbox_error=BusError(403, "Cannot read this channel")
        )
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake)
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION

        wake.revision = "sha256:rev-2"
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:goal_revision"
        assert result.decision.ignite

    def test_a_mid_park_inbox_error_skips_the_source_instead_of_waking(
        self, tmp_path: Path
    ) -> None:
        """Waking on a transient inbox error would re-ignite a line whose
        goal.md anchor is still perfectly checkable."""
        from fleet_graph.bus.client import BusError

        wake = FakeWake()
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake)
        assert scheduler.tick()[0].park_event == "established"

        wake.inbox_error = BusError(403, "alias rebound mid-park")
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert result.park_event is None

        wake.revision = "sha256:rev-2"  # the anchor still works
        clock.now += 60.0
        assert scheduler.tick()[0].park_event == "woken:goal_revision"

    def test_losing_the_goal_anchor_still_fails_the_whole_establish_open(
        self, tmp_path: Path
    ) -> None:
        """The goal.md anchor is the one fact parking stands on: without it
        there is nothing to wake on, so no parking -- unchanged semantics."""
        wake = FakeWake(revision_error=RuntimeError("mcp down"))
        scheduler, _, _ = blocked_line(tmp_path, wake=wake)
        result = scheduler.tick()[0]
        assert result.parked is False
        assert result.park_event == "not_parked:probe_failed:RuntimeError"
        assert result.decision.ignite

    def test_the_error_tag_carries_the_http_status(self) -> None:
        from fleet_graph.bus.client import BusError
        from fleet_graph.scheduler.wake import probe_error_tag

        assert probe_error_tag(BusError(403, "no ACL")) == "BusError:403"
        assert probe_error_tag(BusError(404, "no channel")) == "BusError:404"
        assert probe_error_tag(RuntimeError("boom")) == "RuntimeError"


class RecordingBus:
    """A fake bus that answers `messages` and remembers being asked."""

    def __init__(self, *, head_seq: int = 0) -> None:
        self.head_seq = head_seq
        self.calls: list[str] = []

    def messages(
        self, channel_id: str, *, limit: int = 100, after_seq: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(channel_id)
        return [], self.head_seq


class TestInboxCredential:
    """The inbox belongs to the line: `agent:{alias}` is private, owner-only
    readable, and the owner is the line's pump agent. So the probe presents
    the line's own mirrored token; the fleet-graph service token (which the
    channel ACL structurally 403s) is only the fallback, and the channel ACL
    is deliberately not widened."""

    def test_the_line_token_file_is_the_credential_when_present(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "canary.token").write_text("line-secret\n", encoding="utf-8")

        built_with: list[str] = []
        line_bus = RecordingBus()

        def factory(token: str) -> RecordingBus:
            built_with.append(token)
            return line_bus

        service = RecordingBus()
        signals = LiveWakeSignals(
            bus_client=service,
            line_token_template=str(secrets / "{alias}.token"),
            line_bus_factory=factory,
        )

        assert signals.inbox_message_after("canary", BLOCKED_EPOCH) is False
        assert built_with == ["line-secret"]  # the stripped file content, verbatim
        assert line_bus.calls == ["agent:canary"]
        assert service.calls == []  # the service token never touches the channel

    def test_a_missing_token_file_falls_back_to_the_service_token(self, tmp_path: Path) -> None:
        built_with: list[str] = []
        service = RecordingBus()
        signals = LiveWakeSignals(
            bus_client=service,
            line_token_template=str(tmp_path / "secrets" / "{alias}.token"),
            line_bus_factory=lambda token: built_with.append(token),
        )

        assert signals.inbox_message_after("canary", BLOCKED_EPOCH) is False
        assert built_with == []
        assert service.calls == ["agent:canary"]

    def test_a_successful_line_token_probe_marks_the_snapshot_available(
        self, tmp_path: Path
    ) -> None:
        """`parked_inbox_available` tells the truth about the credential that
        was actually used: probing with the line's token succeeds, so the
        source is available -- and mid-park inbox checks keep running."""

        class FakeCaller:
            def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                return {"ok": True, "content_revision": "sha256:rev-1"}

        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "canary.token").write_text("line-secret", encoding="utf-8")
        wake = LiveWakeSignals(
            bus_client=RecordingBus(),  # would 403 in production; must stay unused
            wf_caller=FakeCaller(),
            line_token_template=str(secrets / "{alias}.token"),
            line_bus_factory=lambda token: RecordingBus(),
        )
        scheduler, _, _ = blocked_line(tmp_path, wake=wake)

        result = scheduler.tick()[0]
        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert result.park_event == "established"
        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert state["parked_inbox_available"] is True

    def test_both_credentials_dead_still_parks_on_the_goal_anchor(self, tmp_path: Path) -> None:
        """No token file *and* the service token 403s: the inbox source
        degrades exactly as in #89 -- the goal.md anchor parks alone, nothing
        crashes, and the snapshot records the source as unavailable."""
        from fleet_graph.bus.client import BusError

        class DeniedBus:
            def messages(
                self, channel_id: str, *, limit: int = 100, after_seq: int = 0
            ) -> tuple[list[dict[str, Any]], int]:
                raise BusError(403, "Cannot read this channel")

        class FakeCaller:
            def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                return {"ok": True, "content_revision": "sha256:rev-1"}

        wake = LiveWakeSignals(
            bus_client=DeniedBus(),
            wf_caller=FakeCaller(),
            line_token_template=str(tmp_path / "secrets" / "{alias}.token"),
        )
        scheduler, _, launcher = blocked_line(tmp_path, wake=wake)

        result = scheduler.tick()[0]
        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert result.park_event == "established:inbox_unavailable:BusError:403"
        assert launcher.launched == []
        state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
        assert state["parked_inbox_available"] is False
        assert state["parked_goal_revision"] == "sha256:rev-1"

    def test_an_unsafe_alias_never_touches_the_filesystem(self, tmp_path: Path) -> None:
        """The alias is a path component of the token file; anything that
        could traverse out of the secrets dir skips straight to fallback."""
        service = RecordingBus()
        signals = LiveWakeSignals(
            bus_client=service,
            line_token_template=str(tmp_path / "secrets" / "{alias}.token"),
            line_bus_factory=lambda token: (_ for _ in ()).throw(
                AssertionError("no line client for an unsafe alias")
            ),
        )
        assert signals.inbox_message_after("../../etc/passwd", BLOCKED_EPOCH) is False
        assert service.calls == ["agent:../../etc/passwd"]
