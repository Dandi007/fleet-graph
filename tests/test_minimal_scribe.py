"""Tests for fleet_graph.minimal.scribe: observation log, §12 evidence gate, new_runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet_graph.minimal.events import EventLog
from fleet_graph.minimal.scribe import (
    DROP_DETAIL,
    SCRIBE_TRIGGERS,
    ObservationLog,
    findings_subset,
    new_runs_from_events,
    partition_observations,
    validate_observation,
)


def make_log(tmp_path: Path, goal_id: str = "g-001") -> ObservationLog:
    return ObservationLog(tmp_path / "goals" / goal_id)


def obs(**overrides: object) -> dict:
    base: dict = {
        "kind": "progress",
        "severity": "info",
        "title": "标题",
        "summary": "两三句理解",
        "evidence": [{"event_seq": 5}],
        "tags": ["impl"],
    }
    base.update(overrides)
    return base


def test_scribe_triggers_are_goal_level_boundaries() -> None:
    assert SCRIBE_TRIGGERS == (
        "goal.turn.finished",
        "dd.merged",
        "dd.failed",
        "goal.done",
        "goal.blocked",
        "goal.warning",
    )


# ---------------------------------------------------------------------------
# ObservationLog
# ---------------------------------------------------------------------------


def test_append_read_roundtrip_stamps_ts_trigger_seq_range(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    r1 = log.append(obs(), trigger="dd.merged", seq_range=[380, 418])
    r2 = log.append(obs(kind="anomaly", severity="high"), trigger="goal.warning", seq_range=(1, 2))

    assert r1["trigger"] == "dd.merged"
    assert r1["seq_range"] == [380, 418]
    assert r1["ts"]
    assert r2["seq_range"] == [1, 2]
    assert r2["kind"] == "anomaly"

    records = list(log.read())
    assert len(records) == 2
    assert records[0]["kind"] == "progress"
    assert records[0]["title"] == "标题"
    assert records[0]["trigger"] == "dd.merged"
    assert records[0]["seq_range"] == [380, 418]
    assert records[1]["severity"] == "high"


def test_append_uses_given_ts_verbatim(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    stamped = "2026-09-07T00:00:00+00:00"
    record = log.append(obs(), trigger="goal.done", seq_range=[1, 9], ts=stamped)
    assert record["ts"] == stamped
    assert next(iter(log.read()))["ts"] == stamped


def test_append_writes_one_json_line_each_ensure_ascii_false(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append(obs(), trigger="dd.merged", seq_range=[1, 2])
    log.append(obs(), trigger="dd.failed", seq_range=[3, 4])
    raw = log.path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == 2
    assert raw.endswith("\n")
    assert "标题" in raw  # ensure_ascii=False: non-ASCII stays literal, no \u escape
    assert "\\u" not in raw


def test_append_rejects_non_dict_obs_and_bad_seq_range(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    with pytest.raises(ValueError):
        log.append(["not", "an", "object"], trigger="dd.merged", seq_range=[1, 2])  # type: ignore[arg-type]
    for bad in ((1,), (1, 2, 3), (1, "x"), "12", None):
        with pytest.raises(ValueError):
            log.append(obs(), trigger="dd.merged", seq_range=bad)


def test_append_overwrites_agent_supplied_bookkeeping_keys(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    forged = obs(ts="1970-01-01T00:00:00+00:00", trigger="goal.done", seq_range=[0, 0])
    stamped = "2026-09-07T01:00:00+00:00"
    record = log.append(forged, trigger="dd.merged", seq_range=[10, 20], ts=stamped)
    assert record["trigger"] == "dd.merged"
    assert record["seq_range"] == [10, 20]
    assert record["ts"] == stamped


def test_read_tolerates_truncated_last_line(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append(obs(), trigger="dd.merged", seq_range=[1, 2])
    with log.path.open("a", encoding="utf-8") as f:
        f.write('{"kind": "progress", "ti')

    records = list(log.read())
    assert len(records) == 1
    assert records[0]["kind"] == "progress"


def test_read_raises_on_middle_corrupt_line(tmp_path: Path) -> None:
    d = tmp_path / "goals" / "g-001"
    d.mkdir(parents=True)
    (d / "observations.jsonl").write_text(
        '{"kind":"progress","ts":"t1"}\n{"half-written": \n{"kind":"anomaly","ts":"t3"}\n',
        encoding="utf-8",
    )
    log = ObservationLog(d)
    with pytest.raises(ValueError):
        list(log.read())


def test_read_missing_file_yields_nothing(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    assert list(log.read()) == []


# ---------------------------------------------------------------------------
# validate_observation (§12 evidence gate)
# ---------------------------------------------------------------------------


def test_validate_ok_at_range_boundaries() -> None:
    assert (
        validate_observation(
            obs(evidence=[{"event_seq": 3}]),
            since_seq=3,
            until_seq=7,
            session_exists=lambda _p: False,
        )
        == []
    )
    assert (
        validate_observation(
            obs(evidence=[{"event_seq": 7}]),
            since_seq=3,
            until_seq=7,
            session_exists=lambda _p: False,
        )
        == []
    )


def test_validate_rejects_empty_or_missing_evidence() -> None:
    missing_key = {key: value for key, value in obs().items() if key != "evidence"}
    for bad in (missing_key, obs(evidence=[]), obs(evidence="x")):
        errors = validate_observation(bad, since_seq=1, until_seq=9, session_exists=lambda _p: True)
        assert len(errors) == 1
        assert "evidence" in errors[0]


def test_validate_rejects_out_of_range_event_seq() -> None:
    for seq in (2, 8):
        errors = validate_observation(
            obs(evidence=[{"event_seq": seq}]),
            since_seq=3,
            until_seq=7,
            session_exists=lambda _p: True,
        )
        assert errors == [f"evidence[0].event_seq={seq} outside [3, 7]"]


def test_validate_rejects_non_integer_event_seq() -> None:
    errors = validate_observation(
        obs(evidence=[{"event_seq": "5"}]), since_seq=1, until_seq=9, session_exists=lambda _p: True
    )
    assert errors == ["evidence[0].event_seq must be an integer"]


def test_validate_session_pointer_uses_injected_probe(tmp_path: Path) -> None:
    existing = str(tmp_path / "sessions" / "run-1")
    errors = validate_observation(
        obs(evidence=[{"session": existing, "line": 118}]),
        since_seq=1,
        until_seq=9,
        session_exists=lambda p: p == existing,
    )
    assert errors == []

    errors = validate_observation(
        obs(evidence=[{"session": existing, "line": 118}]),
        since_seq=1,
        until_seq=9,
        session_exists=lambda _p: False,
    )
    assert errors == [f"evidence[0].session does not exist: {existing!r}"]

    errors = validate_observation(
        obs(evidence=[{"session": ""}]), since_seq=1, until_seq=9, session_exists=lambda _p: True
    )
    assert errors == ["evidence[0].session must be a non-empty string"]


def test_validate_stop_of_pointer() -> None:
    assert (
        validate_observation(
            obs(evidence=[{"stop_of": "run-1"}]),
            since_seq=1,
            until_seq=9,
            session_exists=lambda _p: True,
        )
        == []
    )
    errors = validate_observation(
        obs(evidence=[{"stop_of": ""}]), since_seq=1, until_seq=9, session_exists=lambda _p: True
    )
    assert errors == ["evidence[0].stop_of must be a non-empty string"]


def test_validate_evidence_entry_must_be_exactly_one_pointer() -> None:
    for entry in (
        {"event_seq": 5, "session": "/x"},
        {"session": "/x", "stop_of": "run-1"},
        {"line": 118},
        "not-an-object",
    ):
        errors = validate_observation(
            obs(evidence=[entry]), since_seq=1, until_seq=9, session_exists=lambda _p: True
        )
        assert len(errors) == 1
        assert errors[0].startswith("evidence[0]")


def test_validate_kind_and_severity_enums() -> None:
    errors = validate_observation(
        obs(kind="bogus", severity="critical"),
        since_seq=1,
        until_seq=9,
        session_exists=lambda _p: True,
    )
    assert errors == [
        "kind='bogus' not in {progress, anomaly, cost, quality, decision, pattern}",
        "severity='critical' not in {info, warn, high}",
    ]


# ---------------------------------------------------------------------------
# partition_observations
# ---------------------------------------------------------------------------


def test_partition_empty_observations_is_legal() -> None:
    assert partition_observations([], since_seq=1, until_seq=9, session_exists=lambda _p: True) == (
        [],
        [],
    )


def test_partition_keeps_valid_drops_invalid_with_reasons() -> None:
    good = obs(evidence=[{"event_seq": 5}, {"stop_of": "run-1"}])
    no_evidence = obs(evidence=[])
    out_of_range = obs(severity="warn", evidence=[{"event_seq": 99}])

    kept, dropped = partition_observations(
        [good, no_evidence, out_of_range], since_seq=1, until_seq=9, session_exists=lambda _p: True
    )

    assert kept == [good]
    assert len(dropped) == 2
    assert dropped[0]["detail"] == DROP_DETAIL
    assert dropped[0]["detail"] == "observation_without_evidence"
    assert dropped[0]["observation"] is no_evidence
    assert dropped[0]["errors"] == ["evidence must be a non-empty list"]
    assert dropped[1]["observation"] is out_of_range
    assert dropped[1]["errors"] == ["evidence[0].event_seq=99 outside [1, 9]"]


def test_partition_drops_non_dict_entry_without_crashing() -> None:
    kept, dropped = partition_observations(
        ["oops"], since_seq=1, until_seq=9, session_exists=lambda _p: True
    )
    assert kept == []
    assert dropped == [
        {"detail": DROP_DETAIL, "errors": ["observation must be an object"], "observation": "oops"}
    ]


# ---------------------------------------------------------------------------
# findings_subset
# ---------------------------------------------------------------------------


def test_findings_subset_keeps_only_warn_and_high_in_order() -> None:
    kept = [
        obs(severity="info", title="a", summary="sa"),
        obs(severity="warn", title="b", summary="sb"),
        obs(severity="high", title="c", summary="sc"),
        obs(severity="info", title="d", summary="sd"),
    ]
    assert findings_subset(kept) == ["[warn] b: sb", "[high] c: sc"]
    assert findings_subset([]) == []


# ---------------------------------------------------------------------------
# new_runs_from_events
# ---------------------------------------------------------------------------


def _events(tmp_path: Path) -> EventLog:
    log = EventLog(tmp_path / "goals" / "g-001")
    log.append("goal.enrolled", {})  # 1: outside the window below
    log.append("agent.spawned", {"role": "impl", "run_id": "run-0"})  # 2: outside (before since)
    log.append("agent.spawned", {"role": "impl", "run_id": "run-1"}, dd_id="dd-01")  # 3
    log.append("agent.exited", {"run_id": "run-1", "exit_code": 0, "usage": {}}, dd_id="dd-01")  # 4
    log.append(
        "dd.stage.finished",
        {"stage": "impl", "stop": "committed", "run_id": "run-1", "commit": "abc", "usage": {}},
        dd_id="dd-01",
    )  # 5
    log.append("agent.spawned", {"role": "cr", "run_id": "run-2"})  # 6: crashed, never finished
    log.append(
        "dd.stage.finished", {"stage": "fr", "stop": "pass", "run_id": "run-9"}
    )  # 7: run not in window
    log.append("goal.warning", {"message": "w"})  # 8
    log.append("agent.spawned", {"role": "fr", "run_id": "run-3"})  # 9: outside (after until)
    return log


def test_new_runs_from_events_builds_scribe_in_new_runs(tmp_path: Path) -> None:
    log = _events(tmp_path)
    runs = new_runs_from_events(log.read(), 3, 8, "/data/fleet/goals/g-001/sessions")
    assert runs == [
        {
            "run_id": "run-1",
            "role": "impl",
            "session_dir": "/data/fleet/goals/g-001/sessions/run-1/",
            "stop": {
                "stage": "impl",
                "stop": "committed",
                "run_id": "run-1",
                "commit": "abc",
                "usage": {},
            },
        },
        {
            "run_id": "run-2",
            "role": "cr",
            "session_dir": "/data/fleet/goals/g-001/sessions/run-2/",
            "stop": None,
        },
    ]


def test_new_runs_ignores_finished_for_unregistered_runs(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "goals" / "g-001")
    log.append("goal.enrolled", {})
    log.append("dd.stage.finished", {"stage": "fr", "stop": "pass", "run_id": "ghost"})
    assert new_runs_from_events(log.read(), 1, 2, "/s") == []


def test_new_runs_agent_exited_fills_missing_role(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "goals" / "g-001")
    log.append("agent.spawned", {"run_id": "run-1"})
    log.append("agent.exited", {"run_id": "run-1", "role": "merge", "exit_code": 1})
    runs = new_runs_from_events(log.read(), 1, 2, "/s")
    assert runs == [{"run_id": "run-1", "role": "merge", "session_dir": "/s/run-1/", "stop": None}]


def test_new_runs_goal_turn_finished_attaches_stop_by_run_id(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "goals" / "g-001")
    log.append("agent.spawned", {"role": "goal", "run_id": "run-1"})
    output = {
        "schema": "goal.turn/1",
        "stop": "dispatch",
        "summary": "派下一张",
        "run_id": "run-1",
    }
    log.append("goal.turn.finished", output)
    runs = new_runs_from_events(log.read(), 1, 2, "/s")
    assert runs[0]["stop"] == output


def test_new_runs_pathlike_sessions_dir(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "goals" / "g-001")
    log.append("agent.spawned", {"role": "scribe", "run_id": "run-1"})
    runs = new_runs_from_events(log.read(), 1, 1, tmp_path / "sessions")
    assert runs[0]["session_dir"].endswith("/sessions/run-1/")
