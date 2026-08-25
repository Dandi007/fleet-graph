"""Work-folder MCP client -- the only durable state fleet-graph owns.

Invariant 4: durable state is the work folder plus git, and the LangGraph
checkpointer is a discardable cache. Everything a human needs to read, or that
must survive a rebuild, goes through here.

The server is katana-work-folder-mcp over streamable-http. `folder_id` is an
opaque token: it comes back from wf_create / wf_search / wf_list and is passed
along verbatim. It is never parsed, joined onto a path, or turned into a
filesystem location -- the whole point of the MCP cutover is that clients do
not know where the data lives.

Reuse note: ronin-mcp already talks to this server with fastmcp.Client, so this
follows that shape rather than hand-rolling JSON-RPC over SSE.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_WORK_FOLDER_MCP_URL = "http://127.0.0.1:5602/mcp/"


class WorkFolderError(RuntimeError):
    """The MCP refused or could not be reached."""


class WorkFolderBroken(WorkFolderError):
    """wf_resume reported BROKEN.

    House rule: stop. Do not improvise around a broken folder -- report the
    blockage and wait for a human. This is deliberately a distinct type so a
    caller cannot lump it in with transient failures and retry.
    """


class ToolCaller(Protocol):
    """The seam tests substitute for a live MCP."""

    def call(self, tool: str, arguments: dict[str, Any]) -> Any: ...


class FastMCPCaller:
    """Calls one tool per short-lived session, like the ronin-mcp backend."""

    def __init__(self, url: str = DEFAULT_WORK_FOLDER_MCP_URL) -> None:
        self.url = url

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        return asyncio.run(self._call(tool, arguments))

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        client = Client(
            StreamableHttpTransport(self.url, httpx_client_factory=_loopback_httpx_client)
        )
        try:
            async with client:
                result = await client.call_tool(tool, arguments)
        except Exception as exc:  # transport, protocol, or tool error
            raise WorkFolderError(f"work-folder MCP {tool} failed: {exc}") from exc
        return _unwrap(result)


def _loopback_httpx_client(**kwargs: Any) -> Any:
    """httpx client that refuses to proxy.

    Same hazard as the bus client: this host exports a SOCKS proxy, and httpx
    honours it by default even for 127.0.0.1. The work-folder MCP is loopback,
    so proxying is always wrong -- and here it does not merely misroute, it
    fails to connect at all, because the socks extra is not installed.

    kwargs is passed through: fastmcp decides what it needs (headers, timeout,
    auth, follow_redirects, ...) and that set has changed between versions.
    Only trust_env is ours to force.
    """
    import httpx

    kwargs["trust_env"] = False
    return httpx.AsyncClient(**kwargs)


def _unwrap(result: Any) -> Any:
    """Pull the payload out of an MCP tool result.

    fastmcp hands back structured content when the tool declares it and a list
    of content blocks otherwise; the katana tools answer with a JSON object
    encoded as text, so try that before giving up.
    """
    data = getattr(result, "structured_content", None) or getattr(result, "data", None)
    if isinstance(data, dict):
        return data

    content = getattr(result, "content", None)
    if content:
        text = getattr(content[0], "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return result


@dataclass(frozen=True)
class ResumeReport:
    folder_id: str
    blocked: bool
    verification: str
    progress: str
    context: str
    raw: dict[str, Any]


class WorkFolder:
    """Durable state for one work folder.

    Every method is a passthrough to the MCP tool of the same name. There is no
    caching layer on purpose: a stale read here is a graph acting on a state a
    human already changed.
    """

    def __init__(self, folder_id: str, caller: ToolCaller | None = None) -> None:
        self.folder_id = folder_id
        self._caller = caller if caller is not None else FastMCPCaller()

    # --- lifecycle -------------------------------------------------------

    @classmethod
    def resume(
        cls, folder_id: str, caller: ToolCaller | None = None
    ) -> tuple[WorkFolder, ResumeReport]:
        folder = cls(folder_id, caller)
        raw = folder._call("wf_resume", {"folder_id": folder_id})
        report = ResumeReport(
            folder_id=folder_id,
            blocked=bool(raw.get("blocked", False)),
            verification=str((raw.get("verification") or {}).get("overall", "")),
            progress=str((raw.get("loaded") or {}).get("progress") or ""),
            context=str((raw.get("loaded") or {}).get("context") or ""),
            raw=raw,
        )
        if report.blocked or report.verification == "BROKEN":
            raise WorkFolderBroken(
                f"wf_resume({folder_id}) came back BROKEN -- stop and report, do not improvise"
            )
        return folder, report

    def append_progress(
        self, entry: str, *, source_session_id: str, idempotency_key: str
    ) -> dict[str, Any]:
        return self._call(
            "wf_append_progress",
            {
                "folder_id": self.folder_id,
                "entry": entry,
                "source_session_id": source_session_id,
                "idempotency_key": idempotency_key,
            },
        )

    def save(self, *, summary: str, findings_addition: str | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {"folder_id": self.folder_id, "summary": summary}
        if findings_addition:
            arguments["findings_addition"] = findings_addition
        return self._call("wf_save", arguments)

    # --- files -----------------------------------------------------------

    def read(self, filename: str) -> str:
        result = self._call("fs_read", {"folder_id": self.folder_id, "filename": filename})
        return str(result.get("content", ""))

    def create(self, filename: str, content: str) -> dict[str, Any]:
        """Create a new file. fs_write does not create -- that is the server's rule."""
        return self._call(
            "fs_create",
            {"folder_id": self.folder_id, "filename": filename, "content": content},
        )

    def write(self, filename: str, content: str) -> dict[str, Any]:
        """Overwrite an existing file. Fails if it does not exist; use create()."""
        return self._call(
            "fs_write",
            {"folder_id": self.folder_id, "filename": filename, "content": content},
        )

    def edit(self, filename: str, old_string: str, new_string: str) -> dict[str, Any]:
        return self._call(
            "fs_edit",
            {
                "folder_id": self.folder_id,
                "filename": filename,
                "old_string": old_string,
                "new_string": new_string,
            },
        )

    def list(self, dirname: str = "") -> list[dict[str, Any]]:
        result = self._call("fs_list", {"folder_id": self.folder_id, "dirname": dirname})
        return list(result.get("entries", []))

    # --- internals -------------------------------------------------------

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._caller.call(tool, arguments)
        if not isinstance(result, dict):
            raise WorkFolderError(f"{tool} returned {type(result).__name__}, expected an object")
        if result.get("ok") is False:
            raise WorkFolderError(f"{tool} failed: {json.dumps(result, ensure_ascii=False)[:400]}")
        return result


__all__ = [
    "DEFAULT_WORK_FOLDER_MCP_URL",
    "ResumeReport",
    "WorkFolder",
    "WorkFolderBroken",
    "WorkFolderError",
]
