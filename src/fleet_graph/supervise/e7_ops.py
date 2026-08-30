"""E7 处置反应器的机械操作层：决策链解析 + work-folder goal.md 直写（被编排层调用）。

编排层（`supervise/e7_write.py`）只调用 `E7Ops` 协议方法；这里的 `DefaultE7Ops`
是默认实现，负责：

- `resolve_folder_id` —— 机械决策链：source_message_id -> decision 消息 ->
  payload.card_entity_id -> card head -> payload.work_folder_id（复用 supervisor
  `_folder_id` 同款读取：结构化 card head payload，禁从 prose 解析）。
- goal.md 直写信道 —— 经 `state/work_folder.py::WorkFolder` 的
  `fs_edit`/`fs_write`/`fs_read`/`fs_stat` 向该线自己的 goal.md 追加固定块模板。

本模块只执行被 E7 编排层 gate（`authorize_e7_write`，直写目标圈点）判定之后的
目标；它自己不判断 allowlist——判定是编排层的写门，这里是门的另一侧（生成-验证
分离，同 Guard D/Guard E 纪律）。
"""

from __future__ import annotations

from typing import Any

from fleet_graph.bus.board import WORK_NOTES, Board
from fleet_graph.bus.client import BusClient
from fleet_graph.state.work_folder import FastMCPCaller, WorkFolder, WorkFolderError

DEFAULT_WORK_FOLDER_MCP_URL = "http://127.0.0.1:5602/mcp/"

#: goal.md 文件名（E7 直写目标）。
GOAL_FILENAME = "goal.md"

#: E7 送达失败块：监督面直写署名（固定，不写任意 prose）。
SUPERVISOR_SIGNATURE = "fleet-graph-supervisor（监督面直写）"


class E7ResolutionError(RuntimeError):
    """E7 决策链解析失败。调用方记 gap 并 escalated，不猜、不降级静默。"""


class E7Ops:
    def resolve_folder_id(self, bus: BusClient | None, source_message_id: str) -> str:
        """机械决策链 -> card head work_folder_id（wf- 前缀），否则抛 E7ResolutionError。"""
        raise NotImplementedError

    def goal_revision(self, folder_id: str) -> str:
        """fs_stat goal.md 的 content_revision。"""
        raise NotImplementedError

    def append_delivery_fail_block(self, folder_id: str, block: str) -> dict[str, Any]:
        """向 goal.md 追加固定块，返回机械事实（before/after revision + 回读在场）。"""
        raise NotImplementedError

    def read_goal(self, folder_id: str) -> str:
        """fs_read goal.md 全文（送达自验回读）。"""
        raise NotImplementedError


class DefaultE7Ops(E7Ops):
    """决策链解析 + WorkFolder 封装。work_folder_caller 是测试缝。"""

    def __init__(self, work_folder_caller: Any | None = None) -> None:
        self._caller = work_folder_caller

    def _work_folder(self, folder_id: str) -> WorkFolder:
        if self._caller is not None:
            return WorkFolder(folder_id, self._caller)
        return WorkFolder(folder_id, FastMCPCaller(DEFAULT_WORK_FOLDER_MCP_URL))

    def resolve_folder_id(self, bus: BusClient | None, source_message_id: str) -> str:
        if bus is None:
            raise E7ResolutionError("无 bus 凭证——决策链不可解析")
        decision = bus.message(WORK_NOTES, source_message_id)
        if decision is None:
            raise E7ResolutionError(f"decision {source_message_id} 不在 {WORK_NOTES} 频道")
        payload = decision.get("payload") or {}
        card_entity_id = str(payload.get("card_entity_id") or "")
        if not card_entity_id:
            raise E7ResolutionError(
                f"decision {source_message_id} payload 无 card_entity_id——决策链断裂"
            )
        head = Board(bus).card_head(card_entity_id)
        if head is None:
            raise E7ResolutionError(f"card {card_entity_id} 无 head——决策链断裂")
        head_payload = head.get("payload") or {}
        folder_id = str(head_payload.get("work_folder_id") or "")
        if not folder_id.startswith("wf-"):
            raise E7ResolutionError(
                f"card head work_folder_id {folder_id!r} 不是 wf- 前缀——不是目标线"
            )
        return folder_id

    def goal_revision(self, folder_id: str) -> str:
        try:
            stat = self._work_folder(folder_id).stat(GOAL_FILENAME)
        except WorkFolderError as exc:
            raise E7ResolutionError(f"fs_stat goal.md 失败: {exc}") from exc
        revision = str(stat.get("content_revision") or "")
        if not revision:
            raise E7ResolutionError(f"fs_stat goal.md 未返回 content_revision（{folder_id}）")
        return revision

    def read_goal(self, folder_id: str) -> str:
        try:
            return self._work_folder(folder_id).read(GOAL_FILENAME)
        except WorkFolderError as exc:
            raise E7ResolutionError(f"fs_read goal.md 失败: {exc}") from exc

    def append_delivery_fail_block(self, folder_id: str, block: str) -> dict[str, Any]:
        """读-追加-写 goal.md，返回 before/after content_revision + 回读在场。

        WorkFolder.write 是覆盖整文件；「追加」= 读当前 + 拼 block + 写回。这是
        机械写动作，编排层 gate（authorize_e7_write）判定之后才会调用到这里。
        """
        wf = self._work_folder(folder_id)
        before = self.goal_revision(folder_id)
        current = wf.read(GOAL_FILENAME)
        new_content = (current.rstrip() + "\n\n" + block).strip() + "\n"
        try:
            wf.write(GOAL_FILENAME, new_content)
        except WorkFolderError as exc:
            raise E7ResolutionError(f"fs_write goal.md 失败: {exc}") from exc
        after = self.goal_revision(folder_id)
        readback = wf.read(GOAL_FILENAME)
        # 回读在场：块首行标题必须出现在回读正文（机械送达自验，不采信自述）。
        marker = block.splitlines()[0].strip() if block.splitlines() else ""
        present = bool(marker) and marker in readback
        return {
            "before_revision": before,
            "after_revision": after,
            "revision_changed": before != after,
            "readback_present": present,
            "marker": marker,
        }


__all__ = [
    "DEFAULT_WORK_FOLDER_MCP_URL",
    "GOAL_FILENAME",
    "SUPERVISOR_SIGNATURE",
    "DefaultE7Ops",
    "E7Ops",
    "E7ResolutionError",
]
