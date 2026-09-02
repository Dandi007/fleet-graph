"""M4 交付 B：E7 处置反应器单测（goal.md 直写信道 + 送达自验）。

覆盖 spec 交付 E.2：

1. 合成 E7 decision_swallowed 事件 -> goal.md 直写 + content_revision/回读验证
   送达自验（fs_stat 变化 + fs_read 块正文在场）。
2. 解析不到 folder（决策链断裂）-> escalated。
3. 传送失败（content_revision 未变化）-> escalated。
4. validate_event unknown 事件名仍拒绝（负例保留）。

所有 bus/work-folder 操作都是注入的 fake，绝不触碰真实网络。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.supervise.e7_allowlist import E7WriteAllowlist, parse_e7_write_allowlist
from fleet_graph.supervise.e7_write import (
    OUTCOME_DELIVERED,
    OUTCOME_ESCALATED,
    OUTCOME_REFUSED,
    SOP_STEPS,
    E7WriteRunConfig,
    build_delivery_fail_block,
    run_e7_write,
)
from fleet_graph.supervise.events import (
    SupervisorEventError,
    decision_swallowed_event,
    validate_event,
)


def fake_ops(
    *,
    folder_id: str = "wf-a",
    revision_changed: bool = True,
    readback_present: bool = True,
    resolve_raises: bool = False,
) -> dict[str, Any]:
    """A recording fake E7 ops: decision chain + goal.md write scripted."""
    calls: list[str] = []

    class Ops:
        def resolve_folder_id(self, bus: Any, source_message_id: str) -> str:
            calls.append("resolve_folder_id")
            if resolve_raises:
                from fleet_graph.supervise.e7_ops import E7ResolutionError

                raise E7ResolutionError(f"decision {source_message_id} 决策链断裂")
            return folder_id

        def goal_revision(self, folder_id: str) -> str:
            calls.append("goal_revision")
            return "rev-1" if not revision_changed else "rev-before"

        def append_delivery_fail_block(self, folder_id: str, block: str) -> dict[str, Any]:
            calls.append("append_delivery_fail_block")
            return {
                "before_revision": "rev-before",
                "after_revision": "rev-before" if not revision_changed else "rev-after",
                "revision_changed": revision_changed,
                "readback_present": readback_present,
                "marker": "## E7 送达失败（监督面直写）",
            }

        def read_goal(self, folder_id: str) -> str:
            calls.append("read_goal")
            return "## E7 送达失败（监督面直写）\n" if readback_present else "# goal\n"

    return {"ops": Ops(), "calls": calls}


class FakeBus:
    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []

    def add_decision(self, source_message_id: str, card_entity_id: str) -> None:
        self.notes.append(
            {
                "message_id": source_message_id,
                "channel_seq": len(self.notes) + 1,
                "kind": "work.decision.v1",
                "payload": {"decision": "APPROVE", "card_entity_id": card_entity_id},
            }
        )
        self.notes.append(
            {
                "message_id": f"card-{card_entity_id}",
                "channel_seq": len(self.notes) + 1,
                "kind": "work.card.v1",
                "entity_id": card_entity_id,
                "payload": {"work_folder_id": card_entity_id},
            }
        )

    def message(self, channel: str, message_id: str) -> dict[str, Any] | None:
        for note in self.notes:
            if note["message_id"] == message_id:
                return note
        return None

    def messages(self, channel: str, *, limit: int = 100, after_seq: int = 0):
        selected = [n for n in self.notes if n["channel_seq"] > after_seq][:limit]
        head = max((n["channel_seq"] for n in self.notes), default=0)
        return selected, head

    def refs_to(self, entity_id: str) -> list[dict[str, Any]]:
        return []

    def publish(self, channel, kind, payload, idempotency_key, *, refs=None, **_kw):
        self.published.append(
            {
                "channel": channel,
                "kind": kind,
                "payload": payload,
                "idempotency_key": idempotency_key,
                "refs": refs or [],
            }
        )

        class _Result:
            message_id = f"msg_pub_{len(self.published)}"

        return _Result()


def full_allowlist(*folder_ids: str) -> E7WriteAllowlist:
    return parse_e7_write_allowlist({"folder_ids": list(folder_ids)})


def config_for(
    tmp_path: Path,
    *,
    ops: Any | None = None,
    bus: Any | None = None,
    allowlist: E7WriteAllowlist | None = None,
    publish_notes: bool = False,
    **overrides: Any,
) -> E7WriteRunConfig:
    event = decision_swallowed_event(source_message_id="msg_sw", reason="noop").as_dict()
    fake = ops or fake_ops()
    config = E7WriteRunConfig(
        event=event,
        state_root=tmp_path / "supervisor",
        run_root=tmp_path / "runs",
        allowlist=allowlist or full_allowlist("wf-a"),
        ops=fake["ops"],
        bus=bus,
        publish_notes=publish_notes,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config, fake


class TestHappyPath:
    def test_synthetic_e7_direct_writes_goal_and_verifies_delivery(self, tmp_path: Path) -> None:
        bus = FakeBus()
        bus.add_decision("msg_sw", "wf-a")
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake, bus=bus)
        result = run_e7_write(config)
        assert result["outcome"] == OUTCOME_DELIVERED
        assert fake["calls"] == [
            "resolve_folder_id",
            "append_delivery_fail_block",
        ], fake["calls"]
        ran_steps = [s["step"] for s in result["steps"]]
        for step in SOP_STEPS:
            assert step in ran_steps, f"{step} missing from {ran_steps}"
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["folder_id"] == "wf-a"
        assert receipt["write_facts"]["revision_changed"] is True
        assert receipt["write_facts"]["readback_present"] is True

    def test_delivery_self_verify_confirms_revision_and_readback(self, tmp_path: Path) -> None:
        """postcondition：fs_stat content_revision 变化 + fs_read 块正文在场。"""
        bus = FakeBus()
        bus.add_decision("msg_sw", "wf-a")
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake, bus=bus)
        result = run_e7_write(config)
        assert result["outcome"] == OUTCOME_DELIVERED
        post = next(s for s in result["steps"] if s["step"] == "postconditions")
        assert post["ok"] is True

    def test_block_template_is_closed_form(self) -> None:
        block = build_delivery_fail_block("msg_sw", "noop", at="2026-08-31T00:00:00Z")
        assert "## E7 送达失败（监督面直写）" in block
        assert "- source_message_id: msg_sw" in block
        assert "- reason: noop" in block
        assert "- at: 2026-08-31T00:00:00Z" in block
        assert "监督面直写" in block

    def test_finished_event_is_a_no_op_on_rerun(self, tmp_path: Path) -> None:
        bus = FakeBus()
        bus.add_decision("msg_sw", "wf-a")
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake, bus=bus)
        first = run_e7_write(config)
        second = run_e7_write(config)
        assert second["resumed"] == "already_complete"
        assert second["receipt_path"] == first["receipt_path"]
        assert fake["calls"].count("append_delivery_fail_block") == 1


class TestAllowlistRefusal:
    def test_folder_outside_allowlist_refuses_without_write(self, tmp_path: Path) -> None:
        bus = FakeBus()
        bus.add_decision("msg_sw", "wf-other")
        fake = fake_ops(folder_id="wf-other")
        config, fake = config_for(tmp_path, ops=fake, bus=bus, allowlist=full_allowlist("wf-a"))
        result = run_e7_write(config)
        assert result["outcome"] == OUTCOME_REFUSED
        assert "append_delivery_fail_block" not in fake["calls"], f"wrote: {fake['calls']}"
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        gate = next(s for s in receipt["steps"] if s["step"] == "gate")
        assert gate["evidence"]["granted"] is False
        assert any("不在 E7 直写目标线白名单" in r for r in gate["evidence"]["reasons"])

    def test_default_deny_all_refuses_everything(self, tmp_path: Path) -> None:
        bus = FakeBus()
        bus.add_decision("msg_sw", "wf-a")
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake, bus=bus, allowlist=E7WriteAllowlist.default())
        result = run_e7_write(config)
        assert result["outcome"] == OUTCOME_REFUSED
        assert "append_delivery_fail_block" not in fake["calls"]


class TestResolutionEscalation:
    def test_unresolvable_folder_escalates_without_write(self, tmp_path: Path) -> None:
        fake = fake_ops(resolve_raises=True)
        config, fake = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_e7_write(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert "append_delivery_fail_block" not in fake["calls"]

    def test_missing_source_message_id_escalates(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake)
        config.event = decision_swallowed_event(source_message_id="", reason="noop").as_dict()
        result = run_e7_write(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert fake["calls"] == []


class TestDeliveryEscalation:
    def test_revision_unchanged_escalates(self, tmp_path: Path) -> None:
        """传送失败：content_revision 未变化 -> escalated。"""
        bus = FakeBus()
        bus.add_decision("msg_sw", "wf-a")
        fake = fake_ops(revision_changed=False)
        config, fake = config_for(tmp_path, ops=fake, bus=bus)
        result = run_e7_write(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        post = next(s for s in receipt["steps"] if s["step"] == "postconditions")
        assert post["ok"] is False
        assert any("content_revision 未变化" in m for m in post["missing"])

    def test_readback_missing_escalates(self, tmp_path: Path) -> None:
        """送达未证：回读不含块标题 -> escalated。"""
        bus = FakeBus()
        bus.add_decision("msg_sw", "wf-a")
        fake = fake_ops(readback_present=False)
        config, fake = config_for(tmp_path, ops=fake, bus=bus)
        result = run_e7_write(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        post = next(s for s in receipt["steps"] if s["step"] == "postconditions")
        assert any("回读不含块标题" in m for m in post["missing"])


class TestVocabularyNegative:
    def test_validate_event_still_refuses_unknown_names(self) -> None:
        with pytest.raises(SupervisorEventError, match="vocabulary is closed"):
            validate_event({"type": "harvest_ready", "key": "e9-x", "payload": {}})

    def test_e7_event_round_trips(self) -> None:
        event = decision_swallowed_event("msg_sw", "noop")
        assert validate_event(event.as_dict()) == event
        assert event.key == "e7-msg_sw"


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
    """M4 交付 C：E7 DELIVERED 时 wiki 客户端非 None -> record_defect_closed 被调用
    （追加「缺陷闭环」分节）；失败 best-effort 不咬 goal.md 直写语义。"""

    def _delivered_config(self, tmp_path: Path, wiki: Any):
        bus = FakeBus()
        bus.add_decision("msg_sw", "wf-a")
        fake = fake_ops()
        config, fake = config_for(tmp_path, ops=fake, bus=bus, wiki=wiki)
        return config, fake

    def test_delivered_appends_defect_closed_section(self, tmp_path: Path) -> None:
        wiki = FakeWiki()
        config, _ = self._delivered_config(tmp_path, wiki)
        result = run_e7_write(config)
        assert result["outcome"] == OUTCOME_DELIVERED
        assert wiki.calls.count("page_append") == 1, wiki.calls
        body = "".join(wiki.pages.values())
        assert "缺陷闭环：" in body
        assert "E7 送达失败" in body

    def test_wiki_failure_does_not_bite_delivery_semantics(self, tmp_path: Path) -> None:
        wiki = FakeWiki(fail=True)
        config, fake = self._delivered_config(tmp_path, wiki)
        result = run_e7_write(config)
        # 直写语义不变：DELIVERED + wiki_report ok:false 留痕（receipt 节点只回
        # receipt_path，steps 落盘）。
        assert result["outcome"] == OUTCOME_DELIVERED
        assert "append_delivery_fail_block" in fake["calls"]
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        wiki_step = next(s for s in receipt["steps"] if s["step"] == "wiki_report")
        assert wiki_step["ok"] is False
        assert "wiki 追加失败" in wiki_step["detail"]
