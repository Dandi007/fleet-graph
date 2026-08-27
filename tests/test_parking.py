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
from pathlib import Path
from typing import Any

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
    """Scriptable wake facts. `error` makes every probe raise."""

    def __init__(
        self,
        *,
        revision: str = "sha256:rev-1",
        inbox: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.revision = revision
        self.inbox = inbox
        self.error = error
        self.inbox_calls: list[tuple[str, float]] = []
        self.revision_calls: list[str] = []

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        self.inbox_calls.append((alias, after_epoch))
        if self.error is not None:
            raise self.error
        return self.inbox

    def goal_revision(self, folder_id: str) -> str:
        self.revision_calls.append(folder_id)
        if self.error is not None:
            raise self.error
        return self.revision


class FakeTicket:
    question_note_id = "note-123"


class FakeBoard:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.asked: list[dict[str, Any]] = []

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> FakeTicket:
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
) -> tuple[Scheduler, Clock, FakeLauncher]:
    """A line the scheduler launched once, which then blocked.

    This is the production shape: the terminal is *accounted* (the stall file
    witnessed the launch), which is what makes it park-eligible. The clock ends
    up an hour past the block -- far beyond the streak-1 backoff -- so any
    refusal from here on is parking, not cooldown.
    """
    scheduler, clock, launcher = make(tmp_path, wake=wake, board=board, alias=alias)
    assert scheduler.tick()[0].decision.ignite  # the priming launch
    launcher.launched.clear()
    write_blocked(tmp_path, waiting_on=waiting_on)
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

    def test_an_unparseable_terminal_timestamp_fails_open(self, tmp_path: Path) -> None:
        scheduler, clock, _ = blocked_line(tmp_path, wake=FakeWake())
        write_blocked(tmp_path, at="not a timestamp", run_id="run-b2")
        clock.now += 60.0
        assert scheduler.tick()[0].decision.ignite


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
        assert board.asked[0]["idempotency_key"] == "parked:wf-1:run-b1"
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


class TestLiveWakeSignals:
    """The production probes, over fakes of their transports."""

    def test_inbox_reads_the_newest_tail_not_the_oldest_page(self) -> None:
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
        signals = LiveWakeSignals(bus_client=bus)
        assert signals.inbox_message_after("canary", BLOCKED_EPOCH) is True
        assert bus.calls[0]["channel"] == "agent:canary"
        assert bus.calls[1]["after_seq"] == 450

    def test_old_mail_does_not_wake(self) -> None:
        class FakeBus:
            def messages(
                self, channel_id: str, *, limit: int = 100, after_seq: int = 0
            ) -> tuple[list[dict[str, Any]], int]:
                if after_seq == 0:
                    return [], 3
                return [{"created_at": "2026-08-27T09:00:00.000Z"}], 3

        signals = LiveWakeSignals(bus_client=FakeBus())
        assert signals.inbox_message_after("canary", BLOCKED_EPOCH) is False

    def test_an_empty_channel_does_not_wake(self) -> None:
        class FakeBus:
            def messages(
                self, channel_id: str, *, limit: int = 100, after_seq: int = 0
            ) -> tuple[list[dict[str, Any]], int]:
                return [], 0

        signals = LiveWakeSignals(bus_client=FakeBus())
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
