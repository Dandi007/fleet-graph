"""Tests for fleet_graph.minimal.steer: apply_patch, steered payload, projections."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from fleet_graph.minimal.control import FORBIDDEN_PATCH_KEYS, validate_control
from fleet_graph.minimal.events import Event, EventLog, fold
from fleet_graph.minimal.steer import (
    IMMUTABLE_FIELDS,
    apply_patch,
    current_goal,
    steer_diff_since,
    steered_payload,
)

ENROLL: dict = {
    "schema": "goal.enroll/1",
    "goal_id": "g-001",
    "work_folder": "wf-ab12cd",
    "title": "把 X 功能做出来",
    "acceptance": ["make test"],
    "repo": {"path": "/data/code/self/foo", "target_branch": "main"},
}


def steered_event(
    seq: int,
    version: int,
    *,
    changed: dict | None = None,
    added: dict | None = None,
    note: str | None = None,
) -> Event:
    diff = {"changed": changed or {}, "added": added or {}}
    return Event(
        ts=f"2026-09-07T01:{seq:02d}:00+00:00",
        goal_id="g-001",
        dd_id=None,
        kind="goal.steered",
        seq=seq,
        payload=steered_payload(version, diff, note),
    )


def other_event(seq: int, kind: str = "goal.turn.started") -> Event:
    return Event(
        ts=f"2026-09-07T00:{seq:02d}:00+00:00",
        goal_id="g-001",
        dd_id=None,
        kind=kind,
        seq=seq,
        payload={},
    )


# --- IMMUTABLE_FIELDS 与 control 同一份规则 ---------------------------------


def test_immutable_fields_is_control_rule() -> None:
    assert IMMUTABLE_FIELDS == FORBIDDEN_PATCH_KEYS == ("goal_id", "work_folder", "repo.path")


@pytest.mark.parametrize(
    "patch",
    [
        {"title": "ok"},
        {"repo": {"target_branch": "dev"}},
        {"title": "t", "deadline": "d"},
        {},
        [],
        "x",
        42,
        None,
        {"goal_id": "g-999"},
        {"work_folder": "wf-x"},
        {"repo.path": "/other"},
        {"repo": {"path": "/other"}},
        {"repo": {"path": "/same/path", "target_branch": "dev"}},
        {"title": "ok", "goal_id": "g-999"},
    ],
)
def test_apply_patch_rejection_set_matches_control(patch: object) -> None:
    """apply_patch 拒绝的 patch 集合与 validate_control 的 steer 校验完全一致。"""
    control_rejects = validate_control({"op": "steer", "patch": patch}) != []
    if control_rejects:
        with pytest.raises(ValueError):
            apply_patch(dict(ENROLL), patch)
    else:
        new_goal, diff = apply_patch(dict(ENROLL), patch)
        assert new_goal
        assert diff["changed"] or diff["added"]


# --- apply_patch -------------------------------------------------------------


def test_apply_patch_splits_changed_and_added() -> None:
    goal = copy.deepcopy(ENROLL)
    new_goal, diff = apply_patch(goal, {"title": "新标题", "deadline": "2026-10-01"})
    assert diff == {
        "changed": {"title": "新标题"},
        "added": {"deadline": "2026-10-01"},
    }
    assert new_goal["title"] == "新标题"
    assert new_goal["deadline"] == "2026-10-01"
    assert new_goal["acceptance"] == ["make test"]


def test_apply_patch_never_mutates_or_aliases_inputs() -> None:
    goal = copy.deepcopy(ENROLL)
    patch = {"repo": {"target_branch": "dev"}, "warn": {"turns": 30}}
    goal_snap = copy.deepcopy(goal)
    patch_snap = copy.deepcopy(patch)

    new_goal, diff = apply_patch(goal, patch)

    assert goal == goal_snap
    assert patch == patch_snap
    assert new_goal is not goal
    assert new_goal["repo"] is not goal["repo"]
    assert new_goal["repo"] is not patch["repo"]
    assert diff["changed"]["repo"] is not patch["repo"]

    new_goal["repo"]["target_branch"] = "touched"
    new_goal["warn"]["turns"] = -1
    diff["changed"]["repo"]["target_branch"] = "touched-too"
    diff["added"]["warn"]["turns"] = -1

    assert goal == goal_snap
    assert patch == patch_snap


def test_apply_patch_nested_dict_whole_value_replacement() -> None:
    goal = {"warn": {"turns": 30, "dd_rounds": 6}, "title": "t"}
    new_goal, diff = apply_patch(goal, {"warn": {"dd_rounds": 6}})
    # 整值替换：不做递归 merge，turns 不保留
    assert new_goal["warn"] == {"dd_rounds": 6}
    assert diff["changed"] == {"warn": {"dd_rounds": 6}}
    assert diff["added"] == {}


def test_apply_patch_repo_replacement_allowed_without_path() -> None:
    new_goal, diff = apply_patch(dict(ENROLL), {"repo": {"target_branch": "dev"}})
    assert new_goal["repo"] == {"target_branch": "dev"}
    assert diff["changed"] == {"repo": {"target_branch": "dev"}}


# --- steered_payload ---------------------------------------------------------


def test_steered_payload_shape_and_key_order() -> None:
    diff = {"changed": {"title": "t2"}, "added": {}}
    payload = steered_payload(2, diff, "操作者附言")
    assert payload == {"version": 2, "diff": diff, "note": "操作者附言"}
    assert list(payload) == ["version", "diff", "note"]
    assert steered_payload(3, diff, None)["note"] is None


# --- steer_diff_since --------------------------------------------------------


def test_steer_diff_since_filters_by_seq_and_keeps_order() -> None:
    e2 = steered_event(2, 2, changed={"title": "旧"}, note="early")
    e5 = steered_event(5, 3, changed={"title": "t2"}, note="一")
    e9 = steered_event(9, 4, added={"deadline": "d"})
    events = [e2, other_event(3), e9, other_event(7), e5]  # 故意乱序

    entries = steer_diff_since(events, 2)
    assert [entry["version"] for entry in entries] == [3, 4]
    assert [entry["ts"] for entry in entries] == [e5.ts, e9.ts]

    first = entries[0]
    assert list(first) == ["version", "ts", "changed", "added", "note"]
    assert first["version"] == 3
    assert first["changed"] == {"title": "t2"}
    assert first["added"] == {}
    assert first["note"] == "一"
    assert entries[1]["added"] == {"deadline": "d"}
    assert entries[1]["note"] is None

    assert [entry["version"] for entry in steer_diff_since(events, 0)] == [2, 3, 4]
    assert steer_diff_since(events, 9) == []
    assert steer_diff_since([], 0) == []


def test_steer_diff_since_tolerates_sparse_payload() -> None:
    ev = Event(
        ts="t",
        goal_id="g-001",
        dd_id=None,
        kind="goal.steered",
        seq=4,
        payload={"version": 2},
    )
    (entry,) = steer_diff_since([ev], 0)
    assert entry["version"] == 2
    assert entry["changed"] == {}
    assert entry["added"] == {}
    assert entry["note"] is None


# --- current_goal ------------------------------------------------------------


def test_current_goal_no_steers_returns_enroll_copy_version_1() -> None:
    enroll = copy.deepcopy(ENROLL)
    goal, version = current_goal(enroll, [other_event(1), other_event(2)])
    assert version == 1
    assert goal == enroll
    assert goal is not enroll
    assert goal["repo"] is not enroll["repo"]
    enroll["title"] = "mutated-after"
    assert goal["title"] == "把 X 功能做出来"


def test_current_goal_replays_steers_version_and_last_write_wins() -> None:
    events = [
        Event(ts="t0", goal_id="g-001", dd_id=None, kind="goal.enrolled", seq=1, payload={}),
        other_event(2),
        steered_event(3, 2, changed={"title": "v2 标题"}),
        steered_event(4, 3, added={"deadline": "2026-10-01"}),
        other_event(5),
        steered_event(6, 4, changed={"title": "v4 标题", "acceptance": ["make test", "make e2e"]}),
    ]
    goal, version = current_goal(ENROLL, events)
    assert version == 4  # enroll=1 + 3 条 steered
    assert goal["title"] == "v4 标题"  # 同 key 后写覆盖先写
    assert goal["deadline"] == "2026-10-01"
    assert goal["acceptance"] == ["make test", "make e2e"]
    assert goal["schema"] == "goal.enroll/1"
    # 与 events.fold 的 goal_version 派生一致
    assert fold(events).goal_version == version


def test_current_goal_does_not_mutate_enroll() -> None:
    enroll = copy.deepcopy(ENROLL)
    snap = copy.deepcopy(enroll)
    goal, _ = current_goal(enroll, [steered_event(2, 2, changed={"title": "x"})])
    assert enroll == snap
    assert goal["title"] == "x"


def test_current_goal_rejects_corrupt_history() -> None:
    bad = Event(
        ts="t",
        goal_id="g-001",
        dd_id=None,
        kind="goal.steered",
        seq=2,
        payload={
            "version": 2,
            "diff": {"changed": {"goal_id": "g-999"}, "added": {}},
            "note": None,
        },
    )
    with pytest.raises(ValueError, match="must not touch"):
        current_goal(dict(ENROLL), [bad])


# --- 端到端：真实 event 日志 round-trip ---------------------------------------


def test_roundtrip_through_event_log(tmp_path: Path) -> None:
    elog = EventLog(tmp_path / "goals" / "g-001")
    elog.append("goal.enrolled", {"title": "把 X 功能做出来"})

    goal1, diff1 = apply_patch(ENROLL, {"title": "新标题", "deadline": "d1"})
    elog.append("goal.steered", steered_payload(2, diff1, "改标题"))
    goal2, diff2 = apply_patch(goal1, {"acceptance": ["make test", "make e2e"]})
    elog.append("goal.steered", steered_payload(3, diff2, None))

    events = list(elog.read())
    goal, version = current_goal(ENROLL, events)
    assert version == 3
    assert goal == goal2

    entries = steer_diff_since(events, 0)
    assert [entry["version"] for entry in entries] == [2, 3]
    assert entries[0]["note"] == "改标题"
    assert entries[0]["changed"] == {"title": "新标题"}
    assert entries[0]["added"] == {"deadline": "d1"}
    assert entries[1]["note"] is None
    assert entries[1]["changed"] == {"acceptance": ["make test", "make e2e"]}
    assert fold(events).goal_version == version
