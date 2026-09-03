"""缺陷⑭ wake fact: a board decision wakes a `waiting_decision` park (wf-8d9737).

The D5 gate ruling lands on `board:work-notes` as a `work.decision.v1`, but
that fact was missing from the wake vocabulary: a line parked in
`waiting_decision` stayed parked until an operator resumed it by hand, even
though its ruling was sitting on the board. The contract under test:

- positive: a parked line + a targeted, signed, fresh ruling -> the next tick
  wakes (`woken:board_decision`) and ignites the next generation;
- negative (off-target): a ruling referencing *another* question, one with no
  refs, or an unsigned one (`decided_by` empty) is not the fact -> no wake;
- negative (time): a ruling older than the parking instant is not the fact;
- negative (privilege): the probe is two plain GETs -- publish/consume/lease
  never exist on its path -- and a probe failure fails open, never a lock;
- regression: deleting the new probe's emission turns these red (the wake is
  the new fact's work, not polling luck), and the existing vocabulary
  (inbox / goal.md) still fires verbatim.

The question note id comes from the stall-state field the parking escalation
already writes (`board_question_note_id`) -- reused, never re-created.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet_graph.bus.board import DECISION_KIND, WORK_NOTES
from fleet_graph.bus.client import BusError
from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig
from fleet_graph.scheduler.ignition import Refusal
from fleet_graph.scheduler.launcher import LaunchResult
from fleet_graph.scheduler.wake import LiveWakeSignals, parse_bus_timestamp

BLOCKED_AT = "2026-08-27T10:00:00Z"
BLOCKED_EPOCH = parse_bus_timestamp(BLOCKED_AT)
#: When the priming launch happens: half an hour before the line blocks.
PRIME_EPOCH = BLOCKED_EPOCH - 1800.0
#: When the tests tick: an hour after the block -- the park establishes here,
#: so the parking instant (`parked_at`) is exactly TICK_EPOCH.
TICK_EPOCH = BLOCKED_EPOCH + 3600.0
#: The park's question note id, as `_ask_board` persists it.
QUESTION_ID = "note-123"
#: A ruling created after the park (11:00) but before the wake tick (12:00).
WAKE_AT = "2026-08-27T11:30:00Z"
WAKE_EPOCH = parse_bus_timestamp(WAKE_AT)
#: A ruling created between the block and the park: the time-direction negative.
STALE_AT = "2026-08-27T10:30:00Z"

FOLDER_ID = "wf-1"


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
    """Scriptable wake facts; the board-decision probe records its calls."""

    def __init__(
        self,
        *,
        revision: str = "sha256:rev-1",
        inbox: bool = False,
        decision: bool = False,
        decision_error: Exception | None = None,
    ) -> None:
        self.revision = revision
        self.inbox = inbox
        self.decision = decision
        self.decision_error = decision_error
        self.decision_calls: list[tuple[str, float]] = []

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        return self.inbox

    def goal_revision(self, folder_id: str) -> str:
        return self.revision

    def decision_landed(self, question_note_id: str, after_epoch: float) -> bool:
        self.decision_calls.append((question_note_id, after_epoch))
        if self.decision_error is not None:
            raise self.decision_error
        return self.decision


class FakeTicket:
    question_note_id = QUESTION_ID


class FakePublishResult:
    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self.message_id = entity_id
        self.channel_seq = 1
        self.deduplicated = False


class FakeBoard:
    """Publishes the card and answers the park question, like the real board."""

    def publish_card(self, payload: dict[str, Any], idempotency_key: str) -> FakePublishResult:
        return FakePublishResult("msg-card-1")

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> FakeTicket:
        return FakeTicket()


def make(
    tmp_path: Path,
    *,
    wake: Any = None,
    board: Any = None,
) -> tuple[Scheduler, Clock, FakeLauncher]:
    clock = Clock(PRIME_EPOCH)
    launcher = FakeLauncher()
    scheduler = Scheduler(
        SchedulerConfig(
            lines=[
                LineSpec(folder_id=FOLDER_ID, seat="opencode-dsv4pro", alias="canary", enabled=True)
            ],
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


def write_blocked(tmp_path: Path) -> None:
    record = {
        "terminal": "blocked",
        "rounds": 0,
        "run_id": "run-b1",
        "at": BLOCKED_AT,
        "reason": "等监督面拍板（L2-5）",
        "waiting_on": "decision",
        "goal_revision": "sha256:rev-1",
    }
    path = tmp_path / "runs" / FOLDER_ID / "terminal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def blocked_line(
    tmp_path: Path,
    *,
    wake: Any = None,
    board: Any = None,
) -> tuple[Scheduler, Clock, FakeLauncher]:
    """A launched line that blocked on a decision, clocked an hour past it."""
    scheduler, clock, launcher = make(tmp_path, wake=wake, board=board)
    assert scheduler.tick()[0].decision.ignite  # the priming launch
    launcher.launched.clear()
    write_blocked(tmp_path)
    clock.now = TICK_EPOCH
    return scheduler, clock, launcher


def stall_file(tmp_path: Path) -> Path:
    return tmp_path / "runs" / ".scheduler" / f"{FOLDER_ID}.json"


def seed_question_note_id(tmp_path: Path, note_id: str = QUESTION_ID) -> None:
    """Write the question id exactly as `_ask_board` leaves it after a park."""
    state = json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))
    state["board_question_note_id"] = note_id
    stall_file(tmp_path).write_text(json.dumps(state), encoding="utf-8")


def parked_state(tmp_path: Path) -> dict[str, Any]:
    return json.loads(stall_file(tmp_path).read_text(encoding="utf-8"))


class TestBoardDecisionWake:
    """Positive: the ruling lands, the line comes back on the next tick."""

    def test_a_targeted_ruling_wakes_the_line_and_ignites(self, tmp_path: Path) -> None:
        board = FakeBoard()
        wake = FakeWake()
        scheduler, clock, launcher = blocked_line(tmp_path, wake=wake, board=board)

        result = scheduler.tick()[0]
        assert result.park_event == "established"
        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        # The question id the parking escalation already wrote is the probe's
        # target -- reused, no second field invented.
        assert parked_state(tmp_path)["board_question_note_id"] == QUESTION_ID

        wake.decision = True
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:board_decision"
        assert result.decision.ignite
        assert len(launcher.launched) == 1

    def test_the_wake_clears_the_park_but_never_the_question_record(self, tmp_path: Path) -> None:
        """唤醒≠消费: the wake only lets the line come back to observe the
        fact; the question note id (the decision surface's anchor) survives,
        and no ruling state is written by the wake."""
        wake = FakeWake()
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake)
        seed_question_note_id(tmp_path)
        scheduler.tick()
        wake.decision = True
        clock.now += 60.0
        scheduler.tick()

        after = parked_state(tmp_path)
        assert after["parked_run_id"] is None
        assert after["parked_at"] is None
        assert after["board_question_note_id"] == QUESTION_ID

    def test_the_probe_targets_the_question_note_and_the_parking_instant(
        self, tmp_path: Path
    ) -> None:
        """The regression canary: the wake must come from *this* probe. Delete
        the probe's emission and `decision_calls` stays empty while the line
        never wakes -- either way this turns red."""
        wake = FakeWake()
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake)
        seed_question_note_id(tmp_path)
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION

        wake.decision = True
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert wake.decision_calls == [(QUESTION_ID, TICK_EPOCH)]
        assert result.park_event == "woken:board_decision"
        assert result.decision.ignite

    def test_without_a_question_note_id_the_probe_is_skipped(self, tmp_path: Path) -> None:
        """No board escalation happened (or it failed): there is no target to
        probe, so the source stays silent and the other facts hold the park."""
        wake = FakeWake()
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake)
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION

        wake.decision = True
        clock.now += 60.0
        scheduler.tick()
        assert wake.decision_calls == []
        assert scheduler.tick  # and the park logic ran without a crash

    def test_the_existing_vocabulary_still_fires_verbatim(self, tmp_path: Path) -> None:
        """inbox / goal.md behaviour is untouched: with no ruling on the board,
        a goal.md edit wakes exactly as before."""
        wake = FakeWake()
        scheduler, clock, _ = blocked_line(tmp_path, wake=wake)
        seed_question_note_id(tmp_path)
        assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION

        wake.revision = "sha256:rev-2"
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:goal_revision"
        assert result.decision.ignite


class TestBoardDecisionNegatives:
    """The negatives: nothing but this park's own fresh, signed ruling wakes."""

    def _parked(self, tmp_path: Path) -> tuple[Scheduler, Clock, FakeLauncher, FakeWake]:
        wake = FakeWake()
        scheduler, clock, launcher = blocked_line(tmp_path, wake=wake)
        seed_question_note_id(tmp_path)
        result = scheduler.tick()[0]
        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        return scheduler, clock, launcher, wake

    def test_no_ruling_keeps_the_line_parked(self, tmp_path: Path) -> None:
        scheduler, clock, launcher, _wake = self._parked(tmp_path)
        for _ in range(3):
            clock.now += 60.0
            result = scheduler.tick()[0]
            assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
            assert result.park_event is None
        assert launcher.launched == []

    def test_an_off_target_ruling_is_not_a_wake_fact(self, tmp_path: Path) -> None:
        """The probe (not the scheduler) decides targeting; a False answer --
        refs pointing elsewhere, no refs, unsigned -- must hold the park."""
        scheduler, clock, launcher, wake = self._parked(tmp_path)
        wake.decision = False  # the probe says: not our ruling
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.decision.refusal is Refusal.PARKED_AWAITING_DECISION
        assert launcher.launched == []

    def test_a_probe_failure_fails_open_never_locks(self, tmp_path: Path) -> None:
        """探针挂 → fail-open: the line is treated as not parked and falls
        back to plain backoff, with the failure mechanically attributed."""
        scheduler, clock, launcher, wake = self._parked(tmp_path)
        wake.decision_error = BusError(403, "board ACL gap")
        clock.now += 60.0
        result = scheduler.tick()[0]
        assert result.park_event == "woken:probe_failed:BusError:403"
        assert result.decision.ignite
        assert len(launcher.launched) == 1


def decision_message(
    message_id: str = "d1",
    *,
    target: str | None = QUESTION_ID,
    kind: str = DECISION_KIND,
    created_at: str = WAKE_AT,
    decided_by: str | None = "alice",
) -> dict[str, Any]:
    """One served `board:work-notes` message, shaped like the bus serves it."""
    message: dict[str, Any] = {
        "message_id": message_id,
        "kind": kind,
        "created_at": created_at,
        "channel_seq": 2,
        "payload": {"decision": "APPROVE"},
    }
    if decided_by is not None:
        message["payload"]["decided_by"] = decided_by
    if target is not None:
        message["refs"] = [{"target_entity": target}]
    return message


class RecordingBus:
    """A fake bus exposing only the probe's read surface.

    Any attribute beyond `refs_to`/`messages` -- publish, consume, lease,
    anything write-shaped -- fails the test, so the read-only discipline is
    enforced by construction rather than by assertion after the fact.
    """

    def __init__(
        self,
        *,
        refs: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        head_seq: int = 0,
        refs_error: Exception | None = None,
    ) -> None:
        self.refs = refs or []
        self.messages_list = messages or []
        self.head_seq = head_seq
        self.refs_error = refs_error
        self.calls: list[tuple[Any, ...]] = []

    def refs_to(self, entity_id: str) -> list[dict[str, Any]]:
        self.calls.append(("refs_to", entity_id))
        if self.refs_error is not None:
            raise self.refs_error
        return self.refs

    def messages(
        self, channel_id: str, *, limit: int = 100, after_seq: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(("messages", channel_id, limit, after_seq))
        return self.messages_list, self.head_seq

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the wake probe touched a non-read bus surface: {name}")


def live_signals(bus: RecordingBus) -> LiveWakeSignals:
    return LiveWakeSignals(bus_client=bus)


class TestDecisionLandedProbe:
    """The production probe, over fakes of its transport."""

    def test_a_targeted_signed_fresh_ruling_is_true(self) -> None:
        bus = RecordingBus(
            refs=[{"message_id": "d1", "target_entity": QUESTION_ID}],
            messages=[decision_message("d1")],
            head_seq=5,
        )
        assert live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH) is True
        # Read discipline: the refs lookup for the question, then the channel
        # tail on board:work-notes -- both plain GETs.
        assert ("refs_to", QUESTION_ID) in bus.calls
        assert any(call[0] == "messages" and call[1] == WORK_NOTES for call in bus.calls)

    def test_inline_refs_on_the_served_message_also_match(self) -> None:
        bus = RecordingBus(refs=[], messages=[decision_message("d9")], head_seq=1)
        assert live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH) is True

    def test_a_ruling_for_another_question_is_false(self) -> None:
        bus = RecordingBus(
            refs=[{"message_id": "d1", "target_entity": "note-other"}],
            messages=[decision_message("d1", target="note-other")],
            head_seq=5,
        )
        assert live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH) is False

    def test_a_ruling_with_no_refs_is_false(self) -> None:
        bus = RecordingBus(refs=[], messages=[decision_message("d1", target=None)], head_seq=1)
        assert live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH) is False

    def test_an_unsigned_ruling_is_false(self) -> None:
        bus = RecordingBus(
            refs=[{"message_id": "d1", "target_entity": QUESTION_ID}],
            messages=[decision_message("d1", decided_by=None)],
            head_seq=5,
        )
        assert live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH) is False

    def test_a_whitespace_decided_by_is_false(self) -> None:
        bus = RecordingBus(
            refs=[{"message_id": "d1", "target_entity": QUESTION_ID}],
            messages=[decision_message("d1", decided_by="   ")],
            head_seq=5,
        )
        assert live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH) is False

    def test_a_note_is_not_a_ruling(self) -> None:
        bus = RecordingBus(
            refs=[{"message_id": "d1", "target_entity": QUESTION_ID}],
            messages=[decision_message("d1", kind="work.note.v1")],
            head_seq=5,
        )
        assert live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH) is False

    def test_a_ruling_older_than_the_parking_instant_is_false(self) -> None:
        bus = RecordingBus(
            refs=[{"message_id": "d1", "target_entity": QUESTION_ID}],
            messages=[decision_message("d1", created_at=STALE_AT)],
            head_seq=5,
        )
        assert live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH) is False

    def test_a_ruling_exactly_at_the_parking_instant_is_false(self) -> None:
        """`created_at > after_epoch` is strict: a stamp equal to the parking
        instant predates the park by construction."""
        at_the_instant = "2026-08-27T11:00:00Z"
        assert parse_bus_timestamp(at_the_instant) == TICK_EPOCH
        bus = RecordingBus(
            refs=[{"message_id": "d1", "target_entity": QUESTION_ID}],
            messages=[decision_message("d1", created_at=at_the_instant)],
            head_seq=5,
        )
        assert live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH) is False

    def test_the_channel_tail_is_read_not_the_oldest_page(self) -> None:
        """The bus pages ascending: a plain read returns the oldest page, where
        a fresh ruling on a long-lived channel never sits (the 2026-08-31
        production gate outage). The probe must learn head_seq first."""
        bus = RecordingBus(
            refs=[{"message_id": "d1", "target_entity": QUESTION_ID}],
            messages=[decision_message("d1")],
            head_seq=5000,
        )
        assert live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH) is True
        tail_calls = [call for call in bus.calls if call[0] == "messages" and call[2] != 1]
        assert tail_calls[-1][3] == 5000 - 200  # after_seq = head - DECISION_TAIL_WINDOW

    def test_failures_raise_for_the_caller_to_fail_open(self) -> None:
        bus = RecordingBus(refs_error=BusError(403, "no ACL"))
        try:
            live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH)
        except BusError:
            pass
        else:
            raise AssertionError("expected the probe to raise, not guess")

    def test_an_empty_board_is_false(self) -> None:
        bus = RecordingBus()
        assert live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH) is False

    def test_the_probe_never_writes(self) -> None:
        """越权面: the only bus surfaces on the probe's path are the two
        reads -- RecordingBus fails the test on anything else."""
        bus = RecordingBus(
            refs=[{"message_id": "d1", "target_entity": QUESTION_ID}],
            messages=[decision_message("d1")],
            head_seq=5,
        )
        live_signals(bus).decision_landed(QUESTION_ID, TICK_EPOCH)
        assert {call[0] for call in bus.calls} == {"refs_to", "messages"}


def test_fixture_time_story_is_coherent() -> None:
    """The fixtures' clock: park 11:00 < ruling 11:30 < wake tick 12:00, and
    the stale ruling sits between the block (10:00) and the park."""
    assert TICK_EPOCH < WAKE_EPOCH
    assert BLOCKED_EPOCH < parse_bus_timestamp(STALE_AT) < TICK_EPOCH
