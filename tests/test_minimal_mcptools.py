"""Tests for fleet_graph.minimal.mcptools: tool table, validation, read/write handlers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet_graph.minimal import mcptools
from fleet_graph.minimal.events import EventLog
from fleet_graph.minimal.mcptools import (
    TOOL_NAMES,
    TOOLS,
    SpawnPlan,
    default_observations_reader,
    goal_enroll,
    goal_events,
    goal_list,
    goal_message,
    goal_observations,
    goal_resume,
    goal_status,
    goal_steer,
    goal_stop,
    validate_tool_call,
)
from source_tools import executable_source


def make_event_log(engine_root: Path, goal_id: str) -> EventLog:
    return EventLog(engine_root / goal_id)


def seed_running(engine_root: Path, goal_id: str, title: str | None = None) -> None:
    elog = make_event_log(engine_root, goal_id)
    elog.append("goal.enrolled", {"title": title} if title else {})
    elog.append("goal.turn.started", {})


class _AllTrueProbe:
    def is_worktree(self, path: str) -> bool:
        return True

    def branch_exists(self, path: str, branch: str) -> bool:
        return True

    def bash_parses(self, command: str) -> bool:
        return True


def base_enroll() -> dict:
    return {
        "schema": "goal.enroll/2",
        "work_folder": "wf-ab12cd",
        "title": "重做最小系统",
        "goal_text": "把 fleet-graph 重建为最小系统。",
        "source_branch": "release/loopx-minimal",
        "repos": [
            {
                "path": "/data/wt/alpha",
                "remote": "origin",
                "target_branch": "main",
                "acceptance": ["make verify"],
            }
        ],
    }


# --- TOOLS 声明表 ----------------------------------------------------------


def test_tools_are_the_nine_in_spec_order() -> None:
    assert TOOL_NAMES == (
        "goal_enroll",
        "goal_list",
        "goal_status",
        "goal_events",
        "goal_message",
        "goal_steer",
        "goal_stop",
        "goal_resume",
        "goal_observations",
    )
    assert [tool.name for tool in TOOLS] == list(TOOL_NAMES)


def test_tools_read_write_classification() -> None:
    reads = {"goal_list", "goal_status", "goal_events", "goal_observations"}
    writes = {"goal_enroll", "goal_message", "goal_steer", "goal_stop", "goal_resume"}
    by_name = {tool.name: tool for tool in TOOLS}
    assert {name for name in by_name if by_name[name].kind == "read"} == reads
    assert {name for name in by_name if by_name[name].kind == "write"} == writes


def test_tool_required_fields_are_declared() -> None:
    by_name = {tool.name: tool for tool in TOOLS}
    assert by_name["goal_enroll"].required == ("enroll",)
    assert by_name["goal_list"].required == ()
    assert by_name["goal_message"].required == ("goal_id", "text")
    assert by_name["goal_steer"].required == ("goal_id", "patch")
    assert by_name["goal_stop"].required == ("goal_id", "mode")


# --- validate_tool_call ----------------------------------------------------


def test_unknown_tool_rejected() -> None:
    errors = validate_tool_call("nope", {})
    assert errors == ["unknown tool 'nope'"]


def test_missing_required_fields_rejected() -> None:
    errors = validate_tool_call("goal_message", {})
    assert "goal_id" in errors[0] or "goal_id" in errors[1]
    assert "text" in errors[0] or "text" in errors[1]


def test_wrong_type_rejected() -> None:
    errors = validate_tool_call("goal_message", {"goal_id": "g-7f3a2c", "text": 123})
    assert any("text" in e and "string" in e for e in errors)
    errors = validate_tool_call("goal_status", {"goal_id": 42})
    assert any("goal_id" in e and "string" in e for e in errors)


def test_args_must_be_an_object() -> None:
    assert validate_tool_call("goal_message", ["x"]) != []
    assert validate_tool_call("goal_message", "goal_id") != []


def test_valid_calls_have_no_errors() -> None:
    assert validate_tool_call("goal_message", {"goal_id": "g-7f3a2c", "text": "hi"}) == []
    assert validate_tool_call("goal_list", {}) == []
    assert validate_tool_call("goal_resume", {"goal_id": "g-7f3a2c"}) == []


# --- 读 handler ------------------------------------------------------------


def test_goal_list_scans_two_goals(tmp_path: Path) -> None:
    seed_running(tmp_path, "g-000001", title="甲")
    seed_running(tmp_path, "g-000002", title="乙")
    rows = goal_list(tmp_path)
    assert [row["goal_id"] for row in rows] == ["g-000001", "g-000002"]
    assert rows[0]["title"] == "甲"
    assert rows[1]["title"] == "乙"


def test_goal_list_empty_root(tmp_path: Path) -> None:
    assert goal_list(tmp_path) == []


def test_crashed_is_reported_and_never_auto_resumed(tmp_path: Path) -> None:
    seed_running(tmp_path, "g-000001")
    EventLog(tmp_path / "g-000001").append("engine.started", {"pid": 4242})
    rows = goal_list(tmp_path, alive_probe=lambda pid: False)
    assert rows[0]["state"] == "crashed"
    assert rows[0]["pid"] == 4242
    # 只标出来，绝不主动 resume：goal_list 只读，不写 control.jsonl、不产 SpawnPlan。
    assert not (tmp_path / "g-000001" / "control.jsonl").exists()


def test_goal_status_reuses_status_view(tmp_path: Path) -> None:
    seed_running(tmp_path, "g-000001")
    view = goal_status(tmp_path, "g-000001", tail=0)
    assert view["state"] == "crashed"  # no engine pid -> not alive
    assert "tail" not in view


def test_goal_status_tail(tmp_path: Path) -> None:
    elog = make_event_log(tmp_path, "g-000001")
    elog.append("goal.enrolled", {})
    elog.append("goal.turn.started", {})
    elog.append("engine.started", {"pid": 4242})
    view = goal_status(tmp_path, "g-000001", alive_probe=lambda pid: True)
    assert view["state"] == "running"
    assert [e["seq"] for e in view["tail"]] == [1, 2, 3]


def test_goal_events_since_seq(tmp_path: Path) -> None:
    elog = make_event_log(tmp_path, "g-000001")
    elog.append("goal.enrolled", {})
    elog.append("goal.turn.started", {})
    elog.append("goal.turn.finished", {})
    events = goal_events(tmp_path, "g-000001", since_seq=1)
    assert [e["seq"] for e in events] == [2, 3]
    assert events[0]["kind"] == "goal.turn.started"


def test_goal_observations_default_reader_and_filters(tmp_path: Path) -> None:
    root = tmp_path / "g-000001"
    root.mkdir(parents=True)
    lines = [
        {"ts": "2026-09-05T10:00:00+00:00", "severity": "info", "title": "a"},
        {"ts": "2026-09-05T11:00:00+00:00", "severity": "warn", "title": "b"},
        {"ts": "2026-09-05T12:00:00+00:00", "severity": "warn", "title": "c"},
    ]
    (root / "observations.jsonl").write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )

    assert default_observations_reader(root / "observations.jsonl") == lines

    by_severity = goal_observations(tmp_path, "g-000001", severity="warn")
    assert [obs["title"] for obs in by_severity] == ["b", "c"]

    since = goal_observations(tmp_path, "g-000001", since_ts="2026-09-05T10:30:00+00:00")
    assert [obs["title"] for obs in since] == ["b", "c"]

    both = goal_observations(
        tmp_path,
        "g-000001",
        since_ts="2026-09-05T10:30:00+00:00",
        severity="info",
    )
    assert both == []


def test_goal_observations_injected_reader(tmp_path: Path) -> None:
    def fake_reader(path: str) -> list[dict]:
        return [{"ts": "2026-09-05T12:00:00+00:00", "severity": "warn", "title": "x"}]

    rows = goal_observations(tmp_path, "g-000001", reader=fake_reader)
    assert [obs["title"] for obs in rows] == ["x"]


# --- 写 handler ------------------------------------------------------------


def test_goal_message_appends_exactly_one_line(tmp_path: Path) -> None:
    record = goal_message(tmp_path, "g-000001", "继续推进")
    assert record["op"] == "message"
    assert record["text"] == "继续推进"
    assert record["seq"] == 1

    control_path = tmp_path / "g-000001" / "control.jsonl"
    assert control_path.exists()
    lines = control_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["op"] == "message"
    # 只动 control.jsonl，不产 events.jsonl。
    assert not (tmp_path / "g-000001" / "events.jsonl").exists()


def test_goal_steer_appends_exactly_one_line(tmp_path: Path) -> None:
    record = goal_steer(tmp_path, "g-000001", {"title": "新标题"})
    assert record["op"] == "steer"
    assert record["patch"] == {"title": "新标题"}

    lines = (tmp_path / "g-000001" / "control.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert not (tmp_path / "g-000001" / "events.jsonl").exists()


def test_goal_steer_forbidden_field_rejected(tmp_path: Path) -> None:
    for patch in (
        {"goal_id": "g-999999"},
        {"work_folder": "wf-x"},
        {"repo.path": "/other"},
        {"repo": {"path": "/other"}},
    ):
        with pytest.raises(ValueError):
            goal_steer(tmp_path, "g-000001", patch)
    assert not (tmp_path / "g-000001" / "control.jsonl").exists()


def test_goal_stop_graceful_appends_one_line(tmp_path: Path) -> None:
    record = goal_stop(tmp_path, "g-000001", "graceful")
    assert record["op"] == "stop"
    assert record["mode"] == "graceful"


def test_goal_stop_kill_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        goal_stop(tmp_path, "g-000001", "kill")
    assert not (tmp_path / "g-000001" / "control.jsonl").exists()


def test_goal_resume_produces_spawn_plan(tmp_path: Path) -> None:
    plan = goal_resume(tmp_path, "g-7f3a2c")
    assert isinstance(plan, SpawnPlan)
    assert plan.action == "resume"
    assert plan.goal_id == "g-7f3a2c"
    assert plan.enroll is None
    assert plan.run_root == str(tmp_path / "g-7f3a2c")
    # 只产计划，不写任何文件。
    assert not (tmp_path / "g-7f3a2c").exists()


def test_goal_enroll_produces_spawn_plan(tmp_path: Path) -> None:
    plan = goal_enroll(
        base_enroll(), goal_id="g-7f3a2c", engine_root=tmp_path, git_probe=_AllTrueProbe()
    )
    assert isinstance(plan, SpawnPlan)
    assert plan.action == "enroll"
    assert plan.goal_id == "g-7f3a2c"
    assert plan.enroll is not None
    assert plan.enroll["goal_id"] == "g-7f3a2c"
    assert plan.enroll["repos"][0]["remote"] == "origin"


def test_goal_enroll_invalid_payload_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        goal_enroll({}, engine_root=tmp_path, git_probe=_AllTrueProbe())


def test_invalid_goal_id_shape_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        goal_message(tmp_path, "../../etc", "hi")
    with pytest.raises(ValueError):
        goal_events(tmp_path, "g-7f3a2c/..")


# --- 源码级断言 ------------------------------------------------------------


def test_module_does_not_import_subprocess_signal_or_kill() -> None:
    body = executable_source(Path(mcptools.__file__))
    for forbidden in ("subprocess", "signal", "kill"):
        assert forbidden not in body, f"{forbidden} is imported/used in mcptools.py"
    assert "langgraph" not in body
