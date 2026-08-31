"""M4 交付 C：wiki 人话账节点（独立组件）。

用 katana-wiki-mcp 向「舰队开发阶段性成果报告」页追加**带日期分节**（不攒批，
命中任一触发就追加一条）：

- line-done / 生产晋级（harvest HARVESTED）/ 缺陷闭环（E6/E7 处置成功收口）/
  新阶段授权。

写法铁律 §6.5：分节先背景（这条线是什么、为什么做）→ 交付与现状 → 证据指针
（PR 号 / commit / 看板 seq / 真机回显）；**裸 wf-id/订单号等抽象缩写不进正文**
（`sanitize_prose` 把裸 `wf-…`/`dev-fg-…` 等 token 剥掉，证据指针字段是结构化
字段，不受影响）。

机械 postcondition（送达自验，不采信自述）：`page_append` 返回成功 + 回读页含
刚写分节标题。wiki 客户端注入以便测试替换（禁触真网）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

#: The real katana-wiki-mcp surface. This used to point at :5610/mcp/ (the
#: dev-dispatch MCP itself), a same-topic confusion resolved when the goal
#: surface split clarified :5610's job: :5610 is dev-dispatch, and wiki prose
#: lands through the katana-wiki-mcp service on :8113, not the dd control plane.
DEFAULT_WIKI_MCP_URL = "http://127.0.0.1:8113/mcp"
DEFAULT_REPORT_PAGE_TITLE = "舰队开发阶段性成果报告"

#: §6.5：正文不得含的裸抽象缩写（wf-id / dev-fg / order 号等）。
_BARE_ABSTRACT_RE = re.compile(r"\b(?:wf-|dev-fg-|ord-|order-)[A-Za-z0-9_-]+")


class WikiReportError(RuntimeError):
    """wiki MCP 拒绝或不可达，或送达自验失败。"""


class WikiClient(Protocol):
    """katana-wiki-mcp 的注入面。测试注入 fake。"""

    def search(self, title: str) -> list[dict[str, Any]]: ...
    def page_append(self, page_id: str, content: str) -> dict[str, Any]: ...
    def read_page(self, page_id: str) -> str: ...
    def page_create(self, title: str, content: str) -> dict[str, Any]: ...


class DefaultWikiClient:
    """katana-wiki-mcp 的默认实现（streamable-http，同 work_folder 形状）。

    假定工具：`search`（按标题定位）、`page_append`、`read_page`、`page_create`。
    测试一律注入 fake，不触碰真网。
    """

    def __init__(self, url: str = DEFAULT_WIKI_MCP_URL, timeout: float | None = None) -> None:
        self.url = url
        self.timeout = timeout

    def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        import asyncio

        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        def factory(**kwargs: Any) -> Any:
            if self.timeout is not None:
                kwargs["timeout"] = self.timeout
            import httpx

            kwargs["trust_env"] = False
            return httpx.AsyncClient(**kwargs)

        client = Client(StreamableHttpTransport(self.url, httpx_client_factory=factory))
        try:

            async def run() -> Any:
                async with client:
                    result = await client.call_tool(tool, arguments)
                    return _unwrap(result)

            return asyncio.run(run())
        except Exception as exc:
            raise WikiReportError(f"katana-wiki-mcp {tool} failed: {exc}") from exc

    def search(self, title: str) -> list[dict[str, Any]]:
        result = self._call("search", {"query": title})
        if isinstance(result, dict):
            return list(result.get("results") or result.get("pages") or [])
        if isinstance(result, list):
            return result
        return []

    def page_append(self, page_id: str, content: str) -> dict[str, Any]:
        result = self._call("page_append", {"page_id": page_id, "content": content})
        return result if isinstance(result, dict) else {"ok": bool(result)}

    def read_page(self, page_id: str) -> str:
        result = self._call("read_page", {"page_id": page_id})
        if isinstance(result, dict):
            return str(result.get("content") or result.get("text") or "")
        return str(result)

    def page_create(self, title: str, content: str) -> dict[str, Any]:
        result = self._call("page_create", {"title": title, "content": content})
        return result if isinstance(result, dict) else {"ok": bool(result)}


def _unwrap(result: Any) -> Any:
    """fastmcp 结果解包：structured_content / content[].text / 原样。"""
    data = getattr(result, "structured_content", None) or getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    content = getattr(result, "content", None)
    if content:
        text = getattr(content[0], "text", None)
        if text is not None:
            return text
    return result


def sanitize_prose(text: str) -> str:
    """§6.5：把正文里裸抽象缩写 token 剥掉（证据指针字段不受影响）。"""
    return _BARE_ABSTRACT_RE.sub("<abstract-id>", text)


@dataclass(frozen=True)
class WikiSection:
    """一条带日期分节：标题 + 正文（§6.5 结构）。"""

    title: str
    background: str
    delivery: str
    evidence: tuple[str, ...]
    at: str

    def render(self) -> tuple[str, str]:
        """(分节标题, 分节正文)。标题含日期；正文先背景 → 交付与现状 → 证据指针。"""
        section_title = f"{self.title}（{self.at}）"
        lines = [
            f"## {section_title}",
            "",
            "**背景**",
            sanitize_prose(self.background).strip(),
            "",
            "**交付与现状**",
            sanitize_prose(self.delivery).strip(),
        ]
        if self.evidence:
            lines.append("")
            lines.append("**证据指针**")
            for pointer in self.evidence:
                lines.append(f"- {pointer}")
        body = "\n".join(lines)
        return section_title, body


def locate_or_create_page(client: WikiClient, page_title: str, skeleton: str) -> str:
    """按标题定位报告页；页不存在按该页「报告更新约定」骨架重建。

    返回 page_id。骨架内容由调用方提供（报告更新约定的头注骨架）。
    """
    hits = client.search(page_title)
    for hit in hits or []:
        if isinstance(hit, dict):
            candidate = str(hit.get("title") or hit.get("name") or "")
            if candidate == page_title or page_title in candidate:
                return str(hit.get("page_id") or hit.get("id") or "")
    created = client.page_create(page_title, skeleton)
    return str(created.get("page_id") or created.get("id") or "")


def append_achievement_section(
    client: WikiClient,
    *,
    page_title: str = DEFAULT_REPORT_PAGE_TITLE,
    skeleton: str,
    section: WikiSection,
) -> dict[str, Any]:
    """追加一条带日期分节，机械送达自验（page_append 成功 + 回读含分节标题）。

    不攒批：一次触发一条。失败抛 WikiReportError（调用方 best-effort 降级）。
    """
    page_id = locate_or_create_page(client, page_title, skeleton)
    section_title, body = section.render()
    appended = client.page_append(page_id, body)
    if isinstance(appended, dict) and appended.get("ok") is False:
        raise WikiReportError(f"page_append 返回失败: {appended}")
    readback = client.read_page(page_id)
    if section_title not in readback:
        raise WikiReportError(f"送达自验失败：回读页不含分节标题 {section_title!r}")
    return {
        "page_title": page_title,
        "page_id": page_id,
        "section_title": section_title,
        "readback_present": True,
    }


# --- 四类触发 ---------------------------------------------------------------


def record_line_done(
    client: WikiClient,
    *,
    line_name: str,
    background: str,
    delivery: str,
    evidence: tuple[str, ...],
    at: str,
    skeleton: str,
    page_title: str = DEFAULT_REPORT_PAGE_TITLE,
) -> dict[str, Any]:
    section = WikiSection(
        title=f"line-done：{line_name}",
        background=background,
        delivery=delivery,
        evidence=evidence,
        at=at,
    )
    return append_achievement_section(
        client, page_title=page_title, skeleton=skeleton, section=section
    )


def record_production_promotion(
    client: WikiClient,
    *,
    development_name: str,
    background: str,
    delivery: str,
    evidence: tuple[str, ...],
    at: str,
    skeleton: str,
    page_title: str = DEFAULT_REPORT_PAGE_TITLE,
) -> dict[str, Any]:
    section = WikiSection(
        title=f"生产晋级：{development_name}",
        background=background,
        delivery=delivery,
        evidence=evidence,
        at=at,
    )
    return append_achievement_section(
        client, page_title=page_title, skeleton=skeleton, section=section
    )


def record_defect_closed(
    client: WikiClient,
    *,
    defect_name: str,
    background: str,
    delivery: str,
    evidence: tuple[str, ...],
    at: str,
    skeleton: str,
    page_title: str = DEFAULT_REPORT_PAGE_TITLE,
) -> dict[str, Any]:
    section = WikiSection(
        title=f"缺陷闭环：{defect_name}",
        background=background,
        delivery=delivery,
        evidence=evidence,
        at=at,
    )
    return append_achievement_section(
        client, page_title=page_title, skeleton=skeleton, section=section
    )


def record_stage_authorized(
    client: WikiClient,
    *,
    stage_name: str,
    background: str,
    delivery: str,
    evidence: tuple[str, ...],
    at: str,
    skeleton: str,
    page_title: str = DEFAULT_REPORT_PAGE_TITLE,
) -> dict[str, Any]:
    section = WikiSection(
        title=f"新阶段授权：{stage_name}",
        background=background,
        delivery=delivery,
        evidence=evidence,
        at=at,
    )
    return append_achievement_section(
        client, page_title=page_title, skeleton=skeleton, section=section
    )


__all__ = [
    "DEFAULT_REPORT_PAGE_TITLE",
    "DEFAULT_WIKI_MCP_URL",
    "DefaultWikiClient",
    "WikiClient",
    "WikiReportError",
    "WikiSection",
    "append_achievement_section",
    "locate_or_create_page",
    "record_defect_closed",
    "record_line_done",
    "record_production_promotion",
    "record_stage_authorized",
    "sanitize_prose",
]
