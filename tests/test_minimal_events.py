"""Tests for fleet_graph.minimal.events: append/fsync log, read, fold, resume_point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet_graph.minimal.events import (
    KINDS,
    EventLog,
    fold,
    resume_point,
)


def make_log(tmp_path: Path, goal_id: str = "g-001") -> EventLog:
    return EventLog(tmp_path / "goals" / goal_id)


def _line(
    goal_id: str,
    kind: str,
    seq: int,
    *,
    dd_id: str | None = None,
    payload: dict | None = None,
) -> str:
    return json.dumps(
        {
            "ts": "2026-09-07T01:00:00+00:00",
            "goal_id": goal_id,
            "dd_id": dd_id,
            "kind": kind,
            "seq": seq,
            "payload": payload or {},
        }
    )


def test_append_three_read_back_seq_and_content(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    e1 = log.append("goal.enrolled", {"title": "t"})
    e2 = log.append("goal.turn.started", {}, dd_id="dd-01")
    e3 = log.append("dd.stage.finished", {"stage": "fr", "stop": "pass"}, dd_id="dd-01")

    events = list(log.read())
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[0] == e1
    assert events[1] == e2
    assert events[2] == e3
    assert events[1].payload == {}
    assert events[1].dd_id == "dd-01"
    assert events[2].payload == {"stage": "fr", "stop": "pass"}
    assert all(e.goal_id == "g-001" for e in events)
    assert all(e.ts for e in events)


def test_reopen_resumes_seq(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append("goal.enrolled", {})
    log.append("goal.turn.started", {})
    log.append("goal.turn.finished", {})
    assert log.append("goal.warning", {}).seq == 4

    log2 = make_log(tmp_path)
    assert [e.seq for e in log2.read()] == [1, 2, 3, 4]
    assert log2.append("goal.blocked", {}).seq == 5


def test_unknown_kind_raises(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    with pytest.raises(ValueError):
        log.append("goal.bogus", {})


def test_kinds_match_protocol_section_8() -> None:
    assert (
        frozenset(
            {
                "goal.enrolled",
                "goal.turn.started",
                "goal.turn.finished",
                "goal.done",
                "goal.blocked",
                "goal.warning",
                "goal.message",
                "goal.steered",
                "goal.merged_to_target",
                "goal.dispatch_rejected",
                "dd.dispatched",
                "dd.stage.started",
                "dd.stage.finished",
                "dd.pr_opened",
                "dd.acceptance",
                "dd.review_requested",
                "dd.approved",
                "dd.rejected",
                "dd.merged",
                "dd.failed",
                "agent.spawned",
                "agent.exited",
                "agent.failed",
                "agent.invalid_output",
                "agent.compacted",
                "engine.started",
                "engine.resumed",
                "engine.exiting",
                "control.received",
            }
        )
        == KINDS
    )


def test_read_tolerates_trailing_partial_line(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append("goal.enrolled", {})
    log.append("goal.turn.started", {})
    log.append("goal.turn.finished", {})
    with log.path.open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026-09-07T01:00:00+00:00", "seq": 4, "k')

    events = list(log.read())
    assert [e.seq for e in events] == [1, 2, 3]


def test_read_raises_on_middle_corrupt_line(tmp_path: Path) -> None:
    d = tmp_path / "goals" / "g-001"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text(
        "\n".join(
            [
                _line("g-001", "goal.enrolled", 1),
                '{"half-written": ',
                _line("g-001", "goal.done", 2),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    log = EventLog(d)
    with pytest.raises(ValueError):
        list(log.read())


def test_fold_turn_no_dd_history_goal_version(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append("goal.enrolled", {})
    log.append("goal.steered", {"version": 2, "diff": {}})
    log.append("goal.steered", {"version": 3, "diff": {}})
    log.append("goal.turn.started", {})
    log.append("goal.turn.finished", {})
    log.append("dd.dispatched", {"spec": "x"}, dd_id="dd-01")
    log.append("dd.stage.started", {"stage": "impl"}, dd_id="dd-01")
    log.append("dd.stage.finished", {"stage": "impl", "stop": "pass"}, dd_id="dd-01")
    log.append("dd.merged", {}, dd_id="dd-01")
    log.append("dd.dispatched", {"spec": "y"}, dd_id="dd-02")
    log.append("dd.failed", {"detail": "boom"}, dd_id="dd-02")

    state = fold(log.read())

    assert state.turn_no == 1
    assert state.goal_version == 3
    assert state.state == "running"
    assert state.terminal is False
    assert state.last_seq == 11
    assert [(s.dd_id, s.outcome, s.rounds) for s in state.dd_history] == [
        ("dd-01", "merged", 1),
        ("dd-02", "failed", 1),
    ]
    assert state.current_dd.dd_id is None


def test_fold_terminal_states(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append("goal.enrolled", {})
    log.append("goal.done", {})
    state = fold(log.read())
    assert state.state == "done"
    assert state.terminal is True


def test_resume_point_lost_agent_run(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append("goal.enrolled", {})
    log.append("dd.dispatched", {}, dd_id="dd-01")
    log.append("dd.stage.started", {"stage": "impl"}, dd_id="dd-01")
    log.append("agent.spawned", {"role": "impl"}, dd_id="dd-01")

    rp = resume_point(log.read())
    assert rp.action == "restart_step"
    assert rp.stage == "impl"
    assert rp.dd_id == "dd-01"
    assert rp.detail == "lost_on_restart"


def test_resume_point_acceptance_midway(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append("goal.enrolled", {})
    log.append("dd.dispatched", {}, dd_id="dd-01")
    log.append("dd.acceptance", {"cmd": "make test", "exit": 0}, dd_id="dd-01")

    rp = resume_point(log.read())
    assert rp.action == "rerun_acceptance"
    assert rp.dd_id == "dd-01"


def test_resume_point_boundary_next_step(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append("goal.enrolled", {})
    log.append("dd.dispatched", {}, dd_id="dd-01")
    log.append("dd.stage.finished", {"stage": "fr", "stop": "pass"}, dd_id="dd-01")

    rp = resume_point(log.read())
    assert rp.action == "next_step"
    assert rp.dd_id == "dd-01"
    assert rp.stage == "fr"


def test_resume_point_terminal_exit(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append("goal.enrolled", {})
    log.append("goal.done", {})

    rp = resume_point(log.read())
    assert rp.action == "exit"
