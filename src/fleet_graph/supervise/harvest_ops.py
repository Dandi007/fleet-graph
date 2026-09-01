"""harvest 机械操作层：git 与部署命令的默认实现（被编排层调用）。

编排层（`supervise/harvest.py`）只调用 `HarvestOps` 协议方法；这里的
`DefaultHarvestOps` 是默认实现，负责真实的 git fetch / cherry 判重 / worktree
cherry-pick / 全量套件 / PR squash merge / ff-only pull / 部署 / 真机 verify。
测试注入 fake，绝不触碰真实网络或生产主 checkout。

git 一律走 `dd/git.py` 的守卫 argv（`core.fsmonitor=false` / `hooksPath=/dev/null`
/ `protocol.ext.allow=never`），与仓库其余部分的隔离纪律一致。

本模块只执行被 allowlist 圈定的目标（编排层 gate 判定之后才调用到这里）；
它自己不判断 allowlist——判定是编排层的写门，这里是门的另一侧（生成-验证分离）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fleet_graph.dd.control_plane import RECORD_FILE
from fleet_graph.dd.git import run_git

#: 命令超时（秒）。收割的 verify/deploy 都是全量级操作，给足预算。
COMMAND_TIMEOUT_SECONDS = 3600

#: 机械操作的合成退出码（shell 惯例）。
EXIT_TIMEOUT = 124
EXIT_NOT_FOUND = 127
#: verify_real 的 HEAD 断言失败合成退出码：拒绝在陈旧树上报绿。
EXIT_HEAD_MISMATCH = 3


def _run(argv: list[str], cwd: Path | None = None) -> dict[str, Any]:
    """执行一条机械命令并返回 {ok, exit_code, stdout_tail, stderr_tail}。"""
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
        }
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "exit_code": EXIT_NOT_FOUND,
            "stdout_tail": "",
            "stderr_tail": f"command not found: {exc}",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "exit_code": EXIT_TIMEOUT,
            "stdout_tail": "",
            "stderr_tail": f"timed out after {COMMAND_TIMEOUT_SECONDS}s",
        }


def _dd_ref(development_id: str) -> str:
    """一个开发的 durable 集成 ref（与 dd/control_plane 同规则）。"""
    return f"refs/heads/dd/{development_id}"


def _origin_url(repo: Path) -> str | None:
    """repo 的 origin remote url；无 origin -> None（真实 forge 前置条件）。"""
    proc = run_git(repo, "remote", "get-url", "origin")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _gh_pr_url(stdout: str) -> str | None:
    """`gh pr create` stdout 里 parse PR html url。

    `gh` 无 shell（argv 数组 subprocess），stdout 首行即 `https://github.com/…/pull/N`。
    首行非空即取首行，绝不猜别的来源。
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        return line
    return None


def _gh_pr_number(pr_url: str) -> str | None:
    """PR html url 尾段数字作为 `gh pr merge` 的编号；解析不到 -> None。"""
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else None


def _resolved(path: Path) -> Path:
    """规范化绝对路径（用于 allowlist 命中判定；不用于返回授权对象）。"""
    return Path(path).expanduser().resolve()


def _resolve_canonical_repo(
    record_repo_path: str,
    record_remote_url: str | None,
    allowlist_repo_paths: list[str],
) -> tuple[Path | None, str]:
    """见 `DefaultHarvestOps.resolve_canonical_repo` 的 docstring。

    独立为模块级函数便于直接单测解析逻辑本身（真 git / 合成仓皆可）。
    """
    allowlist_by_resolved = {_resolved(p): p for p in allowlist_repo_paths}
    record = Path(record_repo_path)
    record_resolved = _resolved(record)

    # 1. 直接命中：record repo_path 本身就是白名单里的 canonical。
    if record_resolved.is_dir() and record_resolved in allowlist_by_resolved:
        return Path(allowlist_by_resolved[record_resolved]), ""

    # 2. linked worktree 归属：common-dir 指向 <canonical>/.git。
    common = run_git(record, "rev-parse", "--git-common-dir")
    if common.returncode == 0 and common.stdout.strip():
        common_dir = Path(common.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = record / common_dir
        common_resolved = _resolved(common_dir)
        if common_resolved.name == ".git" and common_resolved != (record / ".git").resolve():
            canonical = common_resolved.parent
            if canonical.is_dir() and canonical in allowlist_by_resolved:
                return Path(allowlist_by_resolved[canonical]), ""

    # 3. origin 本地路径 / 4. origin URL 映射。
    origin = record_remote_url
    if not origin:
        origin_proc = run_git(record, "remote", "get-url", "origin")
        if origin_proc.returncode == 0 and origin_proc.stdout.strip():
            origin = origin_proc.stdout.strip()
    if origin:
        if Path(origin).is_absolute() and Path(origin).is_dir():
            origin_resolved = _resolved(origin)
            if origin_resolved in allowlist_by_resolved:
                return Path(allowlist_by_resolved[origin_resolved]), ""
        else:
            for entry_path in allowlist_repo_paths:
                entry = Path(entry_path)
                if not entry.is_dir():
                    continue
                proc = run_git(entry, "remote", "get-url", "origin")
                if proc.returncode == 0 and proc.stdout.strip() == origin:
                    return entry, ""

    return None, f"record repo_path {record_repo_path!r} 无法解析到任何白名单 canonical 仓"


class DefaultHarvestOps:
    """真实 git/部署操作的默认实现。测试一律注入 fake。"""

    def resolve_canonical_repo(
        self,
        record_repo_path: str,
        record_remote_url: str | None,
        allowlist_repo_paths: list[str],
    ) -> tuple[Path | None, str]:
        """从 record repo_path 解析 canonical 目标仓绝对路径（机械判定，读口）。

        dd 准入 record 的 `repo_path` 是**每单一次的 linked worktree**
        （`/data/worktrees/...`），而收割写白名单按 **canonical 仓**（如
        `/data/code/self/fleet-harvest-sandbox`）签发——直接拿 worktree 路径授权
        恒 deny（真机回执 granted=false，本 spec 根因）。这里把 record repo_path
        解析成命中白名单的 canonical 主 checkout，授权与全部写步才作用其上。

        解析顺序（优先级从高到低，任一命中即返回，全程机械判定）：

        1. **直接命中**：record_repo_path（规范化绝对路径）本身是目录且等于某
           allowlist 条目的 repo_path → canonical = 它（保留「record 已指向
           canonical」的既有正确行为）。
        2. **linked worktree 归属**：`git -C <record_repo_path> rev-parse
           --git-common-dir` 若不等 `<record_repo_path>/.git`（即该路径是被
           canonical 仓注册的 linked worktree），则 common-dir 指向
           `<canonical>/.git`，剥尾段 `.git` 得 canonical 主 checkout；若该目录
           存在且命中 allowlist → canonical = 它。
        3. **origin 本地路径**：record_remote_url（或缺失时 `git -C
           <record_repo_path> remote get-url origin`）是本地绝对路径且该目录命中
           allowlist → canonical = 它。
        4. **origin URL 映射**：record_remote_url 是 forge URL → 对 allowlist 每个
           条目的 repo_path（目录存在）读其 `git remote get-url origin`，精确
           字符串匹配命中者 → canonical = 该条目的 repo_path。

        解析不到可命中 allowlist 的 canonical → `(None, 机器可读留痕理由)`。绝不
        静默放行、绝不 fallback 到 record repo_path（worktree 路径）本身去授权。
        """
        return _resolve_canonical_repo(record_repo_path, record_remote_url, allowlist_repo_paths)

    def fetch_dd_ref(self, repo: Path, development_id: str) -> dict[str, Any]:
        ref = _dd_ref(development_id)
        proc = run_git(repo, "fetch", "origin", ref)
        if proc.returncode != 0:
            return {"ok": False, "detail": (proc.stderr or proc.stdout).strip()[:400]}
        return {"ok": True, "ref": ref}

    def cherry_equivalent(self, repo: Path, head_commit: str, default_branch: str) -> bool:
        """产品 commit 是否已 cherry 等价进默认分支。

        `git cherry <default_branch> <head_commit>` 会列出尚未合入的 commit，
        前缀 `-` 表示等价、`+` 表示未合入。若 head_commit 不在列（已等价），
        输出为空或没有 `+` 行。任何 git 失败都按 False 处理（保守：宁可重复
        判重，绝不漏判「已收割」而重复写）。
        """
        if not head_commit:
            return False
        proc = run_git(repo, "cherry", default_branch, head_commit)
        if proc.returncode != 0:
            return False
        return all(not line.startswith("+ ") for line in proc.stdout.splitlines())

    def worktree_cherry_pick(
        self,
        repo: Path,
        head_commit: str,
        default_branch: str,
        worktree_root: Path,
    ) -> dict[str, Any]:
        """独立 worktree 上 cherry-pick 产品 commit。

        一次性 detached worktree，冲突即兴消解：先尝试直接 cherry-pick，若因
        冲突失败则尝试 `-X theirs`（以默认分支为主）重跑；仍失败则如实报告
        冲突，绝不强行覆盖。

        **worktree 生命周期（rc-702098ab 回归）**：成功路径保留 worktree，供
        下一步 `run_verify` 在真实目录上跑全量套件（若 here 提前删除目录，
        subprocess 必然 FileNotFoundError -> 127，verify 永远不能 0 退出）；
        移除由编排层在 verify 之后的 `cleanup_worktree` 步骤调用
        `remove_worktree` 统一负责。失败/冲突路径无可验证内容，在此立即清理，
        不留给编排层。
        """
        if worktree_root.exists():
            shutil.rmtree(worktree_root, ignore_errors=True)
        worktree_root.mkdir(parents=True, exist_ok=True)

        added = run_git(repo, "worktree", "add", "--detach", str(worktree_root), default_branch)
        if added.returncode != 0:
            return {
                "ok": False,
                "detail": (added.stderr or added.stdout).strip()[:400],
            }
        picked = run_git(worktree_root, "cherry-pick", head_commit)
        if picked.returncode == 0:
            return {"ok": True, "method": "cherry-pick"}
        if (
            picked.returncode != 0
            and b"conflict" not in (picked.stderr + picked.stdout).encode("utf-8").lower()
        ):
            self.remove_worktree(repo, worktree_root)
            return {
                "ok": False,
                "detail": (picked.stderr or picked.stdout).strip()[:400],
            }
        retried = run_git(worktree_root, "cherry-pick", "-X", "theirs", head_commit)
        if retried.returncode == 0:
            return {"ok": True, "method": "cherry-pick -X theirs"}
        self.remove_worktree(repo, worktree_root)
        return {
            "ok": False,
            "conflicts": True,
            "detail": (retried.stderr or retried.stdout).strip()[:400],
        }

    def remove_worktree(self, repo: Path, worktree_root: Path) -> dict[str, Any]:
        """移除 verify 之后的一次性 detached worktree（编排层 cleanup 步骤调用）。

        `git worktree remove` 失败时兜底：删目录 + `git worktree prune` 清注册。
        """
        removed = run_git(repo, "worktree", "remove", "--force", str(worktree_root))
        if removed.returncode != 0:
            shutil.rmtree(worktree_root, ignore_errors=True)
            run_git(repo, "worktree", "prune")
        return {"ok": True}

    def run_verify(self, worktree: Path, argv: list[str]) -> int:
        return int(_run(list(argv), cwd=worktree).get("exit_code") or 0)

    def board_card_entity_id(self, development_id: str, dd_root: Path) -> str | None:
        """dd 准入 record 的 goal-line board card 实体 id；空/null/缺失/坏档 -> None。

        读 `<dd_root>/<development_id>/record.json` 的 `card_entity_id`
        （`control_plane._publish_card` 持久化；`harvest.py::_resolve_repo` 已读
        同文件，复用其读取模式）。尚无卡时字段为 null/缺失，必须如实返回 None
        （evidence 步 best-effort skip），绝不把 development_id 当 ref 伪造。
        """
        record_path = Path(dd_root) / development_id / RECORD_FILE
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        value = record.get("card_entity_id")
        if value is None:
            return None
        text = str(value)
        return text or None

    def pr_squash_merge(
        self,
        repo: Path,
        development_id: str,
        head_commit: str,
        default_branch: str,
    ) -> dict[str, Any]:
        """PR -> squash merge（真实远端 forge，绝不本地伪装合并）。

        流水线：推 `harvest/<development_id>` 分支 -> `gh pr create` -> `gh pr
        merge --squash --delete-branch`，返回 merged PR 的 html url。git 子调用
        沿用 `dd/git.py` 守卫纪律（`run_git`）；`gh` 不经 git 守卫浸泡，独立
        argv 数组 subprocess（无 shell），cwd=repo。

        任一步非零退出 / 缺 gh / 缺 origin -> merged=False + detail（stderr/stdout
        tail[:400]），绝不降级回本地 `git merge --squash`、绝不直接 commit 目标
        repo 生产 checkout 默认分支。
        """
        if not head_commit:
            return {"merged": False, "detail": "无 head_commit 可合"}
        if not development_id:
            return {"merged": False, "detail": "无 development_id——无法命名 harvest 分支"}

        origin = _origin_url(repo)
        if origin is None:
            return {"merged": False, "detail": "缺 origin remote——无法 forge PR"}

        branch = f"harvest/{development_id}"
        pushed = run_git(repo, "push", "origin", f"{head_commit}:refs/heads/{branch}")
        if pushed.returncode != 0:
            return {"merged": False, "detail": (pushed.stderr or pushed.stdout).strip()[:400]}

        created = _run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                origin,
                "--base",
                default_branch,
                "--head",
                branch,
                "--title",
                f"harvest: {development_id} {head_commit[:12]}",
                "--body",
                f"harvest reactor merge of {head_commit} for {development_id}",
            ],
            cwd=repo,
        )
        if not created.get("ok"):
            return {
                "merged": False,
                "detail": (created.get("stderr_tail") or created.get("stdout_tail"))[:400],
            }
        pr_url = _gh_pr_url(str(created.get("stdout_tail") or ""))
        if not pr_url:
            return {
                "merged": False,
                "detail": "gh pr create 未产出 PR 链接: "
                + (created.get("stderr_tail") or created.get("stdout_tail"))[:400],
            }
        number = _gh_pr_number(pr_url)
        if not number:
            return {"merged": False, "detail": f"无法从 PR 链接解析编号: {pr_url}"}

        merged = _run(
            ["gh", "pr", "merge", number, "--squash", "--delete-branch"],
            cwd=repo,
        )
        if not merged.get("ok"):
            return {
                "merged": False,
                "detail": (merged.get("stderr_tail") or merged.get("stdout_tail"))[:400],
            }
        return {"merged": True, "pr_url": pr_url, "method": "gh-pr-squash-merge"}

    def ff_only_pull(self, repo: Path, default_branch: str) -> dict[str, Any]:
        """ff-only pull 默认分支；成功时额外返回 pull 后的 HEAD（字段名 `head`）。

        `head` 是「已合并 commit」的唯一机械来源（pull 成功后在 canonical 仓读
        `rev-parse HEAD`），绝不猜、不另造。失败时 `head` 为 `None`，保留既有
        `ok:false` + `detail`。
        """
        proc = run_git(repo, "pull", "--ff-only", "origin", default_branch)
        if proc.returncode != 0:
            return {
                "ok": False,
                "head": None,
                "detail": (proc.stderr or proc.stdout).strip()[:400],
            }
        head_proc = run_git(repo, "rev-parse", "HEAD")
        if head_proc.returncode != 0:
            return {
                "ok": False,
                "head": None,
                "detail": "pull 成功后无法读取 HEAD: "
                + (head_proc.stderr or head_proc.stdout).strip()[:400],
            }
        return {"ok": True, "head": head_proc.stdout.strip()}

    def deploy(self, command: list[str], repo: Path) -> int:
        """在 canonical 仓绝对路径 cwd 下执行部署命令（缺 cwd 是 127 假绿根因）。"""
        return int(_run(list(command), cwd=repo).get("exit_code") or 0)

    def verify_real(self, argv: list[str], repo: Path, expected_head: str | None) -> int:
        """真机 verify，先在 canonical 仓 cwd 断言 HEAD == 已合并 commit。

        `expected_head is None`（pull 未成功、未捕获到已合并 commit）或当前
        HEAD != `expected_head` 时**不执行** verify 命令，返回合成非零退出码
        `EXIT_HEAD_MISMATCH`——拒绝在陈旧树上报绿。相等时才在 canonical 仓
        cwd 下跑 verify。
        """
        head_proc = run_git(repo, "rev-parse", "HEAD")
        current_head = head_proc.stdout.strip() if head_proc.returncode == 0 else None
        if expected_head is None or current_head != expected_head:
            return EXIT_HEAD_MISMATCH
        return int(_run(list(argv), cwd=repo).get("exit_code") or 0)


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "EXIT_HEAD_MISMATCH",
    "EXIT_NOT_FOUND",
    "EXIT_TIMEOUT",
    "DefaultHarvestOps",
]
