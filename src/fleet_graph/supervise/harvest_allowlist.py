"""收割写权限白名单（M3 交付 A + 扩围安全判据：谁能进 / 进之前要验什么）。

参照 `supervise/preauth.py` 的语义：独立主体 + 机械判定 + 默认拒绝。与 preauth
面向「放行 decision」不同，这里面向「harvest 子图的写动作」——收割要把产品
commit 落进默认分支、跑部署脚本，这些写动作在 M3 里**唯一**的正当性来源就是
命中本白名单条目。

铁律（与 spec 逐条对应）：

- **独立配置**：字段含 `repo_path`（可写仓库绝对路径）+ `allowed_branches`
  （允许写入的分支/ref 前缀列表，前缀语义，不用正则）+ `allowed_deploy`
  （允许执行的部署脚本/命令 argv 列表，精确 argv 匹配）。
- **越界写拒绝并留痕**：写目标不在白名单 -> `granted=False` + 机器可读 reasons，
  调用方（harvest 子图的写步骤）据此拒绝执行并把 reasons 记进 receipt/evidence，
  绝不静默放行。
- **默认 deny-all**：空 allowlist（未合入、配置文件缺失、解析失败）不授予任何
  写权限——harvest 子图在 allowlist 合入之前结构性没有写能力。

扩围安全判据（P0 卡自动收割，三问机械答案）：

- **谁能进（资格）**：仅监督面亲签条目有资格；目标仓必须位于受治代码根
  （`DEFAULT_GOVERNED_ROOT` 物理前缀）之内且是「干净、真实的 git worktree、
  路径即 top-level」；fleet-graph 自身产品源（本线 self）永不在列（自写禁止）。
- **进之前要验什么（机械核验，任一失败即 deny）**：
  ① repo_path 绝对路径 + 存在 + `git rev-parse --is-inside-work-tree` 为真 +
  `--show-toplevel` 等于 repo_path（防子目录/符号替换）；② 仓库无未提交改动
  （干净 worktree）；③ 默认分支（HEAD symbolic-ref 解析出的 ref）必须被
  `allowed_branches` 的某个全-ref 前缀覆盖；④ allowed_branches 每项是合法
  ref 字符集、allowed_deploy 每项是非空精确 argv（已有）；⑤ 条目（或文件顶层
  签发块）必须携带机器可读的签发出处 `signed_by` 与期限 `expires_at`，过期或
  缺出处即 deny；⑥ 任一核验失败 -> `granted=False` + reasons（留痕），绝不
  部分放行、绝不静默。

实现分两层：`authorize` 保持纯配置判定（既有 M3 语义、零回归）；`authorize_repo`
在配置判定之上叠加全部机械核验（真实 git 探测 + 受治根 + 自写禁止 + 签发/期限），
是生产侧应使用的完整写门。本模块仍不发布/不部署任何东西。
"""

from __future__ import annotations

import functools
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fleet_graph.dd.git import git_argv
from fleet_graph.dd.vendor import git_ops

#: 分支/ref 前缀允许出现的合法字符集——与 preauth 的 scope 纪律一致：前缀读得出来，
#: 不用正则、不接受通配。
_ALLOWED_PREFIX_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-"
)

#: 受治代码根（物理前缀）：目标仓必须位于其内才可进入白名单。签发数据文件里
#: 的条目不能给受治根外的仓发通行证——越界仓一律 deny。
DEFAULT_GOVERNED_ROOT = "/data/code/self"


class HarvestAllowlistError(ValueError):
    """配置不合法。拒收，不猜——坏配置按无写权限对待。"""


@dataclass(frozen=True)
class HarvestAllowlistEntry:
    """一条可写目标：哪个仓库、哪些分支/ref 前缀、哪些部署命令、签发与期限。"""

    repo_path: str
    allowed_branches: tuple[str, ...]
    allowed_deploy: tuple[tuple[str, ...], ...]
    signed_by: str | None = None
    expires_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "repo_path": self.repo_path,
            "allowed_branches": list(self.allowed_branches),
            "allowed_deploy": [list(argv) for argv in self.allowed_deploy],
        }
        if self.signed_by is not None:
            out["signed_by"] = self.signed_by
        if self.expires_at is not None:
            out["expires_at"] = self.expires_at
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

    signed_by = raw.get("signed_by")
    if signed_by is not None and not isinstance(signed_by, str):
        raise HarvestAllowlistError(f"{repo_path}: signed_by 必须是字符串")
    expires_at = raw.get("expires_at")
    if expires_at is not None and not isinstance(expires_at, str):
        raise HarvestAllowlistError(f"{repo_path}: expires_at 必须是字符串")

    return HarvestAllowlistEntry(
        repo_path=repo_path,
        allowed_branches=tuple(branches),
        allowed_deploy=tuple(deploy),
        signed_by=signed_by,
        expires_at=expires_at,
    )


def _parse_expiry(value: str) -> float:
    """期限的 epoch 秒。必须是带时区的 RFC3339——naive 时间在跨主机比较里就是
    歧义，拒收（与 preauth 的 expires_at 同一纪律）。"""
    if not value.strip():
        raise HarvestAllowlistError("expires_at 必填：无期限条目是常开通行证，拒收")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HarvestAllowlistError(f"expires_at {value!r} 不是合法 RFC3339 时间: {exc}") from exc
    if parsed.tzinfo is None:
        raise HarvestAllowlistError(f"expires_at {value!r} 缺时区——期限判定不接受歧义时间")
    return parsed.timestamp()


@dataclass(frozen=True)
class HarvestAllowlist:
    """收割写白名单。默认 deny-all：空 entries 不授予任何写权限。

    `signed_by` / `expires_at` 是文件顶层签发块：当某条目自身不带签发字段时
    回落到顶层块；条目显式携带的字段优先。
    """

    entries: tuple[HarvestAllowlistEntry, ...] = ()
    signed_by: str | None = None
    expires_at: str | None = None

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
        """纯配置判定：一次写动作是否命中白名单（M3 语义，零回归）。

        deny-all 默认；命不中即拒绝+留痕。不触碰真实 git——机械核验在
        `authorize_repo`。
        """
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

    def authorize_repo(
        self,
        *,
        repo_path: str,
        branch: str,
        deploy: tuple[str, ...] = (),
        governed_root: str = DEFAULT_GOVERNED_ROOT,
        now: float | None = None,
    ) -> HarvestAuthorization:
        """完整机械写门：配置判定 + 受治根 + 自写禁止 + 真实 git 核验 + 签发/期限。

        任一核验失败 -> `granted=False` + 机器可读 reasons（留痕），绝不部分放行、
        绝不静默。`governed_root` 默认受治代码根，`now` 仅测试注入（默认现时
        UTC 时间戳）。
        """
        reasons = list(self.authorize(repo_path=repo_path, branch=branch, deploy=deploy).reasons)
        entry = self.entry_for(repo_path)
        if entry is None:
            return HarvestAuthorization(granted=False, reasons=tuple(reasons))

        real_repo = os.path.realpath(repo_path)
        root = os.path.realpath(governed_root)
        if not real_repo.startswith(root.rstrip(os.sep) + os.sep):
            reasons.append(
                f"repo_path {repo_path!r} 不在受治代码根 {governed_root!r} 之内——越界仓一律 deny"
            )

        self_root = _module_repo_root()
        if self_root is not None and real_repo == self_root:
            reasons.append("fleet-graph 自身产品源（本线 self）永不在列——自写禁止")

        probe = _probe_repo(repo_path)
        if not probe.exists:
            reasons.append(f"repo_path {repo_path!r} 不存在或不可读")
        elif not probe.inside_work_tree:
            reasons.append(
                f"repo_path {repo_path!r} 不是真实 git worktree（--is-inside-work-tree 非真）"
            )
        else:
            if probe.top_level is None or os.path.realpath(probe.top_level) != real_repo:
                reasons.append(
                    f"repo_path {repo_path!r} 不是 top-level git worktree"
                    "（--show-toplevel ≠ repo_path——子目录或符号替换）"
                )
            if not probe.clean:
                reasons.append(f"repo_path {repo_path!r} worktree 不干净（有未提交改动）")
            if probe.head_ref is None:
                reasons.append(
                    f"repo_path {repo_path!r} 无法解析默认分支（HEAD symbolic-ref 失败，detached?）"
                )
            elif not any(probe.head_ref.startswith(p) for p in entry.allowed_branches):
                reasons.append(
                    f"默认分支 {probe.head_ref!r} 不被 allowed_branches "
                    f"{list(entry.allowed_branches)} 覆盖"
                )

        signed_by = entry.signed_by if entry.signed_by is not None else self.signed_by
        expires_at = entry.expires_at if entry.expires_at is not None else self.expires_at
        if not signed_by or not signed_by.strip():
            reasons.append(
                "条目缺机器可读的签发出处（signed_by 缺失/为空）——拒签发来源不明的通行证"
            )
        if not expires_at or not expires_at.strip():
            reasons.append("条目缺期限（expires_at 缺失/为空）——无期限通行证拒绝")
        else:
            try:
                expires = _parse_expiry(expires_at)
            except HarvestAllowlistError as exc:
                reasons.append(f"期限不可读：{exc}")
            else:
                now_ts = datetime.now(UTC).timestamp() if now is None else now
                if now_ts >= expires:
                    reasons.append(f"条目已过期（expires_at={expires_at}）")

        return HarvestAuthorization(granted=not reasons, reasons=tuple(reasons))


def _run_git(repo_path: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    """只读 git 探测：`git -C <repo> args`，失败返回 None（不抛、不写）。

    所有 git 调用统一经 `fleet_graph.dd.git.git_argv`（带 fsmonitor/hooksPath/
    protocol.ext 三道守卫）构造 argv，绝不出现裸 git argv——守卫的来由与回归
    测试见 `tests/test_dd_git.py`。
    """
    env = dict(git_ops.safe_git_environment())
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            git_argv(repo_path, *args),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass(frozen=True)
class _RepoProbe:
    """一次 repo_path 的真实 git 探测结果。"""

    exists: bool = False
    inside_work_tree: bool = False
    top_level: str | None = None
    clean: bool = False
    head_ref: str | None = None


def _probe_repo(repo_path: str) -> _RepoProbe:
    if not os.path.isdir(repo_path):
        return _RepoProbe(exists=False)
    inside = _run_git(repo_path, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
        return _RepoProbe(exists=True, inside_work_tree=False)
    top = _run_git(repo_path, "rev-parse", "--show-toplevel")
    status = _run_git(repo_path, "status", "--porcelain")
    head = _run_git(repo_path, "symbolic-ref", "HEAD")
    return _RepoProbe(
        exists=True,
        inside_work_tree=True,
        top_level=(top.stdout.strip() if top is not None and top.returncode == 0 else None),
        clean=(status is not None and status.returncode == 0 and status.stdout.strip() == ""),
        head_ref=(head.stdout.strip() if head is not None and head.returncode == 0 else None),
    )


@functools.lru_cache(maxsize=1)
def _module_repo_root() -> str | None:
    """本模块所在仓库的 git toplevel（规范路径）；解析不出则 None（自写禁止
    检查按不可判定略过，受治根仍是主闸）。"""
    module_dir = os.path.dirname(os.path.abspath(__file__))
    proc = _run_git(module_dir, "rev-parse", "--show-toplevel")
    if proc is None or proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return os.path.realpath(top) if top else None


def parse_harvest_allowlist(raw: Any) -> HarvestAllowlist:
    """从已解析的 JSON 对象构建白名单；任何不合法处抛 HarvestAllowlistError。"""
    if not isinstance(raw, dict):
        raise HarvestAllowlistError(f"白名单顶层必须是 JSON 对象，got {type(raw).__name__}")
    entries_raw = raw.get("entries", [])
    if not isinstance(entries_raw, list):
        raise HarvestAllowlistError("entries 必须是列表")
    signed_by = raw.get("signed_by")
    if signed_by is not None and not isinstance(signed_by, str):
        raise HarvestAllowlistError("顶层 signed_by 必须是字符串")
    expires_at = raw.get("expires_at")
    if expires_at is not None and not isinstance(expires_at, str):
        raise HarvestAllowlistError("顶层 expires_at 必须是字符串")
    return HarvestAllowlist(
        entries=tuple(_parse_entry(e) for e in entries_raw),
        signed_by=signed_by,
        expires_at=expires_at,
    )


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
    "DEFAULT_GOVERNED_ROOT",
    "HarvestAllowlist",
    "HarvestAllowlistEntry",
    "HarvestAllowlistError",
    "HarvestAuthorization",
    "load_harvest_allowlist",
    "parse_harvest_allowlist",
]
