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

from fleet_graph.dd.control_plane import RECORD_FILE, STATUS_FILE
from fleet_graph.dd.git import run_git
from fleet_graph.dd.vendor import git_ops

#: 命令超时（秒）。收割的 verify/deploy 都是全量级操作，给足预算。
COMMAND_TIMEOUT_SECONDS = 3600

#: 机械操作的合成退出码（shell 惯例）。
EXIT_TIMEOUT = 124
EXIT_NOT_FOUND = 127
#: verify_real 的 HEAD 断言失败合成退出码：拒绝在陈旧树上报绿。
EXIT_HEAD_MISMATCH = 3

#: 收割时必须从产品树里剔除的两棵顶层 dd 协议子树。绝不全局 gitignore——
#: dd 协议要求工单分支继续提交这些文件，排除只发生在收割侧、按顶层路径精确作用。
DD_EXCLUDED_PATHS = (".dev-dispatch", ".dd-evidence")

#: 洗树重提交的 commit message。
DD_WASH_COMMIT_MESSAGE = "harvest: exclude dd protocol subtrees from product tree"

#: 目标仓 verify 指令解析（交付 A.1）：根目录 Makefile 含 `verify` 目标 -> make verify。
MAKE_VERIFY_ARGV = ["make", "verify"]
#: 无 Makefile 但由 uv 管理的仓（pyproject.toml / uv.lock）-> repo-canonical 全量套件。
UV_PYTEST_ARGV = ["uv", "run", "pytest", "-q"]
#: 解析不到可执行 verify 指令时的机器可读 detail（交付 A.2）。
NO_RESOLVABLE_VERIFY = "no resolvable verify command"


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


def _commit_env() -> dict[str, str]:
    """洗树重提交的隔离环境：dd/git.py 守卫环境 + 固定收割身份。"""
    env = git_ops.safe_git_environment()
    env.update(
        {
            "GIT_AUTHOR_NAME": "fleet-graph harvest",
            "GIT_AUTHOR_EMAIL": "harvest@fleet-graph.local",
            "GIT_COMMITTER_NAME": "fleet-graph harvest",
            "GIT_COMMITTER_EMAIL": "harvest@fleet-graph.local",
        }
    )
    return env


def _strip_dd_subtrees(worktree: Path) -> dict[str, Any]:
    """机械洗树：从 worktree HEAD 剔除 `.dev-dispatch/` 与 `.dd-evidence/` 两棵
    顶层子树并重提交，返回 `{ok, harvest_tip, detail}`。

    按顶层路径精确绑定并剔除（pathspec `:(exclude).dev-dispatch` /
    `:(exclude).dd-evidence` 的等价机械原语：洗树后重提交），绝不靠全局
    `.gitignore`。只剔这两棵顶层子树，不动任何产品文件、不动其它点前缀目录。
    worktree 若无这两棵子树（普通工单 commit），则洗树是 no-op——harvest_tip
    即 cherry-pick 后原 tip，不叠一层空提交。
    """
    removed = run_git(
        worktree,
        "rm",
        "-r",
        "--ignore-unmatch",
        "--quiet",
        "--",
        *DD_EXCLUDED_PATHS,
    )
    if removed.returncode != 0:
        return {
            "ok": False,
            "detail": (removed.stderr or removed.stdout).strip()[:400],
        }
    # 仅当确有剔除（index 有变更）才重提交；无变更时 tip 就是当前 HEAD。
    staged = run_git(worktree, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        tip = run_git(worktree, "rev-parse", "HEAD")
        if tip.returncode != 0 or not tip.stdout.strip():
            return {"ok": False, "detail": "洗树后无法解析 tip commit"}
        return {"ok": True, "harvest_tip": tip.stdout.strip(), "washed": False}
    if staged.returncode != 1:
        return {
            "ok": False,
            "detail": (staged.stderr or staged.stdout).strip()[:400],
        }
    committed = run_git(worktree, "commit", "-q", "-m", DD_WASH_COMMIT_MESSAGE, env=_commit_env())
    if committed.returncode != 0:
        return {
            "ok": False,
            "detail": (committed.stderr or committed.stdout).strip()[:400],
        }
    tip = run_git(worktree, "rev-parse", "HEAD")
    if tip.returncode != 0 or not tip.stdout.strip():
        return {"ok": False, "detail": "洗树后无法解析 tip commit"}
    return {"ok": True, "harvest_tip": tip.stdout.strip(), "washed": True}


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


def _makefile_has_verify_target(worktree: Path) -> bool:
    """机械判定：目标仓根目录 `Makefile` 是否声明 `verify` 目标。

    只读文件，绝不执行 make。目标声明形如 `verify:` / `verify: deps` /
    `verify::`（首列目标，允许 `: ` 前有空白）；`.PHONY: verify` 也算声明。
    `verify = <value>` 变量赋值不算目标。
    """
    makefile = worktree / "Makefile"
    if not makefile.is_file():
        return False
    try:
        text = makefile.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.lstrip()
        if line.startswith("verify"):
            rest = line[len("verify") :].lstrip()
            if rest.startswith(":"):
                return True
        if line.startswith(".PHONY:"):
            rest = line[len(".PHONY:") :]
            if "verify" in rest.split():
                return True
    return False


def _resolve_verify_argv(worktree: Path) -> tuple[list[str] | None, str]:
    """按目标仓自身声明解析 verify 指令（交付 A.1 机械口）。

    优先目标仓自身声明：
    1. 根目录 `Makefile` 含 `verify` 目标 -> `["make","verify"]`；
    2. 无 Makefile 但存在 `pyproject.toml` / `uv.lock` -> repo-canonical 全量套件
       `["uv","run","pytest","-q"]`（如 fleet-sentinel：pyproject.toml + uv.lock +
       tests/，其全量套件不是 make）；
    3. 解析不到可执行 verify 指令 -> `(None, "no resolvable verify command")`。

    纯机械读口（只读目标仓根目录文件，绝不执行任何命令）；测试注入 fake。
    """
    if _makefile_has_verify_target(worktree):
        return list(MAKE_VERIFY_ARGV), ""
    if (worktree / "pyproject.toml").is_file() or (worktree / "uv.lock").is_file():
        return list(UV_PYTEST_ARGV), ""
    return None, NO_RESOLVABLE_VERIFY


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
        """独立 worktree 上 cherry-pick 产品 commit，并洗掉 dd 协议子树。

        一次性 detached worktree，冲突即兴消解：先尝试直接 cherry-pick，若因
        冲突失败则尝试 `-X theirs`（以默认分支为主）重跑；仍失败则如实报告
        冲突，绝不强行覆盖。

        **H6 清场协议（冲突重试路径）**：首次 cherry-pick 因冲突返回非零时，
        worktree 索引会残留 unmerged files（MERGING 态）；若不清场直接重试
        `-X theirs`，git 恒报 `Cherry-picking is not possible because you
        have unmerged files`——与「首败是否真冲突可解」无关。因此重试前必须先
        `git cherry-pick --abort`；abort 非零则兜底 `git reset --merge` 把索引
        从 MERGING 态恢复干净；清场仍失败则如实返回 ok:false + 机器可读 detail，
        绝不带病继续（不 reset --hard、不动生产主 checkout）。

        cherry-pick 成功后在**同一 worktree** 上做同样的剔除并提交（交付 A.4）：
        按顶层路径剔除 `.dev-dispatch/` 与 `.dd-evidence/` 两棵子树，返回干净
        产品树 tip（`harvest_tip`）——保证随后的 `run_verify` 也跑在干净产品树。

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
            washed = _strip_dd_subtrees(worktree_root)
            if not washed.get("ok"):
                self.remove_worktree(repo, worktree_root)
                return washed
            return {
                "ok": True,
                "method": "cherry-pick",
                "harvest_tip": washed["harvest_tip"],
                "washed": washed["washed"],
            }
        if (
            picked.returncode != 0
            and b"conflict" not in (picked.stderr + picked.stdout).encode("utf-8").lower()
        ):
            self.remove_worktree(repo, worktree_root)
            return {
                "ok": False,
                "detail": (picked.stderr or picked.stdout).strip()[:400],
            }
        # H6：冲突重试路径必须先在原 worktree 上清场（abort / reset --merge），
        # 否则首败残留的 unmerged files 会让 -X theirs 重试恒报
        # "Cherry-picking is not possible because you have unmerged files"。
        # 清场失败则如实 ok:false + 机器可读 detail，绝不带病继续。
        aborted = run_git(worktree_root, "cherry-pick", "--abort")
        if aborted.returncode != 0:
            reset_merged = run_git(worktree_root, "reset", "--merge")
            if reset_merged.returncode != 0:
                self.remove_worktree(repo, worktree_root)
                return {
                    "ok": False,
                    "conflicts": True,
                    "detail": (
                        "冲突清场失败（cherry-pick --abort 与 git reset --merge 均非零）: "
                        + (aborted.stderr or aborted.stdout).strip()[:200]
                        + " / "
                        + (reset_merged.stderr or reset_merged.stdout).strip()[:200]
                    ),
                }
        retried = run_git(worktree_root, "cherry-pick", "-X", "theirs", head_commit)
        if retried.returncode == 0:
            washed = _strip_dd_subtrees(worktree_root)
            if not washed.get("ok"):
                self.remove_worktree(repo, worktree_root)
                return washed
            return {
                "ok": True,
                "method": "cherry-pick -X theirs",
                "harvest_tip": washed["harvest_tip"],
                "washed": washed["washed"],
            }
        self.remove_worktree(repo, worktree_root)
        return {
            "ok": False,
            "conflicts": True,
            "detail": (retried.stderr or retried.stdout).strip()[:400],
        }

    def build_harvest_tip(
        self,
        repo: Path,
        head_commit: str,
        default_branch: str,
        worktree_root: Path,
    ) -> dict[str, Any]:
        """机械写口：从产品 commit 派生一棵「去 dd 协议子树」的干净产品树。

        只做机械事：读 `head_commit`、洗树、产出 tip（交付 A.3，不做 allowlist
        判定——判定在编排层 gate）。在一次性 worktree 上 cherry-pick 后用
        `_strip_dd_subtrees` 剔除 `.dev-dispatch/` / `.dd-evidence/` 两棵顶层
        子树并重提交，返回 `{ok, harvest_tip, detail}`。worktree 用后即清，
        不保留（此口不承担 verify 职责）。
        """
        if worktree_root.exists():
            shutil.rmtree(worktree_root, ignore_errors=True)
        worktree_root.mkdir(parents=True, exist_ok=True)

        added = run_git(repo, "worktree", "add", "--detach", str(worktree_root), default_branch)
        if added.returncode != 0:
            self.remove_worktree(repo, worktree_root)
            return {
                "ok": False,
                "detail": (added.stderr or added.stdout).strip()[:400],
            }
        picked = run_git(worktree_root, "cherry-pick", head_commit)
        if picked.returncode != 0:
            self.remove_worktree(repo, worktree_root)
            return {
                "ok": False,
                "detail": (picked.stderr or picked.stdout).strip()[:400],
            }
        washed = _strip_dd_subtrees(worktree_root)
        self.remove_worktree(repo, worktree_root)
        if not washed.get("ok"):
            return washed
        return {
            "ok": True,
            "harvest_tip": washed["harvest_tip"],
            "washed": washed["washed"],
            "detail": "washed",
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

    def resolve_verify_argv(self, worktree: Path) -> tuple[list[str] | None, str]:
        """目标仓 verify 指令解析（交付 A.1 机械口，测试注入 fake）。

        见模块级 `_resolve_verify_argv`：优先目标仓自身声明（Makefile 含 verify
        目标 / pyproject.toml / uv.lock），解析不到 -> `(None, "no resolvable
        verify command")`。纯读口，绝不执行任何命令。
        """
        return _resolve_verify_argv(worktree)

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

    def _binding_matches(self, tree: Path, record_tree: Path) -> bool:
        """record 的 repo_path（规范化后）是否绑定 tree（规范化后）。

        两种情况都覆盖：
        1. record repo_path 与 tree 直接相等（record 直接指向 canonical / 独立 init）；
        2. record repo_path 是 linked worktree -> `git rev-parse --git-common-dir`
           解析其 canonical，判 canonical 是否等于 tree（worktree -> canonical）。

        只读：`git rev-parse` 是纯读口，零写原语。
        """
        if tree == record_tree:
            return True
        if not record_tree.is_dir():
            return False
        common = run_git(record_tree, "rev-parse", "--git-common-dir")
        if common.returncode != 0 or not common.stdout.strip():
            return False
        common_dir = Path(common.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = record_tree / common_dir
        common_resolved = _resolved(common_dir)
        if common_resolved.name == ".git" and common_resolved != (record_tree / ".git").resolve():
            canonical = _resolved(common_resolved.parent)
            if canonical == tree:
                return True
        return False

    def _terminal_of(self, dev_dir: Path) -> tuple[str, bool]:
        """读 `<dd_root>/<dev>/status.json` 的 terminal 字段；不可读/坏档 -> 按在飞。

        返回 `(terminal, status_ok)`：status_ok=True 表示文件可读且顶层为 JSON 对象
        （此时 terminal 空/缺失/falsy = 在飞）；status_ok=False 表示缺失/坏档
        （fail-closed，调用方按在飞处理）。
        """
        status_path = dev_dir / STATUS_FILE
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "", False
        if not isinstance(status, dict):
            return "", False
        return str(status.get("terminal") or ""), True

    def detect_inflight_binding(
        self,
        tree_path: Path,
        dd_root: Path,
        current_development_id: str | None = None,
    ) -> dict[str, Any]:
        """H8 交付 A：只读 occupancy 判定——`tree_path` 是否被另一张在飞单绑定。

        返回机器可读 `{"bound_development_id": str|None, "in_flight": bool,
        "detail": str}`，语义闭合：

        1. 规范化 `tree_path`（`Path(...).resolve()`，同 `_resolved`）。
        2. 枚举 `<dd_root>/<development_id>/record.json`，读 `repo_path`
           （=worktree 绑定），规范化后与 `tree_path` 比对；不等时若 record
           `repo_path` 是 linked worktree，用 `git rev-parse --git-common-dir`
           解析其 canonical 再判等（覆盖 record 直接指向 canonical 与
           worktree -> canonical 两种情况）。任一条 record 不可读/坏档 ->
           记为「无法判定」，不静默跳过（fail-closed）。
        3. 命中绑定（含自身归属解析命中）时读 `<dd_root>/<id>/status.json` 的
           `terminal` 字段：空/缺失/falsy -> `in_flight=true`；非空 -> 终态
           `in_flight=false`。status.json 缺失/不可读 -> 保守按 `in_flight=true`
           （fail-closed）。
        4. 命中且在飞 -> 返回 `{"bound_development_id": <该id>, "in_flight":
           True, "detail": ...}`。没有任何在飞单绑定（含只被终态单绑定）->
           `{"bound_development_id": None, "in_flight": False, "detail": ""}`。
        5. **本方法只读**：零 `rmtree`/`worktree remove`/`reset`/`checkout`/
           `clean`，绝不写/建/登记任何文件——所有输入都来自既有
           `record.json`/`status.json`/git 读口，不另造所有权账本。

        **rc-3d12fbbe 修复（排序遮蔽）**：当 `current_development_id` 非 None
        时，本单自身在飞绑定**不构成外来占用**——跳过并继续扫描其余 record，
        绝不因「先命中自身绑定」而停止枚举。这样即使本单 dev id 在
        `<dd_root>/` 枚举顺序上排序靠前（如 dev-fg-644942a367ae 先于
        dev-fg-cfe509fa9c23）、且两单都绑定到同一棵 canonical 树，也能继续
        扫到更靠后的外来在飞单并如实返回它，不再被自身绑定遮蔽漏检。
        """
        tree = _resolved(tree_path)
        if not dd_root.is_dir():
            return {"bound_development_id": None, "in_flight": False, "detail": ""}
        indeterminate: list[str] = []
        for child in sorted(dd_root.iterdir()):
            if not child.is_dir():
                continue
            dev_id = child.name
            if current_development_id is not None and dev_id == current_development_id:
                # 本单自身绑定 = 该链要消费的树，不构成外来占用；跳过，继续扫描。
                continue
            record_path = child / RECORD_FILE
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                indeterminate.append(f"{dev_id}: record.json 不可读/坏档（无法判定）")
                continue
            if not isinstance(record, dict):
                indeterminate.append(f"{dev_id}: record.json 顶层非 JSON 对象（无法判定）")
                continue
            repo_path = str(record.get("repo_path") or "")
            if not repo_path:
                continue
            if not self._binding_matches(tree, _resolved(Path(repo_path))):
                continue
            terminal, status_ok = self._terminal_of(child)
            if not status_ok or not terminal:
                return {
                    "bound_development_id": dev_id,
                    "in_flight": True,
                    "detail": (
                        f"{tree_path} 被在飞 development {dev_id} 绑定"
                        f"（status.json terminal={terminal!r}"
                        + ("，status.json 缺失/不可读 fail-closed" if not status_ok else "")
                        + "）"
                    ),
                }
        if indeterminate:
            return {
                "bound_development_id": None,
                "in_flight": True,
                "detail": "；".join(indeterminate),
            }
        return {"bound_development_id": None, "in_flight": False, "detail": ""}

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

    def detect_divergence(self, repo: Path, default_branch: str) -> dict[str, Any]:
        """本地 HEAD 与 origin/<default_branch> 是否分叉（纯读口，零写原语）。

        H3 缺陷修复读口：`git pull --ff-only` 在「本地 HEAD 与 origin 1:1 分叉」
        时报 `Diverging branches can't be fast-forwarded`。这里在 pull **之前**
        机械读判：读本地 HEAD 与远端 tip，双向 `merge-base --is-ancestor`：

        - local 是 origin_tip 的祖先（local 落后/相等）→ 未分叉，可 ff-only pull；
        - origin_tip 是 local 的祖先（local 领先）→ 未分叉（pull 为空操作）；
        - 双向都不是祖先 → 1:1 分叉。

        任一侧读取失败 / 无 origin remote → 保守按「无法判定」返回
        `diverged=True` + detail（不留分叉漏检，编排层据此 escalate，绝不带病
        继续 pull/deploy）。

        返回机器可读 `{"diverged": bool, "local_head": str|None, "origin_head":
        str|None, "detail": str}`。**本方法只读，不含任何 reset / checkout -f /
        强制覆盖**——分叉只 escalate，清理残骸由人做（spec 铁律）。
        """
        local_proc = run_git(repo, "rev-parse", "HEAD")
        if local_proc.returncode != 0:
            return {
                "diverged": True,
                "local_head": None,
                "origin_head": None,
                "detail": "读取本地 HEAD 失败: "
                + (local_proc.stderr or local_proc.stdout).strip()[:400],
            }
        local_head = local_proc.stdout.strip()
        origin_ref = f"origin/{default_branch}"
        origin_proc = run_git(repo, "rev-parse", origin_ref)
        if origin_proc.returncode != 0:
            return {
                "diverged": True,
                "local_head": local_head,
                "origin_head": None,
                "detail": f"读取 {origin_ref} 失败（可能无 origin）: "
                + (origin_proc.stderr or origin_proc.stdout).strip()[:400],
            }
        origin_head = origin_proc.stdout.strip()
        local_is_ancestor = run_git(repo, "merge-base", "--is-ancestor", local_head, origin_head)
        if local_is_ancestor.returncode == 0:
            return {
                "diverged": False,
                "local_head": local_head,
                "origin_head": origin_head,
                "detail": "local 是 origin 祖先（未分叉，可 ff-only pull）",
            }
        if local_is_ancestor.returncode not in (0, 1):
            return {
                "diverged": True,
                "local_head": local_head,
                "origin_head": origin_head,
                "detail": "merge-base 判定 local→origin 失败: "
                + (local_is_ancestor.stderr or local_is_ancestor.stdout).strip()[:400],
            }
        origin_is_ancestor = run_git(repo, "merge-base", "--is-ancestor", origin_head, local_head)
        if origin_is_ancestor.returncode == 0:
            return {
                "diverged": False,
                "local_head": local_head,
                "origin_head": origin_head,
                "detail": "origin 是 local 祖先（未分叉，本地领先）",
            }
        if origin_is_ancestor.returncode not in (0, 1):
            return {
                "diverged": True,
                "local_head": local_head,
                "origin_head": origin_head,
                "detail": "merge-base 判定 origin→local 失败: "
                + (origin_is_ancestor.stderr or origin_is_ancestor.stdout).strip()[:400],
            }
        return {
            "diverged": True,
            "local_head": local_head,
            "origin_head": origin_head,
            "detail": "local 与 origin 双向非祖先 → 1:1 分叉，不能 ff-only pull",
        }

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
    "DD_EXCLUDED_PATHS",
    "DD_WASH_COMMIT_MESSAGE",
    "EXIT_HEAD_MISMATCH",
    "EXIT_NOT_FOUND",
    "EXIT_TIMEOUT",
    "MAKE_VERIFY_ARGV",
    "NO_RESOLVABLE_VERIFY",
    "UV_PYTEST_ARGV",
    "DefaultHarvestOps",
]
