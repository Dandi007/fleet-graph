"""WF 与消息投递仅通过显式配置的 MCP，不推导服务端目录。"""

from __future__ import annotations

import asyncio
import base64
import json

from fastmcp import Client


class MCPPort:
    def __init__(self, url, timeout=30):
        self.url, self.timeout = url, timeout

    async def call(self, name, arguments):
        async def invoke():
            async with Client(self.url, timeout=self.timeout) as client:
                result = await client.call_tool(name, arguments)
                if result.is_error:
                    raise RuntimeError(str(result))
                if result.structured_content is not None:
                    return result.structured_content
                return json.loads(result.content[0].text)

        return await asyncio.wait_for(invoke(), self.timeout)

    async def goal(self, folder_id):
        resumed = await self.call("wf_resume", {"folder_id": folder_id})
        if (
            not resumed.get("ok", True)
            or resumed.get("blocked")
            or resumed.get("verification", {}).get("overall") == "BROKEN"
        ):
            raise ValueError("WF 恢复被阻塞，等待用户决策")
        parts, offset, revision = [], 0, None
        while True:
            result = await self.call(
                "fs_read_bytes",
                {"folder_id": folder_id, "filename": "goal.md", "offset": offset, "limit": 262144},
            )
            if not result.get("ok", False) or result.get("encoding") != "base64":
                raise ValueError("WF goal.md 读取失败")
            current = result.get("content_revision")
            if revision is not None and revision != current:
                raise ValueError("WF goal.md 在分页读取期间变化，请重新 enroll")
            revision = current
            parts.append(base64.b64decode(result["content_base64"], validate=True))
            if result["eof"]:
                break
            if result["next_offset"] <= offset:
                raise ValueError("WF 分页游标未前进")
            offset = result["next_offset"]
        return b"".join(parts).decode("utf-8")


class ReplyPort:
    def __init__(self, port=None, tool=None):
        self.port, self.tool = port, tool

    async def send(self, request, text, action_id):
        if not self.port or not self.tool:
            raise RuntimeError("未配置具有幂等键与查询能力的 reply MCP")
        result = await self.port.call(
            self.tool,
            {
                "request_id": request["request_id"],
                "caller": request["caller"],
                "reply_to": request.get("reply_to"),
                "text": text,
                "idempotency_key": action_id,
            },
        )

        if not isinstance(result, dict) or result.get("ok") is False:
            raise RuntimeError(f"reply MCP 投递未确认：{result!r}")
        return result
