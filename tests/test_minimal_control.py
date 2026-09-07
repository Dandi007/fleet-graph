"""Tests for fleet_graph.minimal.control: validate_control, ControlLog, views."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet_graph.minimal.control import (
    CONTROL_OPS,
    ControlLog,
    goal_list_row,
    goal_status_view,
    validate_control,
)
from fleet_graph.minimal.events import EventLog


def make_control_log(tmp_path: Path, goal_id: str = "g-001") -> ControlLog:
    return ControlLog(tmp_path / "goals" / goal_id)


def make_event_log(tmp_path: Path, goal_id: str = "g-001") -> EventLog:
    return EventLog(tmp_path / "goals" / goal_id)


# --- validate_control ------------------------------------------------------


def test_control_ops_enum() -> None:
    assert CONTROL_OPS == ("message", "steer", "stop", "resume")


@pytest.mark.parametrize(
    "op_obj",
    [
        {"op": "message", "text": "hello"},
        {"op": "steer", "patch": {"title": "new title"}},
        {"op": "stop", "mode": "graceful"},
        {"op": "stop", "mode": "kill"},
        {"op": "resume"},
    ],
)
def test_validate_control_valid(op_obj: dict) -> None:
    assert validate_control(op_obj) == []


def test_validate_control_not_a_dict() -> None:
    assert validate_control(["op", "message"]) != []
    assert validate_control("message") != []


def test_validate_control_op_missing_or_unknown() -> None:
    assert validate_control({}) != []
    assert validate_control({"text": "hi"}) != []
    assert validate_control({"op": "bogus"}) != []
    assert validate_control({"op": 42}) != []


@pytest.mark.parametrize(
    "op_obj",
    [
        {"op": "message"},
        {"op": "message", "text": ""},
        {"op": "message", "text": "   "},
        {"op": "message", "text": 123},
    ],
)
def test_validate_control_message_bad_text(op_obj: dict) -> None:
    errors = validate_control(op_obj)
    assert len(errors) == 1
    assert "text" in errors[0]


def test_validate_control_steering() -> None:
    assert validate_control({"op": "steer"}) != []  # patch missing
    assert validate_control({"op": "steer", "patch": []}) != []  # not a dict
    assert validate_control({"op": "steer", "patch": {}}) != []  # empty patch
    for patch in (
        {"goal_id": "g-999"},
        {"work_folder": "wf-x"},
        {"repo.path": "/other"},
        {"repo": {"path": "/other"}},
        {"title": "ok", "goal_id": "g-999"},
    ):
        errors = validate_control({"op": "steer", "patch": patch})
        assert errors, patch


def test_validate_control_stop_mode() -> None:
    assert validate_control({"op": "stop"}) != []  # mode missing
    assert validate_control({"op": "stop", "mode": "halt"}) != []
    assert validate_control({"op": "stop", "mode": None}) != []


def test_validate_control_resume_no_extra_fields() -> None:
    assert validate_control({"op": "resume"}) == []
    errors = validate_control({"op": "resume", "text": "go"})
    assert errors != []
    assert "extra" in errors[0]


# --- ControlLog ------------------------------------------------------------


def test_append_stamps_ts_seq_compact_key_order(tmp_path: Path) -> None:
    log = make_control_log(tmp_path)
    rec = log.append({"op": "message", "text": "继续"})
    assert rec["op"] == "message"
    assert rec["text"] == "继续"
    assert rec["seq"] == 1
    assert rec["ts"].endswith("+00:00")

    raw = log.path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    line = raw.splitlines()[0]
    assert line == json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    assert '"text": "继续"' not in line  # compact separators
    assert line.index('"ts"') < line.index('"seq"') < line.index('"op"')

    rec2 = log.append({"op": "resume"})
    assert rec2["seq"] == 2
    assert set(rec2) == {"ts", "seq", "op"}


def test_append_invalid_op_raises_and_writes_nothing(tmp_path: Path) -> None:
    log = make_control_log(tmp_path)
    with pytest.raises(ValueError):
        log.append({"op": "steer", "patch": {"goal_id": "g-2"}})
    with pytest.raises(ValueError):
        log.append({"op": "stop", "mode": "sigkill"})
    assert not log.path.exists()


def test_read_new_incremental(tmp_path: Path) -> None:
    log = make_control_log(tmp_path)
    log.append({"op": "message", "text": "a"})
    log.append({"op": "steer", "patch": {"title": "t"}})
    log.append({"op": "stop", "mode": "graceful"})

    assert [r["seq"] for r in log.read_new(0)] == [1, 2, 3]
    tail = list(log.read_new(1))
    assert [r["seq"] for r in tail] == [2, 3]
    assert tail[0]["op"] == "steer"
    assert tail[0]["patch"] == {"title": "t"}
    assert list(log.read_new(3)) == []
    assert list(log.read_new(99)) == []


def test_reopen_resumes_seq(tmp_path: Path) -> None:
    log = make_control_log(tmp_path)
    log.append({"op": "message", "text": "a"})
    log.append({"op": "resume"})

    log2 = make_control_log(tmp_path)
    assert log2.append({"op": "stop", "mode": "kill"})["seq"] == 3
    assert [r["seq"] for r in log2.read_new(0)] == [1, 2, 3]


def test_trailing_partial_line_ignored(tmp_path: Path) -> None:
    log = make_control_log(tmp_path)
    log.append({"op": "message", "text": "a"})
    log.append({"op": "resume"})
    with log.path.open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026-09-07T01:00:00+00:00", "seq": 3, "op": "m')

    assert [r["seq"] for r in log.read_new(0)] == [1, 2]
    log2 = make_control_log(tmp_path)
    assert log2.append({"op": "resume"})["seq"] == 3


def test_middle_corrupt_line_raises(tmp_path: Path) -> None:
    d = tmp_path / "goals" / "g-001"
    d.mkdir(parents=True)
    good1 = json.dumps(
        {"ts": "t1", "seq": 1, "op": "resume"}, ensure_ascii=False, separators=(",", ":")
    )
    good2 = json.dumps(
        {"ts": "t2", "seq": 2, "op": "resume"}, ensure_ascii=False, separators=(",", ":")
    )
    (d / "control.jsonl").write_text(
        good1 + "\n" + '{"half-written": ' + "\n" + good2 + "\n",
        encoding="utf-8",
    )
    log = ControlLog(d)
    with pytest.raises(ValueError):
        list(log.read_new(0))


# --- goal_status_view ------------------------------------------------------


def seed_running(elog: EventLog) -> None:
    elog.append("goal.enrolled", {"title": "demo"})
    elog.append("goal.turn.started", {})
    elog.append("dd.dispatched", {"spec": "docs/specs/x.md"}, dd_id="dd-03")
    elog.append("dd.stage.started", {"stage": "cr"}, dd_id="dd-03")


def test_goal_status_view_running(tmp_path: Path) -> None:
    elog = make_event_log(tmp_path)
    seed_running(elog)
    view = goal_status_view(elog.read(), alive=True)
    assert view["goal_id"] == "g-001"
    assert view["state"] == "running"
    assert view["step"] == "dd-03/cr"
    assert view["turn_no"] == 1
    assert view["dd_count"] == 1
    assert view["goal_version"] == 1
    assert view["last_seq"] == 4
    assert view["last_event_ts"]
    assert view["warnings"] == []
    assert view["current_dd"] == {"dd_id": "dd-03", "round": 1}
    assert "tail" not in view


def test_goal_status_view_crashed(tmp_path: Path) -> None:
    elog = make_event_log(tmp_path)
    seed_running(elog)
    view = goal_status_view(elog.read(), alive=False)
    assert view["state"] == "crashed"
    assert view["step"] == "dd-03/cr"


def test_goal_status_view_stopped(tmp_path: Path) -> None:
    elog = make_event_log(tmp_path)
    elog.append("goal.enrolled", {})
    elog.append("engine.exiting", {"reason": "stop"})
    view = goal_status_view(elog.read(), alive=False)
    assert view["state"] == "stopped"
    assert view["step"] == "stopped"


def test_goal_status_view_blocked_with_warning(tmp_path: Path) -> None:
    elog = make_event_log(tmp_path)
    elog.append("goal.enrolled", {})
    elog.append("goal.turn.started", {})
    elog.append("goal.warning", {"message": "spec 缺验收"})
    elog.append("goal.blocked", {"kind": "state_mismatch"})
    view = goal_status_view(elog.read(), alive=False)
    assert view["state"] == "blocked"
    assert view["warnings"] == ["spec 缺验收"]


def test_goal_status_view_done(tmp_path: Path) -> None:
    elog = make_event_log(tmp_path)
    elog.append("goal.enrolled", {})
    elog.append("goal.done", {})
    view = goal_status_view(elog.read(), alive=False)
    assert view["state"] == "done"
    assert view["current_dd"] is None


def test_goal_status_view_goal_turn_step_and_tail(tmp_path: Path) -> None:
    elog = make_event_log(tmp_path)
    elog.append("goal.enrolled", {})
    for _ in range(3):
        elog.append("goal.turn.started", {})
        elog.append("goal.turn.finished", {})
    elog.append("goal.turn.started", {})
    events = list(elog.read())
    view = goal_status_view(events, alive=True, tail_events=events[-2:])
    assert view["state"] == "running"
    assert view["step"] == "goal.turn#4"
    assert view["turn_no"] == 4
    assert [e["seq"] for e in view["tail"]] == [7, 8]
    assert view["tail"][0]["kind"] == "goal.turn.finished"


# --- goal_list_row ---------------------------------------------------------


def test_goal_list_row_reads_pid_warnings_title(tmp_path: Path) -> None:
    elog = make_event_log(tmp_path)
    elog.append("goal.enrolled", {"title": "重建调度器"})
    elog.append("goal.turn.started", {})
    elog.append("goal.warning", {"message": "预算超了"})
    elog.append("engine.started", {"pid": 4321})

    seen: list[int | None] = []

    def probe(pid: int | None) -> bool:
        seen.append(pid)
        return True

    row = goal_list_row(tmp_path / "goals" / "g-001", alive_probe=probe)
    assert seen == [4321]
    assert row["goal_id"] == "g-001"
    assert row["title"] == "重建调度器"
    assert row["state"] == "running"
    assert row["step"] == "goal.turn#1"
    assert row["turn_no"] == 1
    assert row["dd_count"] == 0
    assert row["last_event_ts"]
    assert row["warnings"] == ["预算超了"]
    assert row["pid"] == 4321


def test_goal_list_row_missing_pid_is_crashed(tmp_path: Path) -> None:
    elog = make_event_log(tmp_path)
    elog.append("goal.enrolled", {"title": "t"})
    elog.append("goal.turn.started", {})
    # 无 engine.started / engine.resumed：pid 取不到 → 视为不活 → crashed

    def probe(pid: int | None) -> bool:
        raise AssertionError("probe must not be consulted when pid is None")

    row = goal_list_row(tmp_path / "goals" / "g-001", alive_probe=probe)
    assert row["pid"] is None
    assert row["state"] == "crashed"
    assert "title" in row


def test_goal_list_row_last_engine_event_wins_and_dead_pid_crashes(
    tmp_path: Path,
) -> None:
    elog = make_event_log(tmp_path)
    elog.append("goal.enrolled", {})
    elog.append("engine.started", {"pid": 111})
    elog.append("engine.resumed", {"pid": 222})
    row = goal_list_row(tmp_path / "goals" / "g-001", alive_probe=lambda pid: pid == 222)
    assert row["pid"] == 222
    assert row["state"] == "running"

    dead = goal_list_row(tmp_path / "goals" / "g-001", alive_probe=lambda pid: False)
    assert dead["state"] == "crashed"


def test_goal_list_row_no_events_file(tmp_path: Path) -> None:
    empty = tmp_path / "goals" / "g-empty"
    empty.mkdir(parents=True)
    row = goal_list_row(empty, alive_probe=lambda pid: True)
    assert row["goal_id"] == "g-empty"
    assert row["pid"] is None
    assert row["state"] == "crashed"
    assert row["last_event_ts"] is None
    assert row["warnings"] == []
    assert "title" not in row
