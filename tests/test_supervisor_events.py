"""The supervisor event observer: four scans, two budgets, zero second loops.

The retreat-verified trio the R4-2 ticket names:

- E2 wiring: a blocked+waiting_on=decision terminal becomes exactly one
  `supervisor run` launch with the right event JSON;
- budgets: at most two launches per tick, at most three lifetime attempts per
  event key, and neither is consumed by an audit already in flight;
- fail-open: a broken scan or a dead bus costs observation, never the tick.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig, TickResult
from fleet_graph.scheduler.ignition import IgnitionDecision, Refusal
from fleet_graph.scheduler.supervisor_events import (
    DECISION_TOKEN_ENV,
    ObserverConfig,
    SupervisorLaunchSpec,
    SupervisorObserver,
    observer_environment,
    reset_supervisor_event,
)
from fleet_graph.supervise.events import SupervisorEventError, line_fault_event, validate_event


class RecordingLauncher:
    """Stands in for TransientLauncher; records specs, reports success."""

    def __init__(self, *, start: bool = True) -> None:
        self.specs: list[Any] = []
        self.start = start

    def launch(self, spec: Any):
        self.specs.append(spec)

        class _Result:
            unit_name = spec.unit_name
            started = self.start
            detail = "recorded"

        return _Result()

    def events(self) -> list[dict[str, Any]]:
        parsed = []
        for spec in self.specs:
            argv = spec.argv()
            parsed.append(json.loads(argv[argv.index("--event-json") + 1]))
        return parsed


class StaticUnits:
    def __init__(self, active: bool = False) -> None:
        self.active = active

    def is_active(self, unit_name: str) -> bool:
        return self.active


class FakeBus:
    """messages/refs_to as the observer and Board use them."""

    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []
        self.decisions: dict[str, str] = {}  # question note id -> decision msg id

    def add_question(self, message_id: str, card: str, seq: int) -> None:
        self.notes.append(
            {
                "message_id": message_id,
                "channel_seq": seq,
                "kind": "work.note.v1",
                "payload": {"note_type": "question", "card_entity_id": card, "note": "?"},
            }
        )

    def add_decision_for(self, question_id: str, decision_id: str, seq: int) -> None:
        self.decisions[question_id] = decision_id
        self.notes.append(
            {
                "message_id": decision_id,
                "channel_seq": seq,
                "kind": "work.decision.v1",
                "payload": {"decision": "APPROVE"},
            }
        )

    def messages(self, channel: str, *, limit: int = 100, after_seq: int = 0):
        selected = [n for n in self.notes if n["channel_seq"] > after_seq][:limit]
        head = max((n["channel_seq"] for n in self.notes), default=0)
        return selected, head

    def message(self, channel: str, message_id: str) -> dict[str, Any] | None:
        for note in self.notes:
            if note["message_id"] == message_id:
                return note
        return None

    def refs_to(self, entity_id: str) -> list[dict[str, Any]]:
        if entity_id in self.decisions:
            return [{"message_id": self.decisions[entity_id]}]
        return []


EMPTY_READ_MODEL: dict[str, dict[str, Any]] = {
    "/v1/lines": {"schema_version": "1", "lines": []},
    "/v1/decisions": {"schema_version": "1", "decisions": []},
    "/v1/harvestable": {"schema_version": "1", "developments": []},
    "/v1/enrollments": {"schema_version": "1", "enrollments": []},
}


def read_model_for(snapshots: dict[str, Any]) -> Callable[[str], dict[str, Any] | None]:
    """A fake :7494 snapshot fetcher: view path -> parsed JSON body (or None).

    The observer must never touch the *live* read-model in tests (交付 D:
    合成 read-model 快照,注入假 :7494 客户端/快照函数), so every observer in
    this file is wired to one of these.
    """

    def fetch(path: str) -> dict[str, Any] | None:
        return snapshots.get(path)

    return fetch


def observer_for(
    tmp_path: Path,
    *,
    bus: Any = None,
    units: Any = None,
    max_per_tick: int = 2,
    max_attempts: int = 3,
    read_model: Callable[[str], dict[str, Any] | None] | None = None,
) -> tuple[SupervisorObserver, RecordingLauncher]:
    launcher = RecordingLauncher()
    observer = SupervisorObserver(
        ObserverConfig(
            run_root=tmp_path / "runs",
            supervisor_state_root=tmp_path / "supervisor",
            max_launches_per_tick=max_per_tick,
            max_attempts_per_key=max_attempts,
            cap_window_seconds=3600.0,
        ),
        launcher=launcher,  # type: ignore[arg-type]
        bus=bus,
        units=units,
        read_model=read_model or read_model_for(EMPTY_READ_MODEL),
    )
    return observer, launcher


def terminal(
    terminal_value: str, run_id: str, *, waiting_on: str | None = None, pump_fault: bool = False
) -> dict[str, Any]:
    return {
        "terminal": terminal_value,
        "rounds": 0,
        "run_id": run_id,
        "waiting_on": waiting_on,
        "pump_fault": pump_fault,
        "at": "2026-08-27T10:00:00Z",
    }


def tick(observer: SupervisorObserver, folders: dict[str, Any], results: list[Any] | None = None):
    return observer.after_tick(
        now=1_000_000.0,
        folder_ids=list(folders),
        terminal_reader=lambda folder: folders[folder],
        tick_results=results or [],
    )


class TestTerminalEvents:
    def test_e2_blocked_decision_launches_supervisor_run(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path)
        actions = tick(observer, {"wf-a": terminal("blocked", "run-1", waiting_on="decision")})
        [event] = launcher.events()
        assert event["type"] == "blocked_decision"
        assert event["key"] == "e2-run-1"
        assert event["payload"] == {"folder_id": "wf-a", "run_id": "run-1"}
        assert any(a.get("action") == "launched" for a in actions)

    def test_e2_ignores_blocked_waiting_on_external(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path)
        tick(observer, {"wf-a": terminal("blocked", "run-1", waiting_on="external")})
        assert launcher.events() == []

    def test_e3_fault_launches_line_fault_event(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path)
        tick(observer, {"wf-a": terminal("fault", "run-9")})
        [event] = launcher.events()
        assert event["type"] == "line_fault"
        assert event["key"] == "e3-run-9"

    def test_e3_pump_fault_alone_is_enough(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path)
        tick(observer, {"wf-a": terminal("blocked", "run-9", pump_fault=True)})
        [event] = launcher.events()
        assert event["type"] == "line_fault"

    def test_done_and_running_lines_emit_nothing(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path)
        tick(observer, {"wf-a": terminal("done", "run-1"), "wf-b": None})
        assert launcher.events() == []


class TestCapEvents:
    def _capped(self, folder: str) -> TickResult:
        return TickResult(
            folder,
            IgnitionDecision(ignite=False, refusal=Refusal.TOTAL_CAP_REACHED, detail="cap"),
        )

    def test_e4_cap_refusal_launches_one_bucketed_event(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path)
        tick(observer, {}, results=[self._capped("wf-a"), self._capped("wf-b")])
        [event] = launcher.events()
        assert event["type"] == "cap_breaker"
        assert event["key"] == "e4-cap-277"  # 1_000_000 // 3600
        assert event["payload"]["folder_ids"] == ["wf-a", "wf-b"]

    def test_other_refusals_do_not_trip_e4(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path)
        result = TickResult(
            "wf-a", IgnitionDecision(ignite=False, refusal=Refusal.COOLING_DOWN, detail="")
        )
        tick(observer, {}, results=[result])
        assert launcher.events() == []


class TestReadModelEvents:
    """M2 E5/E6/E7: derived from the synthetic read-model snapshots (:7494),
    never from heartbeat/terminal/bus/bridge files (spec 交付 C + D)."""

    def test_e5_harvestable_development_fires(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/harvestable": {
                        "schema_version": "1",
                        "developments": [
                            {
                                "development_id": "dev-x",
                                "head_commit": "abc123",
                                "stage": "implement",
                                "terminal": "done",
                            }
                        ],
                    }
                }
            ),
        )
        tick(observer, {})
        [event] = launcher.events()
        assert event["type"] == "approved_unharvested"
        assert event["key"] == "e5-dev-x"
        assert event["payload"] == {
            "development_id": "dev-x",
            "head_commit": "abc123",
            "stage": "implement",
        }

    def test_e6_stale_heartbeat_line_fires(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/lines": {
                        "schema_version": "1",
                        "lines": [
                            {
                                "folder_id": "wf-a",
                                "heartbeat_age_s": 600.0,
                                "round": 3,
                                "phase": "coordinator",
                                "terminal": None,
                                "parked": False,
                            }
                        ],
                    }
                }
            ),
        )
        tick(observer, {})
        [event] = launcher.events()
        assert event["type"] == "heartbeat_stale"
        assert event["key"] == "e6-wf-a"
        assert event["payload"] == {
            "folder_id": "wf-a",
            "heartbeat_age_s": 600.0,
            "round": 3,
            "phase": "coordinator",
        }

    def test_e6_skips_fresh_terminal_parked_and_unknown_lines(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/lines": {
                        "schema_version": "1",
                        "lines": [
                            # Fresh heartbeat: age below the 300s threshold.
                            {"folder_id": "wf-fresh", "heartbeat_age_s": 5.0, "terminal": None},
                            # Terminal-ed: not a stalled line.
                            {"folder_id": "wf-term", "heartbeat_age_s": 600.0, "terminal": "done"},
                            # Parked: waiting on a decision, not stalled.
                            {
                                "folder_id": "wf-parked",
                                "heartbeat_age_s": 600.0,
                                "terminal": None,
                                "parked": True,
                            },
                            # No heartbeat at all.
                            {"folder_id": "wf-nohb", "heartbeat_age_s": None, "terminal": None},
                        ],
                    }
                }
            ),
        )
        tick(observer, {})
        assert launcher.events() == []

    def test_e7_swallowed_decision_fires(self, tmp_path: Path) -> None:
        """Reworked to watermark semantics (spec 交付 C.4): first tick adopts
        the current snapshot as baseline (zero E7 emission), a new swallowed
        decision on the second tick is the only thing emitted."""
        snapshot = {
            "/v1/decisions": {
                "schema_version": "1",
                "decisions": [
                    {"source_message_id": "msg_sw", "state": "swallowed", "reason": "noop"},
                    {"source_message_id": "msg_pub", "state": "published", "reason": ""},
                ],
            }
        }
        observer, launcher = observer_for(tmp_path, read_model=read_model_for(snapshot))
        actions = tick(observer, {})  # baseline adoption
        assert launcher.events() == []
        assert any("cursor_adopted:e7_baseline" in a.get("action", "") for a in actions)
        snapshot["/v1/decisions"]["decisions"].append(
            {"source_message_id": "msg_new", "state": "swallowed", "reason": "noop"}
        )
        tick(observer, {})
        [event] = launcher.events()
        assert event["type"] == "decision_swallowed"
        assert event["key"] == "e7-msg_new"
        assert event["payload"] == {"source_message_id": "msg_new", "reason": "noop"}

    def test_e7_first_run_adopts_baseline_and_emits_nothing(self, tmp_path: Path) -> None:
        """交付 C.1: 首轮零发射——快照含历史 swallowed → 第一次 tick
        launcher.events()==[]，cursor 落盘 e7_baseline 含该 source_message_id，
        动作注记含 cursor_adopted:e7_baseline。"""
        observer, launcher = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/decisions": {
                        "schema_version": "1",
                        "decisions": [
                            {
                                "source_message_id": "msg_01M13x",
                                "state": "swallowed",
                                "reason": "noop",
                            },
                            {
                                "source_message_id": "msg_01M14x",
                                "state": "swallowed",
                                "reason": "noop",
                            },
                        ],
                    }
                }
            ),
        )
        actions = tick(observer, {})
        assert launcher.events() == []
        state = json.loads(
            (tmp_path / "runs" / ".scheduler" / "supervisor-cursor.json").read_text()
        )
        assert state["e7_baseline"] == ["msg_01M13x", "msg_01M14x"]
        assert any("cursor_adopted:e7_baseline=n=2" in a.get("action", "") for a in actions)

    def test_e7_new_swallowed_fires_after_baseline(self, tmp_path: Path) -> None:
        """交付 C.2: 第二 tick 快照多一条新增 swallowed（msg_new）→ 仅发射
        e7-msg_new（type/key/payload 精确），历史 id 不重发；后续多 tick 重复
        扫描不重复发射（水位推进）。"""
        snapshot = {
            "/v1/decisions": {
                "schema_version": "1",
                "decisions": [
                    {"source_message_id": "msg_01M13x", "state": "swallowed", "reason": "noop"},
                ],
            }
        }
        observer, launcher = observer_for(tmp_path, read_model=read_model_for(snapshot))
        tick(observer, {})  # baseline adoption
        assert launcher.events() == []
        snapshot["/v1/decisions"]["decisions"].append(
            {"source_message_id": "msg_new", "state": "swallowed", "reason": "blocked"}
        )
        tick(observer, {})
        [event] = launcher.events()
        assert event["type"] == "decision_swallowed"
        assert event["key"] == "e7-msg_new"
        assert event["payload"] == {"source_message_id": "msg_new", "reason": "blocked"}
        # Repeated rescans of the same snapshot must not re-emit.
        tick(observer, {})
        tick(observer, {})
        assert len(launcher.events()) == 1

    def test_e7_baseline_survives_restart(self, tmp_path: Path) -> None:
        """交付 C.3: restart 后基线仍生效——新 observer 对象 + 同 state root /
        cursor → 历史 id 不重发（水位持久化）。"""
        snapshot = {
            "/v1/decisions": {
                "schema_version": "1",
                "decisions": [
                    {"source_message_id": "msg_01M13x", "state": "swallowed", "reason": "noop"},
                ],
            }
        }
        observer, launcher = observer_for(tmp_path, read_model=read_model_for(snapshot))
        tick(observer, {})  # adopt baseline
        assert launcher.events() == []
        observer2, launcher2 = observer_for(tmp_path, read_model=read_model_for(snapshot))
        tick(observer2, {})
        assert launcher2.events() == []

    def test_e7_deferred_by_budget_does_not_advance_watermark(self, tmp_path: Path) -> None:
        """交付 A.4: 被 deferred:tick_budget 未真正处置的新 id 不得推进水位——
        下一 tick 重扫重派；处置后才推进。"""
        snapshot = {
            "/v1/decisions": {
                "schema_version": "1",
                "decisions": [
                    {"source_message_id": "msg_01M13x", "state": "swallowed", "reason": "noop"},
                ],
            }
        }
        observer, launcher = observer_for(
            tmp_path, max_per_tick=0, read_model=read_model_for(snapshot)
        )
        tick(observer, {})  # baseline adoption
        snapshot["/v1/decisions"]["decisions"].append(
            {"source_message_id": "msg_new", "state": "swallowed", "reason": "noop"}
        )
        actions = tick(observer, {})  # budget 0: msg_new deferred
        assert launcher.events() == []
        assert any("deferred:tick_budget" in a.get("action", "") for a in actions)
        cursor = tmp_path / "runs" / ".scheduler" / "supervisor-cursor.json"
        state = json.loads(cursor.read_text())
        assert "msg_new" not in state["e7_baseline"]
        # Budget available again: re-dispatched and the watermark advances.
        observer.config.max_launches_per_tick = 2
        tick(observer, {})
        assert len(launcher.events()) == 1
        assert "msg_new" in json.loads(cursor.read_text())["e7_baseline"]

    def test_no_progress_actions_aggregate_and_dedup_across_ticks(self, tmp_path: Path) -> None:
        """交付 B (P3): 同 tick 内同类「无进展」action 聚合为一条计数
        （skipped:receipt_exists:x2），且与上一 tick 完全相同的批不重复打印。
        日志面去噪，返回的完整 actions 不受影响。"""
        reports = tmp_path / "supervisor" / "reports"
        reports.mkdir(parents=True)
        (reports / "e5-dev-x.json").write_text("{}")
        (reports / "e5-dev-y.json").write_text("{}")
        observer, _launcher = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/harvestable": {
                        "schema_version": "1",
                        "developments": [
                            {"development_id": "dev-x", "head_commit": "a", "stage": "s"},
                            {"development_id": "dev-y", "head_commit": "b", "stage": "s"},
                        ],
                    }
                }
            ),
        )
        seen: list[dict[str, Any]] = []
        observer.observe = lambda record: seen.append(record)
        full_actions = tick(observer, {})
        # Returned actions stay complete (both receipt_exists rows present).
        assert sum(1 for a in full_actions if a.get("action") == "skipped:receipt_exists") == 2
        emitted = [r["supervisor_observer"] for r in seen]
        counted = [a for a in emitted if "receipt_exists" in a.get("action", "")]
        assert len(counted) == 1
        assert counted[0]["action"] == "skipped:receipt_exists:x2"
        # Identical batch next tick: not reprinted.
        seen.clear()
        tick(observer, {})
        assert not any("receipt_exists" in r["supervisor_observer"].get("action", "") for r in seen)

    def test_same_key_across_ticks_is_a_fresh_attempt_not_a_duplicate(self, tmp_path: Path) -> None:
        """The same snapshot re-scanned next tick re-derives the same key; the
        observer turns that into a *new attempt generation* (a2, a3 -- fresh
        thread identity), never a silent duplicate within a tick. A receipt --
        not an attempt -- ends the event for good (next test)."""
        observer, launcher = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/harvestable": {
                        "schema_version": "1",
                        "developments": [
                            {
                                "development_id": "dev-x",
                                "head_commit": "abc123",
                                "stage": "implement",
                                "terminal": "done",
                            }
                        ],
                    }
                }
            ),
        )
        tick(observer, {})
        tick(observer, {})
        events = launcher.events()
        assert len(events) == 2
        assert events[0]["key"] == events[1]["key"] == "e5-dev-x"
        assert [e["attempt"] for e in events] == [1, 2]
        threads = [validate_event(e).thread_id for e in events]
        assert threads == ["supervisor:e5-dev-x:a1", "supervisor:e5-dev-x:a2"]
        # Within one tick the same key is launched at most once: one snapshot,
        # one event object, one `_consider` decision.
        observer2, launcher2 = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/harvestable": {
                        "schema_version": "1",
                        "developments": [
                            {
                                "development_id": "dev-x",
                                "head_commit": "abc123",
                                "stage": "implement",
                                "terminal": "done",
                            }
                        ],
                    }
                }
            ),
        )
        tick(observer2, {})
        assert len(launcher2.events()) == 1

    def test_receipt_ends_a_read_model_event_for_good(self, tmp_path: Path) -> None:
        reports = tmp_path / "supervisor" / "reports"
        reports.mkdir(parents=True)
        (reports / "e6-wf-a.json").write_text("{}")
        observer, launcher = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/lines": {
                        "schema_version": "1",
                        "lines": [
                            {"folder_id": "wf-a", "heartbeat_age_s": 600.0, "terminal": None}
                        ],
                    }
                }
            ),
        )
        actions = tick(observer, {})
        assert launcher.events() == []
        assert any(a.get("action") == "skipped:receipt_exists" for a in actions)

    def test_read_model_events_share_the_tick_budget(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(
            tmp_path,
            max_per_tick=1,
            read_model=read_model_for(
                {
                    "/v1/harvestable": {
                        "schema_version": "1",
                        "developments": [
                            {"development_id": "dev-1", "head_commit": "a", "stage": "s"},
                            {"development_id": "dev-2", "head_commit": "b", "stage": "s"},
                        ],
                    }
                }
            ),
        )
        actions = tick(observer, {})
        assert len(launcher.events()) == 1
        assert any(a.get("action") == "deferred:tick_budget" for a in actions)

    def test_lifetime_attempts_cap_applies_to_read_model_events(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(
            tmp_path,
            max_attempts=2,
            read_model=read_model_for(
                {
                    "/v1/harvestable": {
                        "schema_version": "1",
                        "developments": [
                            {"development_id": "dev-x", "head_commit": "a", "stage": "s"}
                        ],
                    }
                }
            ),
        )
        for _ in range(4):
            tick(observer, {})
        assert len(launcher.events()) == 2
        actions = tick(observer, {})
        assert any("attempts_exhausted" in a.get("action", "") for a in actions)

    def test_unreachable_read_model_is_a_skip_not_a_crash(self, tmp_path: Path) -> None:
        """fail-open: a dead :7494 (all fetches None) costs observation, never
        the tick -- and no E5/E6/E7 event is minted from nothing."""
        observer, launcher = observer_for(
            tmp_path,
            read_model=lambda path: None,
        )
        actions = tick(observer, {})
        assert launcher.events() == []
        assert not any("error" in a for a in actions)

    def test_malformed_snapshot_entries_are_skipped(self, tmp_path: Path) -> None:
        """Bad artifacts degrade per entry: non-dict rows and non-numeric
        heartbeat ages never mint an event, and never 5xx the scan."""
        observer, launcher = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/lines": {
                        "schema_version": "1",
                        "lines": [
                            "not-a-dict",
                            {"folder_id": "wf-badage", "heartbeat_age_s": "oops"},
                            {"heartbeat_age_s": 600.0},  # no folder_id
                        ],
                    },
                    "/v1/decisions": {
                        "schema_version": "1",
                        "decisions": ["not-a-dict", {"state": "swallowed"}],
                    },
                    "/v1/harvestable": {
                        "schema_version": "1",
                        "developments": ["not-a-dict", {"head_commit": "abc"}],
                    },
                }
            ),
        )
        tick(observer, {})
        assert launcher.events() == []

    def test_validate_event_still_refuses_unknown_names(self) -> None:
        # 负例保留（spec 交付 A.3）：词表扩容不改 validate_event 的拒绝语义。
        with pytest.raises(SupervisorEventError, match="vocabulary is closed"):
            validate_event({"type": "harvest_ready", "key": "e9-x", "payload": {}})

    def test_e8_pending_enrollment_fires(self, tmp_path: Path) -> None:
        """E8: a pending enrollment in the /v1/enrollments snapshot emits
        `enrollment_pending` with the dedup key `enroll:{folder_id}`."""
        observer, launcher = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/enrollments": {
                        "schema_version": "1",
                        "enrollments": [
                            {
                                "folder_id": "wf-1",
                                "alias": "ronin-fresh",
                                "submitted_at": "2026-08-31T00:00:00Z",
                                "status": "pending",
                            }
                        ],
                    }
                }
            ),
        )
        tick(observer, {})
        [event] = launcher.events()
        assert event["type"] == "enrollment_pending"
        assert event["key"] == "enroll-wf-1"
        assert event["payload"]["folder_id"] == "wf-1"
        assert event["payload"]["reminder_generation"] is None

    def test_e8_decided_enrollments_do_not_fire(self, tmp_path: Path) -> None:
        """Only `pending` applications are facts worth an audit; decided and
        withdrawn ones are no longer open."""
        observer, launcher = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/enrollments": {
                        "schema_version": "1",
                        "enrollments": [
                            {"folder_id": "wf-a", "alias": "ronin-a", "status": "admitted"},
                            {"folder_id": "wf-b", "alias": "ronin-b", "status": "rejected"},
                            {"folder_id": "wf-c", "alias": "ronin-c", "status": "withdrawn"},
                        ],
                    }
                }
            ),
        )
        tick(observer, {})
        assert launcher.events() == []

    def test_e8_over_age_pending_appends_a_reminder_generation(self, tmp_path: Path) -> None:
        """pending 超龄（24h 未裁）追加提醒 attempt：submitted_at 早于阈值
        24h 的申请额外发射 `enroll:{folder_id}:g{n}` 提醒键，n 为完整超龄
        周期数——`{key}:g{n}` 语义沿用。"""
        observer, launcher = observer_for(
            tmp_path,
            read_model=read_model_for(
                {
                    "/v1/enrollments": {
                        "schema_version": "1",
                        "enrollments": [
                            # now=1_000_000.0, threshold=86400; epoch 0 is far
                            # older than 24h, so the reminder generation is >= 1
                            {
                                "folder_id": "wf-old",
                                "alias": "ronin-old",
                                "submitted_at": "1970-01-01T00:00:00Z",
                                "status": "pending",
                            }
                        ],
                    }
                }
            ),
        )
        tick(observer, {})
        keys = [e["key"] for e in launcher.events()]
        assert "enroll-wf-old" in keys
        reminders = [e for e in launcher.events() if e["payload"].get("reminder_generation")]
        assert reminders, keys
        assert reminders[0]["key"].startswith("enroll-wf-old-g")

    def test_e8_same_key_across_ticks_is_a_fresh_attempt_not_a_duplicate(
        self, tmp_path: Path
    ) -> None:
        """E8 follows the E5-E7 dedup discipline: the same pending application
        re-scanned next tick is a fresh attempt (a1, a2) with a stable dedup
        key -- never a silent duplicate within one tick."""
        snapshot = {
            "/v1/enrollments": {
                "schema_version": "1",
                "enrollments": [{"folder_id": "wf-1", "alias": "ronin-fresh", "status": "pending"}],
            }
        }
        observer, launcher = observer_for(tmp_path, read_model=read_model_for(snapshot))
        tick(observer, {})
        tick(observer, {})
        events = launcher.events()
        assert len(events) == 2
        assert events[0]["key"] == events[1]["key"] == "enroll-wf-1"
        assert [e["attempt"] for e in events] == [1, 2]
        threads = [validate_event(e).thread_id for e in events]
        assert threads == ["supervisor:enroll-wf-1:a1", "supervisor:enroll-wf-1:a2"]
        # Within one tick the same application is launched at most once.
        observer2, launcher2 = observer_for(tmp_path, read_model=read_model_for(snapshot))
        tick(observer2, {})
        assert len(launcher2.events()) == 1

    def test_e8_missing_or_malformed_snapshot_is_a_skip(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path, read_model=lambda path: None)
        actions = tick(observer, {})
        assert launcher.events() == []
        assert not any("error" in a for a in actions)


class TestBudgets:
    def test_at_most_two_launches_per_tick(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path)
        folders = {
            "wf-a": terminal("fault", "run-1"),
            "wf-b": terminal("fault", "run-2"),
            "wf-c": terminal("fault", "run-3"),
        }
        actions = tick(observer, folders)
        assert len(launcher.events()) == 2
        assert any(a.get("action") == "deferred:tick_budget" for a in actions)

    def test_lifetime_attempts_cap_at_three(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path)
        folders = {"wf-a": terminal("fault", "run-1")}
        for _ in range(5):
            tick(observer, folders)
        assert len(launcher.events()) == 3
        actions = tick(observer, folders)
        assert any("attempts_exhausted" in a.get("action", "") for a in actions)

    def test_attempts_survive_a_restart(self, tmp_path: Path) -> None:
        observer, _launcher = observer_for(tmp_path)
        folders = {"wf-a": terminal("fault", "run-1")}
        for _ in range(3):
            tick(observer, folders)
        # A fresh observer object stands in for a restarted daemon.
        observer2, launcher2 = observer_for(tmp_path)
        tick(observer2, folders)
        assert launcher2.events() == []

    def test_in_flight_audit_burns_no_attempt(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path, units=StaticUnits(active=True))
        folders = {"wf-a": terminal("fault", "run-1")}
        actions = tick(observer, folders)
        assert launcher.events() == []
        assert any(a.get("action") == "skipped:audit_in_flight" for a in actions)
        # The attempt counter did not move.
        state = json.loads(
            (tmp_path / "runs" / ".scheduler" / "supervisor-cursor.json").read_text()
        )
        assert state["attempts"] == {}

    def test_each_launch_is_a_new_attempt_and_a_new_thread(self, tmp_path: Path) -> None:
        """Generation semantics: the launch's attempt number rides in the event
        JSON and therefore in the thread identity -- re-runs never share a
        checkpoint thread with the launch they replace."""
        observer, launcher = observer_for(tmp_path)
        folders = {"wf-a": terminal("fault", "run-1")}
        for _ in range(3):
            tick(observer, folders)
        events = launcher.events()
        assert [e["attempt"] for e in events] == [1, 2, 3]
        threads = [validate_event(e).thread_id for e in events]
        assert threads == [
            "supervisor:e3-run-1:a1",
            "supervisor:e3-run-1:a2",
            "supervisor:e3-run-1:a3",
        ]
        assert len(set(threads)) == 3

    def test_cursor_edits_on_disk_are_honored_next_tick(self, tmp_path: Path) -> None:
        """The observer reloads the cursor file at the start of every tick, so
        `supervisor reset` needs no daemon restart. Pinned here: an external
        edit (clearing attempts) between ticks re-arms the same observer."""
        observer, launcher = observer_for(tmp_path, max_attempts=1)
        folders = {"wf-a": terminal("fault", "run-1")}
        tick(observer, folders)
        actions = tick(observer, folders)
        assert any("attempts_exhausted" in a.get("action", "") for a in actions)
        cursor = tmp_path / "runs" / ".scheduler" / "supervisor-cursor.json"
        state = json.loads(cursor.read_text())
        state["attempts"] = {}
        cursor.write_text(json.dumps(state))
        tick(observer, folders)  # same object, no restart
        assert len(launcher.events()) == 2

    def test_receipt_ends_the_event_for_good(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path)
        reports = tmp_path / "supervisor" / "reports"
        reports.mkdir(parents=True)
        (reports / "e3-run-1.json").write_text("{}")
        actions = tick(observer, {"wf-a": terminal("fault", "run-1")})
        assert launcher.events() == []
        assert any(a.get("action") == "skipped:receipt_exists" for a in actions)


class TestBoardScan:
    def test_first_run_adopts_head_and_processes_nothing(self, tmp_path: Path) -> None:
        bus = FakeBus()
        bus.add_question("msg_old", "card-1", seq=5)
        observer, launcher = observer_for(tmp_path, bus=bus)
        actions = tick(observer, {})
        assert launcher.events() == []
        assert any("cursor_adopted:head_seq=5" in a.get("action", "") for a in actions)

    def test_new_undecided_question_becomes_e1(self, tmp_path: Path) -> None:
        bus = FakeBus()
        observer, launcher = observer_for(tmp_path, bus=bus)
        tick(observer, {})  # adopt baseline
        bus.add_question("msg_q1", "card-1", seq=6)
        tick(observer, {})
        [event] = launcher.events()
        assert event["type"] == "board_question"
        assert event["key"] == "e1-msg_q1"
        assert event["payload"]["card_entity_id"] == "card-1"

    def test_decided_question_is_skipped_and_cursor_advances(self, tmp_path: Path) -> None:
        bus = FakeBus()
        observer, launcher = observer_for(tmp_path, bus=bus)
        tick(observer, {})
        bus.add_question("msg_q1", "card-1", seq=6)
        bus.add_decision_for("msg_q1", "msg_d1", seq=7)
        tick(observer, {})
        assert launcher.events() == []
        # Cursor moved past both messages: the next tick re-reads nothing.
        state = json.loads(
            (tmp_path / "runs" / ".scheduler" / "supervisor-cursor.json").read_text()
        )
        assert state["board_seq"] == 7

    def test_budget_deferral_leaves_cursor_before_the_question(self, tmp_path: Path) -> None:
        bus = FakeBus()
        observer, launcher = observer_for(tmp_path, bus=bus)
        tick(observer, {})
        # A non-question at seq 5 shows the cursor advancing *up to* -- and
        # then stopping *before* -- the deferred question at seq 6.
        bus.notes.append(
            {
                "message_id": "msg_p1",
                "channel_seq": 5,
                "kind": "work.note.v1",
                "payload": {"note_type": "progress", "note": "…"},
            }
        )
        bus.add_question("msg_q1", "card-1", seq=6)
        # Two terminal events eat the whole tick budget.
        folders = {
            "wf-a": terminal("fault", "run-1"),
            "wf-b": terminal("fault", "run-2"),
        }
        tick(observer, folders)
        assert len(launcher.events()) == 2  # the terminals only
        state = json.loads(
            (tmp_path / "runs" / ".scheduler" / "supervisor-cursor.json").read_text()
        )
        assert state["board_seq"] == 5  # past the progress note, before msg_q1
        # Next tick has budget again; the question gets its turn.
        actions = tick(observer, {})
        assert any(e["type"] == "board_question" for e in launcher.events()), actions

    def test_no_bus_means_no_board_scan_and_no_crash(self, tmp_path: Path) -> None:
        observer, launcher = observer_for(tmp_path, bus=None)
        actions = tick(observer, {})
        assert launcher.events() == []
        assert not any("error" in a for a in actions)

    def test_repeated_ticks_do_not_duplicate_the_same_e1(self, tmp_path: Path) -> None:
        bus = FakeBus()
        observer, launcher = observer_for(tmp_path, bus=bus)
        tick(observer, {})  # adopt baseline
        bus.add_question("msg_q1", "card-1", seq=6)
        tick(observer, {})  # launches the one E1 audit
        assert len(launcher.events()) == 1
        # The cursor already advanced past the question, so the same observer
        # re-scanning on later ticks produces no second launch.
        tick(observer, {})
        tick(observer, {})
        assert len(launcher.events()) == 1

    def test_a_restarted_observer_produces_no_second_e1_unit(self, tmp_path: Path) -> None:
        bus = FakeBus()
        observer, launcher = observer_for(tmp_path, bus=bus)
        tick(observer, {})  # adopt baseline
        bus.add_question("msg_q1", "card-1", seq=6)
        tick(observer, {})
        assert len(launcher.events()) == 1
        # A fresh observer object over the same state root stands in for a
        # restarted daemon: the persisted cursor (already past the question)
        # and the persisted attempt mean the audit is not launched again.
        observer2, launcher2 = observer_for(tmp_path, bus=bus)
        tick(observer2, {})
        assert launcher2.events() == []

    def test_e1_receipt_suppression_survives_restart(self, tmp_path: Path) -> None:
        bus = FakeBus()
        observer, launcher = observer_for(tmp_path, bus=bus)
        tick(observer, {})  # adopt baseline
        bus.add_question("msg_q1", "card-1", seq=6)
        tick(observer, {})
        assert len(launcher.events()) == 1
        # The completed audit writes its receipt; rewind the cursor so the
        # question is re-presented to a restarted observer and confirm the
        # receipt -- not the attempt counter -- is what suppresses the relaunch.
        reports = tmp_path / "supervisor" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "e1-msg_q1.json").write_text("{}")
        cursor = tmp_path / "runs" / ".scheduler" / "supervisor-cursor.json"
        state = json.loads(cursor.read_text())
        state["board_seq"] = 5
        cursor.write_text(json.dumps(state))
        observer2, launcher2 = observer_for(tmp_path, bus=bus)
        actions = tick(observer2, {})
        assert launcher2.events() == []
        assert any(a.get("action") == "skipped:receipt_exists" for a in actions)

    def test_e1_inflight_audit_is_readopted_not_relaunched(self, tmp_path: Path) -> None:
        bus = FakeBus()
        observer, launcher = observer_for(tmp_path, bus=bus)
        tick(observer, {})  # adopt baseline
        bus.add_question("msg_q1", "card-1", seq=6)
        tick(observer, {})  # launch attempt 1; cursor advances past seq 6
        assert len(launcher.events()) == 1
        # Rewind the cursor to before the question (a reset-style rewind) and
        # restart with the audit still in flight: the active unit is re-adopted
        # (`skipped:audit_in_flight`), no second unit is created, and the
        # persisted attempt counter is not consumed.
        cursor = tmp_path / "runs" / ".scheduler" / "supervisor-cursor.json"
        state = json.loads(cursor.read_text())
        state["board_seq"] = 5
        cursor.write_text(json.dumps(state))
        observer2, launcher2 = observer_for(tmp_path, bus=bus, units=StaticUnits(active=True))
        actions = tick(observer2, {})
        assert launcher2.events() == []
        assert any(a.get("action") == "skipped:audit_in_flight" for a in actions)
        assert json.loads(cursor.read_text())["attempts"]["e1-msg_q1"] == 1


class TestFailOpen:
    def test_broken_terminal_reader_is_an_action_not_a_crash(self, tmp_path: Path) -> None:
        observer, _launcher = observer_for(tmp_path)

        def reader(_folder: str) -> dict[str, Any]:
            raise RuntimeError("disk on fire")

        actions = observer.after_tick(
            now=0.0, folder_ids=["wf-a"], terminal_reader=reader, tick_results=[]
        )
        assert any(a.get("source") == "terminals" and "error" in a for a in actions)

    def test_broken_bus_is_an_action_not_a_crash(self, tmp_path: Path) -> None:
        class ExplodingBus:
            def messages(self, *a: Any, **kw: Any):
                raise RuntimeError("bus down")

        observer, _ = observer_for(tmp_path, bus=ExplodingBus())
        actions = tick(observer, {})
        assert any(a.get("source") == "board" and "error" in a for a in actions)

    def test_unreadable_cursor_storage_is_a_degrade_not_a_crash(self, tmp_path: Path) -> None:
        """Acceptance item 4 (cursor storage half): a cursor file that cannot be
        read degrades to empty state -- the tick still observes, it never raises."""
        observer, launcher = observer_for(tmp_path)
        cursor = tmp_path / "runs" / ".scheduler" / "supervisor-cursor.json"
        cursor.mkdir(parents=True, exist_ok=True)  # a directory where the JSON file goes
        folders = {"wf-a": terminal("fault", "run-1")}
        actions = tick(observer, folders)
        assert len(launcher.events()) == 1
        assert any(a.get("action") == "launched" for a in actions), actions

    def test_unwritable_cursor_storage_is_a_degrade_not_a_crash(self, tmp_path: Path) -> None:
        """Acceptance item 4 (cursor storage half): a cursor that cannot be written
        back loses the cursor, never the tick. `_write_state` swallows the OSError."""
        observer, launcher = observer_for(tmp_path)
        # A regular file sits where the cursor's parent directory must go, so
        # mkdir/write_text inside _write_state raise OSError (and _load_state's
        # read falls back to empty state).
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        observer.config.cursor_path = blocker / "sub" / "cursor.json"
        folders = {"wf-a": terminal("fault", "run-1")}
        actions = tick(observer, folders)
        assert len(launcher.events()) == 1
        assert any(a.get("action") == "launched" for a in actions), actions


class TestReset:
    """`fleet-graph supervisor reset <key>`: the documented replacement for the
    2026-08-28 four-step surgery. Idempotent, supervisor state surface only."""

    def _paths(self, tmp_path: Path) -> tuple[Path, Path]:
        state_root = tmp_path / "supervisor"
        cursor = tmp_path / "runs" / ".scheduler" / "supervisor-cursor.json"
        return state_root, cursor

    def _seed(self, tmp_path: Path, key: str, *, board_seq: int | None = 9) -> tuple[Path, Path]:
        state_root, cursor = self._paths(tmp_path)
        reports = state_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"{key}.json").write_text("{}")
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text(json.dumps({"board_seq": board_seq, "attempts": {key: 2, "other": 1}}))
        return state_root, cursor

    def test_reset_clears_receipt_and_attempts_and_is_idempotent(self, tmp_path: Path) -> None:
        key = "e3-run-1"
        state_root, cursor = self._seed(tmp_path, key)
        first = reset_supervisor_event(key, state_root=state_root, cursor_path=cursor)
        assert first["receipt"].startswith("deleted:")
        # attempts 保留：计数器正是让下次 launch 拿到新 thread（a{n+1}）的东西。
        # 清零会重派 a{n} 撞旧 thread 的 terminal checkpoint（生产实锤
        # e1-msg_01M12MRW…：reset 后重跑 resumed:already_complete）。
        assert first["attempts"] == "kept:2 (next launch is a3)"
        assert not (state_root / "reports" / f"{key}.json").exists()
        state = json.loads(cursor.read_text())
        assert state["attempts"] == {key: 2, "other": 1}  # untouched
        assert state["board_seq"] == 9  # E3: nothing to rewind

        second = reset_supervisor_event(key, state_root=state_root, cursor_path=cursor)
        assert second["receipt"] == "absent"
        assert second["attempts"] == "kept:2 (next launch is a3)"
        assert json.loads(cursor.read_text()) == state

    def test_the_observer_refires_a_reset_terminal_event(self, tmp_path: Path) -> None:
        """End to end against the real observer: exhaust the key, reset it,
        and the very next tick launches again -- same observer object, no
        restart, because the cursor is reloaded every tick."""
        observer, launcher = observer_for(tmp_path, max_attempts=2)
        folders = {"wf-a": terminal("fault", "run-1")}
        tick(observer, folders)
        reports = tmp_path / "supervisor" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "e3-run-1.json").write_text("{}")  # the finished receipt
        actions = tick(observer, folders)
        assert any(a.get("action") == "skipped:receipt_exists" for a in actions)

        state_root, cursor = self._paths(tmp_path)
        reset_supervisor_event("e3-run-1", state_root=state_root, cursor_path=cursor)
        tick(observer, folders)
        assert len(launcher.events()) == 2
        # attempts 保留下的重跑是 a2——新 thread，绝不撞 a1 的 terminal checkpoint
        assert launcher.events()[-1]["attempt"] == 2

    def test_e1_board_seq_rewinds_mechanically_and_never_forwards(self, tmp_path: Path) -> None:
        key = "e1-msg_q1"
        state_root, cursor = self._seed(tmp_path, key, board_seq=9)
        bus = FakeBus()
        bus.add_question("msg_q1", "card-1", seq=6)
        first = reset_supervisor_event(key, state_root=state_root, cursor_path=cursor, bus=bus)
        assert first["board_seq"] == "rewound:9->5"
        assert json.loads(cursor.read_text())["board_seq"] == 5
        second = reset_supervisor_event(key, state_root=state_root, cursor_path=cursor, bus=bus)
        assert second["board_seq"].startswith("already_at_or_before")
        assert json.loads(cursor.read_text())["board_seq"] == 5

    def test_e1_without_a_locatable_note_says_so_and_takes_the_explicit_seq(
        self, tmp_path: Path
    ) -> None:
        key = "e1-msg_gone"
        state_root, cursor = self._seed(tmp_path, key, board_seq=9)
        no_bus = reset_supervisor_event(key, state_root=state_root, cursor_path=cursor, bus=None)
        assert no_bus["board_seq"].startswith("not_rewound:no bus client")
        missing = reset_supervisor_event(
            key, state_root=state_root, cursor_path=cursor, bus=FakeBus()
        )
        assert missing["board_seq"].startswith("not_rewound:note")
        assert json.loads(cursor.read_text())["board_seq"] == 9  # untouched, not guessed
        explicit = reset_supervisor_event(
            key, state_root=state_root, cursor_path=cursor, board_seq=4
        )
        assert explicit["board_seq"] == "set:4"
        assert json.loads(cursor.read_text())["board_seq"] == 4

    def test_explicit_board_seq_never_moves_the_cursor_forward(self, tmp_path: Path) -> None:
        """The explicit --board-seq path obeys the same discipline as the
        mechanical one: never move the cursor forward past unprocessed
        questions, even when the operator names a higher value."""
        key = "e1-msg_q1"
        state_root, cursor = self._seed(tmp_path, key, board_seq=9)
        summary = reset_supervisor_event(
            key, state_root=state_root, cursor_path=cursor, board_seq=12
        )
        assert summary["board_seq"].startswith("not_moved_forward:9")
        assert json.loads(cursor.read_text())["board_seq"] == 9  # unchanged

    def test_reset_survives_a_missing_cursor_file(self, tmp_path: Path) -> None:
        state_root, cursor = self._paths(tmp_path)
        summary = reset_supervisor_event("e3-run-x", state_root=state_root, cursor_path=cursor)
        assert summary["receipt"] == "absent"
        assert summary["attempts"] == "absent"
        assert json.loads(cursor.read_text())["attempts"] == {}


class TestLaunchSpec:
    def test_argv_targets_the_supervisor_cli(self, tmp_path: Path) -> None:
        spec = SupervisorLaunchSpec(
            event=line_fault_event("wf-a", "run-1"),
            run_root=tmp_path / "runs",
            state_root=tmp_path / "supervisor",
            environment={"PATH": "/usr/bin", "FLEET_GRAPH_BUS_TOKEN_FILE": "/tok"},
        )
        argv = spec.argv()
        assert argv[0] == "systemd-run"
        assert "--unit" in argv
        assert spec.unit_name == "fleet-graph-supervisor-e3-run-1"
        joined = " ".join(argv)
        assert "supervisor run --event-json" in joined
        assert "--setenv=FLEET_GRAPH_BUS_TOKEN_FILE=/tok" in argv


class TestSchedulerWiring:
    def _scheduler(self, tmp_path: Path, supervisor: Any) -> Scheduler:
        config = SchedulerConfig(
            lines=[LineSpec(folder_id="wf-a", seat="seat-1")],
            run_root=tmp_path / "runs",
            maintenance_stop_path=tmp_path / "absent-stop",
        )
        return Scheduler(
            config,
            launcher=RecordingLauncher(),  # never launched: line disabled
            units=StaticUnits(active=False),
            clock=lambda: 1_000.0,
            supervisor=supervisor,
        )

    def test_tick_hands_the_observer_results_and_terminals(self, tmp_path: Path) -> None:
        calls: list[dict[str, Any]] = []

        class Recorder:
            def after_tick(self, **kwargs: Any) -> list[dict[str, Any]]:
                calls.append(kwargs)
                return []

        scheduler = self._scheduler(tmp_path, Recorder())
        scheduler.tick()
        [call] = calls
        assert call["folder_ids"] == ["wf-a"]
        assert callable(call["terminal_reader"])
        assert len(call["tick_results"]) == 1

    def test_supervisor_config_flag_defaults_off(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"lines": []}))
        assert SchedulerConfig.from_json(path).supervisor_events is False
        path.write_text(json.dumps({"lines": [], "supervisor_events": True}))
        assert SchedulerConfig.from_json(path).supervisor_events is True

    def test_a_raising_observer_never_breaks_the_tick(self, tmp_path: Path) -> None:
        class Exploding:
            def after_tick(self, **kwargs: Any) -> None:
                raise RuntimeError("observer on fire")

        scheduler = self._scheduler(tmp_path, Exploding())
        results = scheduler.tick()  # must not raise
        assert len(results) == 1

    def test_terminal_record_carries_pump_fault(self, tmp_path: Path) -> None:
        scheduler = self._scheduler(tmp_path, None)
        line_root = tmp_path / "runs" / "wf-a"
        line_root.mkdir(parents=True)
        (line_root / "terminal.json").write_text(
            json.dumps({"terminal": "fault", "rounds": 1, "run_id": "r", "pump_fault": True})
        )
        record = scheduler.terminal_record("wf-a")
        assert record is not None and record["pump_fault"] is True


class TestObserverEnvironment:
    """R4-3 接线：决策凭证只进 supervisor unit 的 env，且只来自 daemon 自身
    的环境（systemd EnvironmentFile），不来自 config line_environment。"""

    def test_the_decision_credential_rides_only_when_the_daemon_has_it(self) -> None:
        line_env = {"PATH": "/usr/bin", "FLEET_GRAPH_BUS_TOKEN_FILE": "/t/bus.token"}
        out = observer_environment(line_env, {"FLEET_GRAPH_DECISION_TOKEN_FILE": "/t/d.token"})
        assert out["FLEET_GRAPH_DECISION_TOKEN_FILE"] == "/t/d.token"
        # 原 line env 原样保留，且未被就地污染
        assert out["PATH"] == "/usr/bin"
        assert "FLEET_GRAPH_DECISION_TOKEN_FILE" not in line_env

    def test_no_daemon_credential_means_no_key_not_an_empty_key(self) -> None:
        out = observer_environment({"PATH": "/usr/bin"}, {})
        assert "FLEET_GRAPH_DECISION_TOKEN_FILE" not in out
        out2 = observer_environment({"PATH": "/usr/bin"}, {"FLEET_GRAPH_DECISION_TOKEN_FILE": ""})
        assert "FLEET_GRAPH_DECISION_TOKEN_FILE" not in out2

    def test_the_env_name_matches_the_publisher_without_importing_it(self) -> None:
        # supervisor_events 用字面量避开 Guard C；这条测试钉住两处同值，
        # 名字漂移在这里炸而不是在生产静默失联。
        from fleet_graph.supervise.decision_publisher import DECISION_TOKEN_ENV as publisher_name

        assert publisher_name == DECISION_TOKEN_ENV


class TestE1NoDecisionCredential:
    """Blocker fix: the E1 board_question unit -- the only event type that can
    reach the decision publisher -- must not carry the decision credential in
    its launch environment (spec Non-Goals: 'E1 receives no decision
    credential'). E2/E3/E4 keep it, since none of them can publish a decision."""

    def _observer(self, tmp_path: Path, bus: Any) -> tuple[SupervisorObserver, RecordingLauncher]:
        launcher = RecordingLauncher()
        observer = SupervisorObserver(
            ObserverConfig(
                run_root=tmp_path / "runs",
                supervisor_state_root=tmp_path / "supervisor",
                environment={
                    DECISION_TOKEN_ENV: "/run/decision.token",
                    "FLEET_GRAPH_BUS_TOKEN_FILE": "/run/bus.token",
                },
            ),
            launcher=launcher,  # type: ignore[arg-type]
            bus=bus,
            read_model=read_model_for(EMPTY_READ_MODEL),
        )
        return observer, launcher

    def _creates_decision_setenv(self, spec: Any) -> bool:
        return any(arg.startswith(f"--setenv={DECISION_TOKEN_ENV}=") for arg in spec.argv())

    def test_e1_board_question_unit_gets_no_decision_credential(self, tmp_path: Path) -> None:
        bus = FakeBus()
        observer, launcher = self._observer(tmp_path, bus)
        tick(observer, {})  # adopt baseline
        bus.add_question("msg_q1", "card-1", seq=6)
        tick(observer, {})
        assert len(launcher.events()) == 1
        [spec] = launcher.specs
        assert DECISION_TOKEN_ENV not in spec.environment
        assert not self._creates_decision_setenv(spec)

    def test_terminal_events_keep_the_decision_credential(self, tmp_path: Path) -> None:
        bus = FakeBus()
        observer, launcher = self._observer(tmp_path, bus)
        folders = {"wf-a": terminal("fault", "run-1")}
        tick(observer, folders)
        assert len(launcher.events()) == 1
        [spec] = launcher.specs
        assert spec.environment[DECISION_TOKEN_ENV] == "/run/decision.token"
        assert self._creates_decision_setenv(spec)


class TestHarvestWiring:
    """M3 E5 harvest 接线：observer 侧把 harvest 写权旗标补传进
    `supervisor run` argv（spec 契约：带 harvest 字段的 ObserverConfig +
    SupervisorObserver → spec.argv() 中 --harvest-allowlist /
    --harvest-default-branch / --harvest-deploy / --repo 逐项 in argv；
    未配置这些字段时上述旗标不出现——deny-all 默认拒绝零放宽）。"""

    def _observer(
        self, tmp_path: Path, **harvest: Any
    ) -> tuple[SupervisorObserver, RecordingLauncher]:
        launcher = RecordingLauncher()
        observer = SupervisorObserver(
            ObserverConfig(
                run_root=tmp_path / "runs",
                supervisor_state_root=tmp_path / "supervisor",
                **harvest,
            ),
            launcher=launcher,  # type: ignore[arg-type]
            read_model=read_model_for(EMPTY_READ_MODEL),
        )
        return observer, launcher

    def test_argv_carries_harvest_flags_when_configured(self, tmp_path: Path) -> None:
        observer, launcher = self._observer(
            tmp_path,
            harvest_allowlist_path="/data/fleet-graph/supervisor/harvest-allowlist.json",
            harvest_default_branch="master",
            harvest_deploy=["bash", "scripts/deploy.sh"],
            repo="/data/code/self/fleet-harvest-sandbox",
        )
        tick(observer, {"wf-a": terminal("fault", "run-1")})
        [spec] = launcher.specs
        argv = spec.argv()
        assert "--harvest-allowlist" in argv
        assert (
            argv[argv.index("--harvest-allowlist") + 1]
            == "/data/fleet-graph/supervisor/harvest-allowlist.json"
        )
        assert "--harvest-default-branch" in argv
        assert argv[argv.index("--harvest-default-branch") + 1] == "master"
        # 每个 deploy 词一个旗标（cli action="append"）。
        assert argv.count("--harvest-deploy") == 2
        assert argv[argv.index("--harvest-deploy") + 1] == "bash"
        assert argv[argv.index("--harvest-deploy") + 3] == "scripts/deploy.sh"
        assert "--repo" in argv
        assert argv[argv.index("--repo") + 1] == "/data/code/self/fleet-harvest-sandbox"

    def test_unconfigured_observer_emits_no_harvest_flags(self, tmp_path: Path) -> None:
        observer, launcher = self._observer(tmp_path)
        tick(observer, {"wf-a": terminal("fault", "run-1")})
        [spec] = launcher.specs
        argv = spec.argv()
        assert not any(arg.startswith("--harvest-") for arg in argv)
        assert "--repo" not in argv

    def test_launch_spec_without_harvest_fields_emits_no_harvest_flags(
        self, tmp_path: Path
    ) -> None:
        # 阴性（默认拒绝零放宽）：SupervisorLaunchSpec 不带 harvest 字段时
        # argv() 无任何 --harvest-*。
        spec = SupervisorLaunchSpec(
            event=line_fault_event("wf-a", "run-1"),
            run_root=tmp_path / "runs",
            state_root=tmp_path / "supervisor",
        )
        argv = spec.argv()
        assert not any(arg.startswith("--harvest-") for arg in argv)
        assert "--repo" not in argv
        assert "--harvest-allowlist" not in argv
        assert "--harvest-default-branch" not in argv

    def test_config_from_json_reads_harvest_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps(
                {
                    "lines": [],
                    "harvest_allowlist_path": "/data/fleet-graph/supervisor/harvest-allowlist.json",
                    "harvest_default_branch": "master",
                    "harvest_deploy": ["bash", "scripts/deploy.sh"],
                    "repo": "/data/code/self/fleet-harvest-sandbox",
                }
            )
        )
        config = SchedulerConfig.from_json(path)
        assert (
            config.harvest_allowlist_path == "/data/fleet-graph/supervisor/harvest-allowlist.json"
        )
        assert config.harvest_default_branch == "master"
        assert config.harvest_deploy == ["bash", "scripts/deploy.sh"]
        assert config.repo == "/data/code/self/fleet-harvest-sandbox"

    def test_config_from_json_absent_harvest_fields_default_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"lines": []}))
        config = SchedulerConfig.from_json(path)
        assert config.harvest_allowlist_path is None
        assert config.harvest_default_branch is None
        assert config.harvest_deploy == []
        assert config.repo is None


class TestWikiWiring:
    """M4 交付 B.2：wiki 旗标接线——ObserverConfig/SupervisorLaunchSpec 启用时
    argv() 发 `--wiki`；缺省不发（零回归、不改变既有 argv 词法）。"""

    def _observer(
        self, tmp_path: Path, **wiki: Any
    ) -> tuple[SupervisorObserver, RecordingLauncher]:
        launcher = RecordingLauncher()
        observer = SupervisorObserver(
            ObserverConfig(
                run_root=tmp_path / "runs",
                supervisor_state_root=tmp_path / "supervisor",
                **wiki,
            ),
            launcher=launcher,  # type: ignore[arg-type]
            read_model=read_model_for(EMPTY_READ_MODEL),
        )
        return observer, launcher

    def test_argv_carries_wiki_flag_when_enabled(self, tmp_path: Path) -> None:
        observer, launcher = self._observer(tmp_path, wiki=True)
        tick(observer, {"wf-a": terminal("fault", "run-1")})
        [spec] = launcher.specs
        argv = spec.argv()
        assert "--wiki" in argv

    def test_argv_omits_wiki_flag_by_default(self, tmp_path: Path) -> None:
        observer, launcher = self._observer(tmp_path)
        tick(observer, {"wf-a": terminal("fault", "run-1")})
        [spec] = launcher.specs
        argv = spec.argv()
        assert "--wiki" not in argv
        assert not any(arg.startswith("--wiki") for arg in argv)

    def test_launch_spec_without_wiki_emits_no_wiki_flag(self, tmp_path: Path) -> None:
        spec = SupervisorLaunchSpec(
            event=line_fault_event("wf-a", "run-1"),
            run_root=tmp_path / "runs",
            state_root=tmp_path / "supervisor",
        )
        argv = spec.argv()
        assert "--wiki" not in argv

    def test_launch_spec_with_wiki_emits_wiki_flag(self, tmp_path: Path) -> None:
        spec = SupervisorLaunchSpec(
            event=line_fault_event("wf-a", "run-1"),
            run_root=tmp_path / "runs",
            state_root=tmp_path / "supervisor",
            wiki=True,
        )
        assert "--wiki" in spec.argv()
