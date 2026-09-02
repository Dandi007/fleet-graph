"""M4 交付 A：E6 处置反应器单测（stop → 代谢重拉）。

覆盖 spec 交付 E.1：

1. 合成 E6 heartbeat_stale 事件 -> stop 目标 unit + postcondition（is-active 非 0）。
2. 越界（非本 folder 的 unit）-> refused 留痕不 stop（fake ops 记录零次 stop）。
3. postcondition 缺（stop 后仍 active 且 :7494 心跳龄未回落）-> escalated。
4. 事件词表负例零回归：validate_event unknown 事件名仍拒绝。

所有 systemctl/read-model 操作都是注入的 fake ops，绝不触碰真实系统。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.supervise.e6_stop import (
    OUTCOME_ESCALATED,
    OUTCOME_REFUSED,
    OUTCOME_STOPPED,
    SOP_STEPS,
    E6StopRunConfig,
    run_e6_stop,
)
from fleet_graph.supervise.events import SupervisorEventError, heartbeat_stale_event, validate_event


def fake_ops(
    *,
    unit: str = "fleet-graph-line-wf-a-g1",
    resolve_ok: bool = True,
    active_after_stop: bool = False,
    read_model_age: float | None = None,
    stop_exit: int = 0,
    board_card_entity_id: str | None = None,
) -> dict[str, Any]:
    """A recording fake E6 ops: resolve/stop/is-active/read-model all scripted."""
    calls: list[str] = []

    class Ops:
        def resolve_line_unit(self, folder_id: str, run_root: Path) -> dict[str, Any]:
            calls.append("resolve_line_unit")
            if not resolve_ok:
                return {"ok": False, "detail": "no active unit and no stall-state generation"}
            return {"ok": True, "unit": unit, "source": "list-units"}

        def is_active(self, unit_name: str) -> bool:
            calls.append("is_active")
            return active_after_stop

        def stop_unit(self, unit_name: str) -> int:
            calls.append("stop_unit")
            return stop_exit

        def line_heartbeat_age_s(self, folder_id: str) -> float | None:
            calls.append("line_heartbeat_age_s")
            return read_model_age

        def board_card_entity_id(self, folder_id: str, run_root: Path) -> str | None:
            calls.append("board_card_entity_id")
            return board_card_entity_id

    return {"ops": Ops(), "calls": calls}


class FakeBus:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, channel, kind, payload, idempotency_key, *, refs=None, **_kw):
        class _Result:
            message_id = f"msg_{len(self.published)}"

        self.published.append(
            {
                "channel": channel,
                "kind": kind,
                "payload": payload,
                "idempotency_key": idempotency_key,
                "refs": refs or [],
            }
        )
        return _Result()


def config_for(
    tmp_path: Path,
    *,
    ops: Any | None = None,
    bus: Any | None = None,
    publish_notes: bool = False,
    **overrides: Any,
) -> E6StopRunConfig:
    event = heartbeat_stale_event(
        folder_id="wf-a", heartbeat_age_s=600.0, round=3, phase="coordinator"
    ).as_dict()
    fake = ops or fake_ops()
    config = E6StopRunConfig(
        event=event,
        state_root=tmp_path / "supervisor",
        run_root=tmp_path / "runs",
        ops=fake["ops"],
        bus=bus,
        publish_notes=publish_notes,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config, fake


class TestHappyPath:
    def test_synthetic_e6_stops_the_resolved_unit(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake)
        result = run_e6_stop(config)
        assert result["outcome"] == OUTCOME_STOPPED
        assert fake["calls"] == ["resolve_line_unit", "stop_unit", "is_active"], fake["calls"]
        ran_steps = [s["step"] for s in result["steps"]]
        for step in SOP_STEPS:
            assert step in ran_steps, f"{step} missing from {ran_steps}"
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["unit"] == "fleet-graph-line-wf-a-g1"
        assert receipt["active_after"] is False

    def test_stop_postcondition_confirms_is_active_nonzero(self, tmp_path: Path) -> None:
        """postcondition：stop 后 is-active 非 0（不再 active）即达成，不采信自述。"""
        fake = fake_ops(active_after_stop=False)
        config, _ = config_for(tmp_path, ops=fake)
        result = run_e6_stop(config)
        assert result["outcome"] == OUTCOME_STOPPED
        post = next(s for s in result["steps"] if s["step"] == "postconditions")
        assert post["active_after"] is False
        assert post["ok"] is True

    def test_finished_event_is_a_no_op_on_rerun(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake)
        first = run_e6_stop(config)
        second = run_e6_stop(config)
        assert second["resumed"] == "already_complete"
        assert second["receipt_path"] == first["receipt_path"]
        assert fake["calls"].count("stop_unit") == 1, "re-run re-executed a stop"


class TestGateRefusal:
    def test_out_of_bounds_unit_refuses_and_records_without_stopping(self, tmp_path: Path) -> None:
        """越界（非本 folder 的 unit）-> refused + 留痕，不 stop。"""
        fake = fake_ops(unit="fleet-graph-line-wf-other-g1")
        config, fake = config_for(tmp_path, ops=fake)
        result = run_e6_stop(config)
        assert result["outcome"] == OUTCOME_REFUSED
        assert "stop_unit" not in fake["calls"], f"stop executed: {fake['calls']}"
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        gate = next(s for s in receipt["steps"] if s["step"] == "gate")
        assert gate["evidence"]["granted"] is False
        assert any("不是 wf-a 自己的 line unit" in r for r in gate["evidence"]["reasons"])

    def test_empty_folder_refuses_at_intake(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake)
        config.event = heartbeat_stale_event(
            folder_id="", heartbeat_age_s=600.0, round=1, phase="coordinator"
        ).as_dict()
        result = run_e6_stop(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert fake["calls"] == []

    def test_non_wf_folder_refuses_at_intake(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake)
        config.event = heartbeat_stale_event(
            folder_id="dev-x", heartbeat_age_s=600.0, round=1, phase="coordinator"
        ).as_dict()
        result = run_e6_stop(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert fake["calls"] == []


class TestResolveEscalation:
    def test_unresolvable_unit_escalates_without_stop(self, tmp_path: Path) -> None:
        fake = fake_ops(resolve_ok=False)
        config, fake = config_for(tmp_path, ops=fake)
        result = run_e6_stop(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert fake["calls"] == ["resolve_line_unit"], f"stop executed: {fake['calls']}"


class TestPostconditionEscalation:
    def test_still_active_without_age_drop_escalates(self, tmp_path: Path) -> None:
        """postcondition 缺：stop 后仍 active 且 :7494 心跳龄未回落 -> escalated。"""
        fake = fake_ops(active_after_stop=True, read_model_age=600.0)
        config, fake = config_for(tmp_path, ops=fake)
        result = run_e6_stop(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        post = next(s for s in receipt["steps"] if s["step"] == "postconditions")
        assert post["ok"] is False
        assert any("仍 active" in m for m in post["missing"])

    def test_still_active_but_age_dropped_accepts(self, tmp_path: Path) -> None:
        """:7494 心跳龄回落（<= threshold）也视为达成。"""
        fake = fake_ops(active_after_stop=True, read_model_age=10.0)
        config, fake = config_for(tmp_path, ops=fake)
        result = run_e6_stop(config)
        assert result["outcome"] == OUTCOME_STOPPED


class TestVocabularyNegative:
    def test_validate_event_still_refuses_unknown_names(self) -> None:
        with pytest.raises(SupervisorEventError, match="vocabulary is closed"):
            validate_event({"type": "harvest_ready", "key": "e9-x", "payload": {}})

    def test_e6_event_round_trips(self) -> None:
        event = heartbeat_stale_event("wf-a", 600.0, 3, "coordinator")
        assert validate_event(event.as_dict()) == event
        assert event.key == "e6-wf-a"


class TestEvidenceNoteRefTarget:
    """交付 C：evidence note 的 ref 目标必须是真实板实体，缺失即 best-effort skip。"""

    def test_evidence_note_targets_real_board_card_entity(self, tmp_path: Path) -> None:
        """有卡：card_entity_id 与 refs target 都填真实板实体，绝不填 folder_id。"""
        bus = FakeBus()
        fake = fake_ops(unit="fleet-graph-line-wf-fdd6ac-g1", board_card_entity_id="card-xyz")
        config, fake = config_for(tmp_path, ops=fake, bus=bus, publish_notes=True)
        config.event = heartbeat_stale_event(
            folder_id="wf-fdd6ac", heartbeat_age_s=600.0, round=3, phase="coordinator"
        ).as_dict()
        result = run_e6_stop(config)
        assert result["outcome"] == OUTCOME_STOPPED
        assert len(bus.published) == 1, f"published: {bus.published}"
        note = bus.published[0]
        assert note["payload"]["card_entity_id"] == "card-xyz"
        targets = {ref["target_entity"] for ref in note["refs"]}
        assert targets == {"card-xyz"}
        assert "wf-fdd6ac" not in targets

    def test_evidence_note_skips_when_no_board_card(self, tmp_path: Path) -> None:
        """无卡：零发布 + evidence_note ok=False + detail 含「缺失」。"""
        bus = FakeBus()
        fake = fake_ops(board_card_entity_id=None)
        config, fake = config_for(tmp_path, ops=fake, bus=bus, publish_notes=True)
        result = run_e6_stop(config)
        assert result["outcome"] == OUTCOME_STOPPED
        assert bus.published == [], f"published: {bus.published}"
        evidence = next(s for s in result["steps"] if s["step"] == "evidence_note")
        assert evidence["ok"] is False
        assert "缺失" in evidence["detail"]


class TestDefaultOpsBoardCardRead:
    """交付 A：DefaultE6Ops.board_card_entity_id 读 stall-state 的 board_card_entity_id。"""

    def test_reads_board_card_entity_id_from_stall_state(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.e6_ops import DefaultE6Ops

        run_root = tmp_path / "runs"
        stall = run_root / ".scheduler"
        stall.mkdir(parents=True)
        (stall / "wf-a.json").write_text(
            json.dumps({"generation": 3, "board_card_entity_id": "card-xyz"})
        )
        ops = DefaultE6Ops()
        assert ops.board_card_entity_id("wf-a", run_root) == "card-xyz"

    def test_missing_or_empty_board_card_entity_id_returns_none(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.e6_ops import DefaultE6Ops

        run_root = tmp_path / "runs"
        stall = run_root / ".scheduler"
        stall.mkdir(parents=True)
        (stall / "wf-a.json").write_text(json.dumps({"generation": 3}))
        (stall / "wf-b.json").write_text(
            json.dumps({"generation": 3, "board_card_entity_id": None})
        )
        (stall / "wf-c.json").write_text(json.dumps({"generation": 3, "board_card_entity_id": ""}))
        ops = DefaultE6Ops()
        assert ops.board_card_entity_id("wf-a", run_root) is None
        assert ops.board_card_entity_id("wf-b", run_root) is None
        assert ops.board_card_entity_id("wf-c", run_root) is None
        assert ops.board_card_entity_id("wf-missing", run_root) is None


class FakeWiki:
    """Recording fake wiki client for M4 交付 C (record_defect_closed assertion)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.pages: dict[str, str] = {}
        self.fail = fail

    def search(self, title: str) -> list[dict[str, Any]]:
        self.calls.append("search")
        if self.fail:
            from fleet_graph.supervise.wiki_report import WikiReportError

            raise WikiReportError("wiki down")
        return [{"title": title, "page_id": "page-1"}]

    def page_append(self, page_id: str, content: str) -> dict[str, Any]:
        self.calls.append("page_append")
        if self.fail:
            from fleet_graph.supervise.wiki_report import WikiReportError

            raise WikiReportError("wiki down")
        self.pages[page_id] = self.pages.get(page_id, "") + content
        return {"ok": True}

    def read_page(self, page_id: str) -> str:
        self.calls.append("read_page")
        if self.fail:
            from fleet_graph.supervise.wiki_report import WikiReportError

            raise WikiReportError("wiki down")
        return self.pages.get(page_id, "")

    def page_create(self, title: str, content: str) -> dict[str, Any]:
        self.calls.append("page_create")
        page_id = "page-new"
        self.pages[page_id] = content
        return {"ok": True, "page_id": page_id}


class TestWikiDefectClosed:
    """M4 交付 C：E6 STOPPED 时 wiki 客户端非 None -> record_defect_closed 被调用
    （追加「缺陷闭环」分节）；失败 best-effort 不咬 stop 语义。"""

    def test_stopped_appends_defect_closed_section(self, tmp_path: Path) -> None:
        wiki = FakeWiki()
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake, wiki=wiki)
        result = run_e6_stop(config)
        assert result["outcome"] == OUTCOME_STOPPED
        assert wiki.calls.count("page_append") == 1, wiki.calls
        body = "".join(wiki.pages.values())
        assert "缺陷闭环：" in body
        assert "E6 停牌" in body

    def test_wiki_failure_does_not_bite_stop_semantics(self, tmp_path: Path) -> None:
        wiki = FakeWiki(fail=True)
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake, wiki=wiki)
        result = run_e6_stop(config)
        # stop 语义不变：STOPPED + wiki_report ok:false 留痕（receipt 节点只回
        # receipt_path，steps 落盘）。
        assert result["outcome"] == OUTCOME_STOPPED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        wiki_step = next(s for s in receipt["steps"] if s["step"] == "wiki_report")
        assert wiki_step["ok"] is False
        assert "wiki 追加失败" in wiki_step["detail"]
