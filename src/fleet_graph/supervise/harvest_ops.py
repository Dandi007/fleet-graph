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


class DefaultHarvestOps:
    """真实 git/部署操作的默认实现。测试一律注入 fake。"""

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
        proc = run_git(repo, "pull", "--ff-only", "origin", default_branch)
        if proc.returncode != 0:
            return {"ok": False, "detail": (proc.stderr or proc.stdout).strip()[:400]}
        return {"ok": True}

    def deploy(self, command: list[str]) -> int:
        return int(_run(list(command)).get("exit_code") or 0)

    def verify_real(self, argv: list[str]) -> int:
        return int(_run(list(argv)).get("exit_code") or 0)


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "EXIT_NOT_FOUND",
    "EXIT_TIMEOUT",
    "DefaultHarvestOps",
]
