"""M4 交付 C：wiki 人话账节点单测。

覆盖 spec 交付 E.3：

1. fake wiki client -> 分节追加 + 分节标题在场（page_append 成功 + 回读含标题）。
2. §6.5 铁律（证据指针字段）断言：分节先背景 → 交付与现状 → 证据指针；裸
   wf-id/订单号等抽象缩写不进正文（sanitize_prose 剥掉）。
3. 送达自验失败（回读不含标题）-> 抛 WikiReportError。

所有 wiki 操作都是注入的 fake client，绝不触碰真网。
"""

from __future__ import annotations

from typing import Any

import pytest

from fleet_graph.supervise.wiki_report import (
    DEFAULT_REPORT_PAGE_TITLE,
    WikiReportError,
    WikiSection,
    append_achievement_section,
    record_defect_closed,
    record_line_done,
    record_production_promotion,
    record_stage_authorized,
    sanitize_prose,
)


class FakeWiki:
    """Recording fake wiki client: search/page_append/read_page/page_create."""

    def __init__(self, *, readback_contains: bool = True, search_hit: bool = True) -> None:
        self.calls: list[str] = []
        self.pages: dict[str, str] = {}
        self._readback_contains = readback_contains
        self._search_hit = search_hit
        self.page_id_counter = 0

    def search(self, title: str) -> list[dict[str, Any]]:
        self.calls.append("search")
        if not self._search_hit:
            return []
        return [{"title": title, "page_id": "page-1"}]

    def page_append(self, page_id: str, content: str) -> dict[str, Any]:
        self.calls.append("page_append")
        self.pages[page_id] = self.pages.get(page_id, "") + content
        return {"ok": True}

    def read_page(self, page_id: str) -> str:
        self.calls.append("read_page")
        return self.pages.get(page_id, "")

    def page_create(self, title: str, content: str) -> dict[str, Any]:
        self.calls.append("page_create")
        self.page_id_counter += 1
        page_id = f"page-{self.page_id_counter}"
        self.pages[page_id] = content
        return {"ok": True, "page_id": page_id}


def section() -> WikiSection:
    return WikiSection(
        title="line-done：引擎事件化重构",
        background="把 fleet-graph 控制面从 goal-driven 手工编排改成事件化重构。",
        delivery="E1-E5 事件已合入，supervisor 对账闭环上线。",
        evidence=("PR #164", "commit 7a081f2", "看板 seq 128", "真机回显 make verify 全绿"),
        at="2026-08-31T00:00:00Z",
    )


class TestSectionRendering:
    def test_section_follows_65_order_background_then_delivery_then_evidence(self) -> None:
        section_title, body = section().render()
        assert section_title == "line-done：引擎事件化重构（2026-08-31T00:00:00Z）"
        bg = body.index("**背景**")
        delivery = body.index("**交付与现状**")
        evidence = body.index("**证据指针**")
        assert bg < delivery < evidence
        assert "引擎事件化重构" in body

    def test_evidence_pointers_are_present(self) -> None:
        _, body = section().render()
        for pointer in ("PR #164", "commit 7a081f2", "看板 seq 128", "真机回显 make verify 全绿"):
            assert pointer in body, pointer

    def test_bare_wf_id_is_stripped_from_prose(self) -> None:
        assert sanitize_prose("这条线 wf-66300e 正在做") == "这条线 <abstract-id> 正在做"
        assert sanitize_prose("dev-fg-2dd4c415b7ce 已晋级") == "<abstract-id> 已晋级"
        # 证据指针字段不走 sanitize——结构化字段原样保留。
        assert sanitize_prose("PR #164") == "PR #164"


class TestAppend:
    def test_fake_wiki_appends_section_and_readback_confirms_title(
        self,
    ) -> None:
        wiki = FakeWiki()
        result = append_achievement_section(
            wiki,
            page_title=DEFAULT_REPORT_PAGE_TITLE,
            skeleton="# 舰队发展阶段性成果报告\n\n按「报告更新约定」追加分节。\n",
            section=section(),
        )
        assert result["readback_present"] is True
        assert result["section_title"] == section().render()[0]
        assert "search" in wiki.calls
        assert "page_append" in wiki.calls
        assert "read_page" in wiki.calls
        assert "page-1" in wiki.pages
        assert result["section_title"] in wiki.pages["page-1"]

    def test_page_missing_rebuilds_by_skeleton(self) -> None:
        wiki = FakeWiki(search_hit=False)
        result = record_defect_closed(
            wiki,
            defect_name="E6 停牌",
            background="一条线 heartbeat 超龄停摆，需要 stop 后自然重拉。",
            delivery="E6 处置已收口，unit 已 stop，scheduler 下一 tick 重拉。",
            evidence=("PR #166",),
            at="2026-08-31T00:00:00Z",
            skeleton="# 舰队发展阶段性成果报告\n\n骨架。\n",
        )
        assert "page_create" in wiki.calls
        assert result["section_title"] == "缺陷闭环：E6 停牌（2026-08-31T00:00:00Z）"

    def test_readback_without_title_is_a_delivery_failure(self) -> None:
        class BadWiki(FakeWiki):
            def page_append(self, page_id: str, content: str) -> dict[str, Any]:
                self.calls.append("page_append")
                return {"ok": True}

        wiki = BadWiki()
        with pytest.raises(WikiReportError, match="送达自验失败"):
            append_achievement_section(
                wiki,
                skeleton="# 报告\n",
                section=section(),
            )

    def test_failed_page_append_is_a_delivery_failure(self) -> None:
        class RejectWiki(FakeWiki):
            def page_append(self, page_id: str, content: str) -> dict[str, Any]:
                self.calls.append("page_append")
                return {"ok": False, "error": "refused"}

        wiki = RejectWiki()
        with pytest.raises(WikiReportError, match="page_append 返回失败"):
            append_achievement_section(
                wiki,
                skeleton="# 报告\n",
                section=section(),
            )


class TestRemainingTriggers:
    """M4 交付 C.2：其余三类触发（line-done / 生产晋级 / 新阶段授权）同样走
    分节追加 + 送达自验 + §6.5 证据指针，不能只测缺陷闭环一条腿。"""

    def test_line_done_trigger_appends_section_and_confirms_title(self) -> None:
        wiki = FakeWiki()
        result = record_line_done(
            wiki,
            line_name="引擎事件化重构",
            background="把控制面从 goal-driven 手工编排改成事件化重构。",
            delivery="E1-E5 事件已合入，supervisor 对账闭环上线。",
            evidence=("PR #164", "commit 7a081f2", "看板 seq 128"),
            at="2026-08-31T00:00:00Z",
            skeleton="# 舰队发展阶段性成果报告\n\n骨架。\n",
        )
        assert result["readback_present"] is True
        assert result["section_title"] == "line-done：引擎事件化重构（2026-08-31T00:00:00Z）"
        assert "search" in wiki.calls and "page_append" in wiki.calls and "read_page" in wiki.calls
        body = wiki.pages["page-1"]
        assert "**背景**" in body and "**交付与现状**" in body and "**证据指针**" in body
        assert "PR #164" in body and "commit 7a081f2" in body and "看板 seq 128" in body

    def test_production_promotion_trigger_appends_section(self) -> None:
        wiki = FakeWiki()
        result = record_production_promotion(
            wiki,
            development_name="M4 破障/汇报节点",
            background="M4 破障/汇报节点开发完成（dev-fg-2dd4c415b7ce）并合入。",
            delivery="harvest HARVESTED，产品 commit 已落 main。",
            evidence=("PR #170",),
            at="2026-08-31T00:00:00Z",
            skeleton="# 舰队发展阶段性成果报告\n\n骨架。\n",
        )
        assert result["readback_present"] is True
        assert result["section_title"] == "生产晋级：M4 破障/汇报节点（2026-08-31T00:00:00Z）"
        body = wiki.pages["page-1"]
        # §6.5：裸 dev-fg- 缩写不进正文（证据指针是结构化字段，原样保留）。
        assert "dev-fg-2dd4c415b7ce" not in body
        assert "PR #170" in body

    def test_stage_authorized_trigger_appends_section_and_confirms_title(self) -> None:
        wiki = FakeWiki()
        result = record_stage_authorized(
            wiki,
            stage_name="M5",
            background="M4 交付通过验收，进入下一阶段。",
            delivery="新阶段已授权，watch 值守面继续盯。",
            evidence=("看板 seq 200",),
            at="2026-08-31T00:00:00Z",
            skeleton="# 舰队发展阶段性成果报告\n\n骨架。\n",
        )
        assert result["readback_present"] is True
        assert result["section_title"] == "新阶段授权：M5（2026-08-31T00:00:00Z）"
        body = wiki.pages["page-1"]
        assert "**背景**" in body and "**交付与现状**" in body and "**证据指针**" in body
        assert "看板 seq 200" in body

    def test_line_done_with_bare_order_token_is_sanitized_in_prose(self) -> None:
        wiki = FakeWiki()
        record_line_done(
            wiki,
            line_name="引擎事件化重构",
            background="这条线 order-928 的收尾工作。",
            delivery="ord-928 已闭环。",
            evidence=("看板 seq 130",),
            at="2026-08-31T00:00:00Z",
            skeleton="# 报告\n",
        )
        body = wiki.pages["page-1"]
        assert "<abstract-id> 的收尾工作" in body
        assert "<abstract-id> 已闭环" in body
        assert "看板 seq 130" in body
