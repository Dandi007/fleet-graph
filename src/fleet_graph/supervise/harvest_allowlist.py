"""收割写权限白名单（M3 交付 A：allowlist 先行；M5 补线分支新语义）。

参照 `supervise/preauth.py` 的语义：独立主体 + 机械判定 + 默认拒绝。与 preauth
面向「放行 decision」不同，这里面向「harvest 子图的写动作」——收割要把产品
commit 落进分支、跑部署脚本，这些写动作**唯一**的正当性来源就是命中本白名单
条目。

三条铁律（与 spec 逐条对应）：

- **独立配置**：字段含 `repo_path`（可写仓库绝对路径）+ `allowed_branches`
  （允许写入的分支/ref 前缀列表，前缀语义，不用正则）+ `allowed_deploy`
  （允许执行的部署脚本/命令 argv 列表，精确 argv 匹配）。
- **越界写拒绝并留痕**：写目标不在白名单 -> `granted=False` + 机器可读 reasons，
  调用方（harvest 子图的写步骤）据此拒绝执行并把 reasons 记进 receipt/evidence，
  绝不静默放行。
- **默认 deny-all**：空 allowlist（未合入、配置文件缺失、解析失败）不授予任何
  写权限——harvest 子图在 allowlist 合入之前结构性没有写能力。

M5 新语义（圈 `release/<line-id>` 可写仓）：条目可携带 `line_id`（合法行 id），
它把 `release/<line-id>` 展开为该仓的允许分支前缀——收割反应器（M3 起改接
merge 段之后）据此前进线分支，而 main 不在展开范围内：main 只能经显式
`allowed_branches` 前缀授予，且只归 goal 级上线器一条 release→main 的路。

本模块是纯函数：只做配置解析、校验与判定，不执行任何 git/部署命令，不发布
任何东西（与 preauth.py 同款纪律）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleet_graph.dd.line_branch import line_ref_for

#: 分支/ref 前缀允许出现的合法字符集——与 preauth 的 scope 纪律一致：前缀读得出来，
#: 不用正则、不接受通配。
_ALLOWED_PREFIX_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-"
)


class HarvestAllowlistError(ValueError):
    """配置不合法。拒收，不猜——坏配置按无写权限对待。"""


@dataclass(frozen=True)
class HarvestAllowlistEntry:
    """一条可写目标：哪个仓库、哪些分支/ref 前缀、哪些部署命令。

    `line_id` 是 M5 新语义：非空时把 `release/<line-id>` 展开为允许前缀之一
    （圈线分支可写仓）。展开发生在解析期，`allowed_branches` 里看得见展开结果。
    """

    repo_path: str
    allowed_branches: tuple[str, ...]
    allowed_deploy: tuple[tuple[str, ...], ...]
    line_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "repo_path": self.repo_path,
            "allowed_branches": list(self.allowed_branches),
            "allowed_deploy": [list(argv) for argv in self.allowed_deploy],
        }
        if self.line_id:
            payload["line_id"] = self.line_id
        return payload


@dataclass(frozen=True)
class HarvestAuthorization:
    """一次写判定的结果。granted=False 时 reasons 即留痕内容。"""

    granted: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"granted": self.granted, "reasons": list(self.reasons)}


def _refusal_for_prefix(prefix: str) -> str | None:
    """一个分支/ref 前缀被拒绝的理由，合法则 None。"""
    if not isinstance(prefix, str) or not prefix.strip():
        return "前缀必须是非空字符串"
    if any(ch.isspace() for ch in prefix):
        return f"前缀 {prefix!r} 含空白字符"
    if any(ch not in _ALLOWED_PREFIX_CHARS for ch in prefix):
        return f"前缀 {prefix!r} 含非法字符——只认 ref 字符集，不接受通配或模式"
    return None


def _parse_entry(raw: Any) -> HarvestAllowlistEntry:
    if not isinstance(raw, dict):
        raise HarvestAllowlistError(f"白名单条目必须是 JSON 对象，got {type(raw).__name__}")
    repo_path = raw.get("repo_path")
    if not isinstance(repo_path, str) or not repo_path.startswith("/"):
        raise HarvestAllowlistError(
            f"repo_path 必须是绝对路径字符串，got {repo_path!r}（拒绝相对/空路径）"
        )

    line_id = raw.get("line_id", "")
    if line_id is None:
        line_id = ""
    if not isinstance(line_id, str):
        raise HarvestAllowlistError(f"{repo_path}: line_id 必须是字符串，got {line_id!r}")
    line_branch_prefix = ""
    if line_id:
        try:
            line_branch_prefix = line_ref_for(line_id)
        except Exception as exc:
            raise HarvestAllowlistError(f"{repo_path}: {exc}") from exc

    branches_raw = raw.get("allowed_branches")
    if branches_raw is None:
        branches_raw = []
    if not isinstance(branches_raw, list):
        raise HarvestAllowlistError(f"{repo_path}: allowed_branches 必须是列表")
    if not branches_raw and not line_branch_prefix:
        raise HarvestAllowlistError(
            f"{repo_path}: allowed_branches 必须是非空列表（或携带 line_id 圈线分支）"
        )
    branches: list[str] = []
    for item in branches_raw:
        if not isinstance(item, str):
            raise HarvestAllowlistError(
                f"{repo_path}: allowed_branches 项必须是字符串，got {item!r}"
            )
        refusal = _refusal_for_prefix(item)
        if refusal is not None:
            raise HarvestAllowlistError(f"{repo_path}: {refusal}")
        branches.append(item)
    if line_branch_prefix and line_branch_prefix not in branches:
        # M5 圈线语义：line_id 展开成该线的 release 分支前缀。main 不在展开
        # 范围内——线分支收割永不落目标分支。
        branches.append(line_branch_prefix)

    deploy_raw = raw.get("allowed_deploy", [])
    deploy: list[tuple[str, ...]] = []
    if deploy_raw is not None:
        if not isinstance(deploy_raw, list):
            raise HarvestAllowlistError(f"{repo_path}: allowed_deploy 必须是列表")
        for argv in deploy_raw:
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(part, str) and part for part in argv)
            ):
                raise HarvestAllowlistError(
                    f"{repo_path}: 部署命令必须是非空字符串 argv 列表，got {argv!r}"
                )
            deploy.append(tuple(argv))

    return HarvestAllowlistEntry(
        repo_path=repo_path,
        allowed_branches=tuple(branches),
        allowed_deploy=tuple(deploy),
        line_id=line_id,
    )


@dataclass(frozen=True)
class HarvestAllowlist:
    """收割写白名单。默认 deny-all：空 entries 不授予任何写权限。"""

    entries: tuple[HarvestAllowlistEntry, ...] = ()

    @classmethod
    def default(cls) -> HarvestAllowlist:
        """未合入 allowlist 的默认形态：空白名单，写权限恒被拒。"""
        return cls(entries=())

    def entry_for(self, repo_path: str) -> HarvestAllowlistEntry | None:
        for entry in self.entries:
            if entry.repo_path == repo_path:
                return entry
        return None

    def authorize(
        self,
        *,
        repo_path: str,
        branch: str,
        deploy: tuple[str, ...] = (),
    ) -> HarvestAuthorization:
        """判定一次写动作是否命中白名单。deny-all 默认；命不中即拒绝+留痕。"""
        reasons: list[str] = []
        entry = self.entry_for(repo_path)
        if entry is None:
            reasons.append(f"repo_path {repo_path!r} 不在收割写白名单（默认 deny-all）")
            return HarvestAuthorization(granted=False, reasons=tuple(reasons))

        if not branch or not any(branch.startswith(prefix) for prefix in entry.allowed_branches):
            reasons.append(f"分支/ref {branch!r} 不在白名单 {list(entry.allowed_branches)} 内")

        if deploy and not any(argv == deploy for argv in entry.allowed_deploy):
            reasons.append(
                f"部署命令 {list(deploy)!r} 不在白名单 {[list(a) for a in entry.allowed_deploy]} 内"
            )

        return HarvestAuthorization(granted=not reasons, reasons=tuple(reasons))


def parse_harvest_allowlist(raw: Any) -> HarvestAllowlist:
    """从已解析的 JSON 对象构建白名单；任何不合法处抛 HarvestAllowlistError。"""
    if not isinstance(raw, dict):
        raise HarvestAllowlistError(f"白名单顶层必须是 JSON 对象，got {type(raw).__name__}")
    entries_raw = raw.get("entries", [])
    if not isinstance(entries_raw, list):
        raise HarvestAllowlistError("entries 必须是列表")
    return HarvestAllowlist(entries=tuple(_parse_entry(e) for e in entries_raw))


def load_harvest_allowlist(path: str | Path) -> HarvestAllowlist:
    """从配置文件加载白名单。

    缺失/不可读/解析失败一律返回默认 deny-all（spec：allowlist 未合入前 harvest
    无任何写权限）——坏配置按无写权限对待，绝不静默放行。
    """
    import json

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return HarvestAllowlist.default()
    try:
        return parse_harvest_allowlist(raw)
    except HarvestAllowlistError:
        return HarvestAllowlist.default()


__all__ = [
    "HarvestAllowlist",
    "HarvestAllowlistEntry",
    "HarvestAllowlistError",
    "HarvestAuthorization",
    "load_harvest_allowlist",
    "parse_harvest_allowlist",
]
