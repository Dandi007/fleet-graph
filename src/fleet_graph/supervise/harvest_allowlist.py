"""收割写权限白名单（M3 交付 A：allowlist 先行）。

参照 `supervise/preauth.py` 的语义：独立主体 + 机械判定 + 默认拒绝。与 preauth
面向「放行 decision」不同，这里面向「harvest 子图的写动作」——收割要把产品
commit 落进默认分支、跑部署脚本，这些写动作在 M3 里**唯一**的正当性来源就是
命中本白名单条目。

三条铁律（与 spec 逐条对应）：

- **独立配置**：字段含 `repo_path`（可写仓库绝对路径）+ `allowed_branches`
  （允许写入的分支/ref 前缀列表，前缀语义，不用正则）+ `allowed_deploy`
  （允许执行的部署脚本/命令 argv 列表，精确 argv 匹配）。
- **越界写拒绝并留痕**：写目标不在白名单 -> `granted=False` + 机器可读 reasons，
  调用方（harvest 子图的写步骤）据此拒绝执行并把 reasons 记进 receipt/evidence，
  绝不静默放行。
- **默认 deny-all**：空 allowlist（未合入、配置文件缺失、解析失败）不授予任何
  写权限——harvest 子图在 allowlist 合入之前结构性没有写能力。

本模块是纯函数：只做配置解析、校验与判定，不执行任何 git/部署命令，不发布
任何东西（与 preauth.py 同款纪律）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

    逐仓生效值（案 A 改写任务 1）：可选 `default_branch` 与 `deploy_command`
    （缺省 None）。命中条目后该单的生效 default_branch / deploy_command 取自
    条目；条目未指定时退回全局缺省。向后兼容：现有 JSON 条目无这两个字段时
    按旧全局行为解析（不新增配置面、不改签 allowlist）。
    """

    repo_path: str
    allowed_branches: tuple[str, ...]
    allowed_deploy: tuple[tuple[str, ...], ...]
    default_branch: str | None = None
    deploy_command: tuple[str, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "repo_path": self.repo_path,
            "allowed_branches": list(self.allowed_branches),
            "allowed_deploy": [list(argv) for argv in self.allowed_deploy],
        }
        if self.default_branch is not None:
            out["default_branch"] = self.default_branch
        if self.deploy_command is not None:
            out["deploy_command"] = list(self.deploy_command)
        return out


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

    branches_raw = raw.get("allowed_branches")
    if not isinstance(branches_raw, list) or not branches_raw:
        raise HarvestAllowlistError(f"{repo_path}: allowed_branches 必须是非空列表")
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

    default_branch = raw.get("default_branch")
    if default_branch is not None and (
        not isinstance(default_branch, str) or not default_branch.strip()
    ):
        raise HarvestAllowlistError(
            f"{repo_path}: default_branch 必须是非空字符串（缺省 None 走全局）"
        )

    deploy_command_raw = raw.get("deploy_command")
    deploy_command: tuple[str, ...] | None = None
    if deploy_command_raw is not None:
        if (
            not isinstance(deploy_command_raw, list)
            or not deploy_command_raw
            or not all(isinstance(part, str) and part for part in deploy_command_raw)
        ):
            raise HarvestAllowlistError(
                f"{repo_path}: deploy_command 必须是非空字符串 argv 列表（缺省 None 走全局）"
            )
        deploy_command = tuple(deploy_command_raw)

    return HarvestAllowlistEntry(
        repo_path=repo_path,
        allowed_branches=tuple(branches),
        allowed_deploy=tuple(deploy),
        default_branch=default_branch,
        deploy_command=deploy_command,
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

    def effective_default_branch(self, repo_path: str, global_default_branch: str) -> str:
        """命中条目且条目指定 default_branch -> 取条目值；否则退回全局缺省。"""
        entry = self.entry_for(repo_path)
        if entry is not None and entry.default_branch is not None:
            return entry.default_branch
        return global_default_branch

    def effective_deploy_command(
        self, repo_path: str, global_deploy_command: list[str]
    ) -> list[str]:
        """命中条目后的生效 deploy 命令（任务 2：只授合并权入围形态）。

        - 条目 `allowed_deploy` 为空（merge-only）-> 生效 deploy 命令恒为空，
          走 `deploy` 节点既有 no-op 路径（deploy_exit_code == 0）；
        - 条目声明 `deploy_command` -> 生效命令取条目声明（声明与白名单不符由
          gate 拒：`authorize` 的 deploy 校验仍按 `allowed_deploy` 精确匹配）；
        - 条目未声明 -> 退回全局缺省。
        """
        entry = self.entry_for(repo_path)
        if entry is None:
            return list(global_deploy_command)
        if not entry.allowed_deploy:
            return []
        if entry.deploy_command is not None:
            return list(entry.deploy_command)
        return list(global_deploy_command)

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
