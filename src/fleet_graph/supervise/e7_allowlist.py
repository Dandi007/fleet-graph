"""E7 goal.md 直写目标线白名单（M4 交付 B.5：与 M3 allowlist 同款、独立配置）。

E7 处置反应器要向某条线的 goal.md 直写送达失败块——这个写动作**唯一**的正当性
来源就是命中本白名单：`folder_id` 圈点（默认 deny-all，未命中即拒绝 + 留痕，
绝不静默放行）。本模块是纯函数：只做配置解析、校验与判定，不执行任何 work-folder
写入、不发布任何东西（与 harvest_allowlist.py 同款纪律）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


#: E7 直写目标线白名单：默认 deny-all 时一条也不圈。
class E7WriteAllowlistError(ValueError):
    """配置不合法。拒收，不猜——坏配置按无写权限对待。"""


@dataclass(frozen=True)
class E7WriteAuthorization:
    """一次 E7 直写判定的结果。granted=False 时 reasons 即留痕内容。"""

    granted: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"granted": self.granted, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class E7WriteAllowlist:
    """E7 goal.md 直写目标线白名单。默认 deny-all：空 folder_ids 不授予任何写权限。"""

    folder_ids: tuple[str, ...] = ()

    @classmethod
    def default(cls) -> E7WriteAllowlist:
        """未合入 allowlist 的默认形态：空白名单，写权限恒被拒。"""
        return cls(folder_ids=())

    def authorize(self, folder_id: str) -> E7WriteAuthorization:
        """判定一次 goal.md 直写是否命中白名单。deny-all 默认；命不中即拒绝+留痕。"""
        if not folder_id:
            return E7WriteAuthorization(granted=False, reasons=("folder_id 为空——无可圈定目标线",))
        if folder_id in self.folder_ids:
            return E7WriteAuthorization(granted=True, reasons=())
        return E7WriteAuthorization(
            granted=False,
            reasons=(f"folder_id {folder_id!r} 不在 E7 直写目标线白名单（默认 deny-all）",),
        )


def parse_e7_write_allowlist(raw: Any) -> E7WriteAllowlist:
    """从已解析的 JSON 对象构建白名单；任何不合法处抛 E7WriteAllowlistError。

    配置形状：``{"folder_ids": ["wf-...", ...]}``。folder_id 必须是 `wf-` 前缀的
    非空字符串；任何一项不合法 -> 整体拒收（坏配置按无写权限对待）。
    """
    if not isinstance(raw, dict):
        raise E7WriteAllowlistError(f"E7 白名单顶层必须是 JSON 对象，got {type(raw).__name__}")
    folder_raw = raw.get("folder_ids", [])
    if not isinstance(folder_raw, list):
        raise E7WriteAllowlistError("folder_ids 必须是列表")
    folder_ids: list[str] = []
    for item in folder_raw:
        if not isinstance(item, str) or not item:
            raise E7WriteAllowlistError(f"folder_id 必须是非空字符串，got {item!r}")
        if not item.startswith("wf-"):
            raise E7WriteAllowlistError(f"folder_id {item!r} 不是 wf- 前缀——不是目标线")
        folder_ids.append(item)
    return E7WriteAllowlist(folder_ids=tuple(folder_ids))


def load_e7_write_allowlist(path: str | Path) -> E7WriteAllowlist:
    """从配置文件加载白名单。

    缺失/不可读/解析失败一律返回默认 deny-all（spec：白名单未合入前 E7 goal.md
    直写无任何权限）——坏配置按无写权限对待，绝不静默放行。
    """
    import json

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return E7WriteAllowlist.default()
    try:
        return parse_e7_write_allowlist(raw)
    except E7WriteAllowlistError:
        return E7WriteAllowlist.default()


__all__ = [
    "E7WriteAllowlist",
    "E7WriteAllowlistError",
    "E7WriteAuthorization",
    "load_e7_write_allowlist",
    "parse_e7_write_allowlist",
]
