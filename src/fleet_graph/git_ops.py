"""独立 Git/PR 边界；所有发布绑定已验证的 commit，外部调用可注入。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse


class GitOpsError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def safe_git_environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_TERMINAL_PROMPT="0",
    )
    return env


class PRAdapter:
    """gh/glab 的最小包装；通过分支与 marker 恢复创建结果。"""

    def __init__(self, runner=None):
        self.runner = runner or subprocess.run

    def _run(self, repo, args):
        p = self.runner(
            args, cwd=repo["path"], capture_output=True, text=True, env=safe_git_environment()
        )
        if p.returncode:
            raise GitOpsError("PR_COMMAND_FAILED", p.stderr.strip())
        return p.stdout.strip()

    def list(self, repo, source, target):
        if repo["platform"] == "github":
            raw = self._run(
                repo,
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repo["remote_url"],
                    "--head",
                    source,
                    "--base",
                    target,
                    "--state",
                    "all",
                    "--limit",
                    "100",
                    "--json",
                    "number,url,state,body,headRefName,baseRefName,headRefOid,mergeCommit",
                ],
            )
            return json.loads(raw)
        raw = self._run(
            repo,
            [
                "glab",
                "mr",
                "list",
                "--repo",
                repo["remote_url"],
                "--source-branch",
                source,
                "--target-branch",
                target,
                "--all",
                "--per-page",
                "100",
                "--output",
                "json",
            ],
        )
        return [
            {
                "number": p["iid"],
                "url": p["web_url"],
                "state": p["state"].upper(),
                "body": p.get("description", ""),
                "headRefName": p["source_branch"],
                "baseRefName": p["target_branch"],
                "headRefOid": p.get("sha") or (p.get("diff_refs") or {}).get("head_sha"),
                "mergeCommit": {"oid": p.get("merge_commit_sha") or p.get("squash_commit_sha")},
            }
            for p in json.loads(raw)
        ]

    def ensure(self, repo, dd, marker):
        matches = [
            p
            for p in self.list(repo, dd["source_branch"], dd["target_branch"])
            if marker in p.get("body", "")
            and p.get("headRefName") == dd["source_branch"]
            and p.get("baseRefName") == dd["target_branch"]
            and p["state"].upper() in {"OPEN", "OPENED", "MERGED"}
        ]
        if len(matches) > 1:
            raise GitOpsError("AMBIGUOUS_PR", "同一 marker 对应多个 PR")
        if matches:
            return matches[0]
        title = dd.get("title", f"Fleet Graph: {dd['source_branch']}")
        body = f"{marker}\n\nSPEC: {dd['spec_path']}"
        if repo["platform"] == "github":
            args = [
                "gh",
                "pr",
                "create",
                "--repo",
                repo["remote_url"],
                "--head",
                dd["source_branch"],
                "--base",
                dd["target_branch"],
                "--title",
                title,
                "--body",
                body,
            ]
        else:
            args = [
                "glab",
                "mr",
                "create",
                "--repo",
                repo["remote_url"],
                "--source-branch",
                dd["source_branch"],
                "--target-branch",
                dd["target_branch"],
                "--title",
                title,
                "--description",
                body,
                "--yes",
            ]
        self._run(repo, args)
        matches = [
            p
            for p in self.list(repo, dd["source_branch"], dd["target_branch"])
            if marker in p.get("body", "")
        ]
        if len(matches) != 1:
            raise GitOpsError("PR_NOT_CONFIRMED", "创建后无法唯一确认 PR")
        return matches[0]

    def get(self, repo, pr, source, target):
        matches = [
            p for p in self.list(repo, source, target) if p.get("number") == pr.get("number")
        ]
        if len(matches) != 1:
            raise GitOpsError("PR_NOT_CONFIRMED", "无法确认原 PR 状态")
        return matches[0]

    def close(self, repo, pr, source, target, *, expected_head=None, merged=False):
        """收回终结 PR；返回平台真实状态，不把 closed 伪装成 merged。"""
        current = self.get(repo, pr, source, target)
        if current.get("headRefName") != source or current.get("baseRefName") != target:
            raise GitOpsError("PR_IDENTITY_CHANGED", "PR 分支身份变化")
        if expected_head and current.get("headRefOid") != expected_head:
            raise GitOpsError("PR_HEAD_CHANGED", "PR HEAD 已变化，不关闭新版本")
        if current["state"].upper() in {"MERGED", "CLOSED"}:
            return current
        number = str(current["number"])
        if repo["platform"] == "github":
            args = ["gh", "pr", "close", number, "--repo", repo["remote_url"]]
            if merged:
                args.extend(
                    [
                        "--comment",
                        f"Fleet Graph 已通过 Git CAS 合入目标分支，commit: {expected_head}。"
                        "此操作仅关闭 PR；平台状态以实际记录为准。",
                    ]
                )
        else:
            args = ["glab", "mr", "close", number, "--repo", repo["remote_url"]]
        self._run(repo, args)
        observed = self.get(repo, pr, source, target)
        if observed["state"].upper() not in {"MERGED", "CLOSED"}:
            raise GitOpsError("PR_CLOSE_UNCONFIRMED", "平台尚未确认 PR 关闭")
        return {**observed, "reason": "closed_after_git_merge" if merged else "cancelled"}


class GitOps:
    def __init__(self, runner=None, pr_adapter=None):
        self.runner = runner or subprocess.run
        self.pr_adapter = pr_adapter or PRAdapter(self.runner)

    def _git(self, path, *args, check=True):
        argv = [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.ext.allow=never",
            "-C",
            str(path),
            *args,
        ]
        p = self.runner(argv, capture_output=True, text=True, env=safe_git_environment())
        if check and p.returncode:
            raise GitOpsError("GIT_COMMAND_FAILED", f"{args[0]}: {p.stderr.strip()}")
        return p

    def _text(self, path, *args):
        return self._git(path, *args).stdout.strip()

    def _branch(self, path, branch):
        if not isinstance(branch, str) or branch.startswith("-") or not branch:
            raise GitOpsError("INVALID_BRANCH", "分支名称无效")
        if self._git(path, "check-ref-format", f"refs/heads/{branch}", check=False).returncode:
            raise GitOpsError("INVALID_BRANCH", f"分支名称无效: {branch}")

    def _repo(self, repo):
        result = dict(repo)
        result["path"] = str(Path(repo["path"]).resolve())
        remote = repo.get("remote", "origin")
        if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote):
            raise GitOpsError("INVALID_REMOTE", "remote 必须是已配置的名称")
        url = self._text(result["path"], "remote", "get-url", "--push", remote)
        if not url or url.startswith(("-", "ext::")) or any(ord(c) < 32 for c in url):
            raise GitOpsError("INVALID_REMOTE", "不支持该 remote URL")
        result.update(remote=remote, remote_url=url)
        hostname = urlparse(url).hostname if "://" in url else url.split(":")[0].split("@")[-1]
        result["platform"] = repo.get("platform") or (
            "github" if "github" in (hostname or "") else "gitlab"
        )
        return result

    def _remote_head(self, repo, branch, missing=False):
        ref = f"refs/heads/{branch}"
        p = self._git(
            repo["path"], "ls-remote", "--exit-code", "--refs", repo["remote_url"], ref, check=False
        )
        if missing and p.returncode == 2:
            return None
        rows = [line.split() for line in p.stdout.splitlines()]
        if (
            p.returncode
            or len(rows) != 1
            or rows[0][1:] != [ref]
            or not re.fullmatch(r"[0-9a-f]{40,64}", rows[0][0])
        ):
            raise GitOpsError("REMOTE_REF_UNAVAILABLE", f"无法解析 {ref}")
        return rows[0][0]

    def _fetch(self, repo, branch):
        self._git(
            repo["path"],
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--refmap=",
            repo["remote_url"],
            f"refs/heads/{branch}",
        )
        return self._text(repo["path"], "rev-parse", "FETCH_HEAD")

    def _ancestor(self, path, before, after):
        p = self._git(path, "merge-base", "--is-ancestor", before, after, check=False)
        if p.returncode not in {0, 1}:
            raise GitOpsError("ANCESTRY_FAILED", p.stderr)
        return p.returncode == 0

    def _common(self, path):
        raw = self._text(path, "rev-parse", "--git-common-dir")
        return (Path(path) / raw).resolve()

    @contextmanager
    def _lock(self, repo, target):
        digest = hashlib.sha256((repo["remote_url"] + "\0" + target).encode()).hexdigest()
        path = self._common(repo["path"]) / f"fleet-graph-{digest}.lock"
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _push(self, repo, branch, old, new):
        ref = f"refs/heads/{branch}"
        p = self._git(
            repo["path"],
            "push",
            f"--force-with-lease={ref}:{old or ''}",
            repo["remote_url"],
            f"{new}:{ref}",
            check=False,
        )
        actual = self._remote_head(repo, branch)
        if actual != new:
            raise GitOpsError("REMOTE_HEAD_CONFLICT", p.stderr or "远端已变化")

    def inspect_refs(self, repo: dict, source_branch: str, target_branch=None) -> dict:
        """只核实当前远端 refs；收尾重放无需重新执行 release 接管。"""
        repo = self._repo(repo)
        target = target_branch or repo["target_branch"]
        for branch in (source_branch, target):
            self._branch(repo["path"], branch)
        return {
            **repo,
            "source_head": self._fetch(repo, source_branch),
            "target_head": self._fetch(repo, target),
        }

    def prepare_repo(self, repo: dict, source_branch: str, takeover: bool = False) -> dict:
        repo = self._repo(repo)
        target = repo["target_branch"]
        for branch in (source_branch, target):
            self._branch(repo["path"], branch)
        if source_branch == target:
            raise GitOpsError("INVALID_BRANCH", "source 与 target 不可相同")
        with self._lock(repo, source_branch):
            target_head = self._fetch(repo, target)
            source_head = self._remote_head(repo, source_branch, missing=True)
            operation = repo.get("operation_id")
            receipt_path = None
            receipt = None
            if operation:
                digest = hashlib.sha256(str(operation).encode()).hexdigest()
                receipt_path = self._common(repo["path"]) / f"fleet-prepare-{digest}.json"
                if receipt_path.exists():
                    receipt = json.loads(receipt_path.read_text())
                    if (
                        receipt["source_branch"] != source_branch
                        or receipt["remote_url"] != repo["remote_url"]
                        or receipt.get("target_branch") != target
                    ):
                        raise GitOpsError("PREPARE_IDENTITY_CHANGED", "准备操作身份变化")
            recovered = receipt is not None and receipt["head"] == source_head
            if source_head:
                if not takeover and not recovered:
                    raise GitOpsError("TAKEOVER_REQUIRED", "已有 release 需要显式接管")
                fetched = self._fetch(repo, source_branch)
                if fetched != source_head or not self._ancestor(repo["path"], target_head, fetched):
                    raise GitOpsError("INVALID_RELEASE_ANCESTRY", "已有 release 未包含当前 target")
            else:
                source_head = target_head
                if receipt_path:
                    # 意图落盘早于 push；重放只接管这个意图对应的精确 HEAD。
                    temporary = receipt_path.with_suffix(".tmp")
                    with temporary.open("w") as handle:
                        json.dump(
                            {
                                "source_branch": source_branch,
                                "remote_url": repo["remote_url"],
                                "target_branch": target,
                                "head": source_head,
                            },
                            handle,
                        )
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, receipt_path)
                    directory = os.open(receipt_path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                self._push(repo, source_branch, None, source_head)
            return {
                **repo,
                "source_branch": source_branch,
                "source_head": source_head,
                "target_head": target_head,
            }

    def verify_handoff(self, repo: dict, dd: dict) -> dict:
        repo = self._repo(repo)
        path = str(Path(dd["worktree"]).resolve())
        for branch in (dd["source_branch"], dd["target_branch"]):
            self._branch(path, branch)
        if dd["source_branch"] == dd["target_branch"]:
            raise GitOpsError("INVALID_BRANCH", "source 与 target 不可相同")
        if self._common(path) != self._common(repo["path"]):
            raise GitOpsError("WRONG_REPOSITORY", "worktree 不属于指定 repository")
        if self._text(path, "symbolic-ref", "--short", "HEAD") != dd["source_branch"]:
            raise GitOpsError("WRONG_BRANCH", "worktree 分支与交付不一致")
        if self._text(path, "status", "--porcelain=v1", "--untracked-files=all"):
            raise GitOpsError("DIRTY_WORKTREE", "worktree 尚有未提交内容")
        head = self._text(path, "rev-parse", "HEAD")
        if head != self._remote_head(repo, dd["source_branch"]):
            raise GitOpsError("REMOTE_HEAD_CONFLICT", "交付 HEAD 尚未 push 或远端已变化")
        spec = Path(dd["spec_path"])
        if spec.is_absolute():
            try:
                spec = spec.resolve().relative_to(Path(path))
            except ValueError as exc:
                raise GitOpsError("INVALID_SPEC", "SPEC 必须位于 worktree 内") from exc
        if not spec.parts or ".." in spec.parts:
            raise GitOpsError("INVALID_SPEC", "SPEC 路径无效")
        listing = self._text(path, "ls-tree", head, "--", str(spec))
        if not listing.startswith(("100644 blob ", "100755 blob ")):
            raise GitOpsError("INVALID_SPEC", "SPEC 必须是已提交的普通文件")
        return {
            "head": head,
            "source_branch": dd["source_branch"],
            "target_branch": dd["target_branch"],
            "worktree": path,
            "spec_path": str(spec),
        }

    def ensure_pr(self, repo: dict, dd: dict, marker: str) -> dict:
        if not marker or "\n" in marker:
            raise GitOpsError("INVALID_MARKER", "PR marker 必须非空且为单行")
        self.verify_handoff(repo, dd)
        return self.pr_adapter.ensure(self._repo(repo), dd, marker)

    def recover_merge(self, repo, source_branch, target_branch, expected_head, pr=None):
        """版本作废后的只读收据恢复，绝不继续尚未发生的合并。"""
        repo = self._repo(repo)
        for branch in (source_branch, target_branch):
            self._branch(repo["path"], branch)
        target = self._fetch(repo, target_branch)
        if self._ancestor(repo["path"], expected_head, target):
            return {
                "status": "merged",
                "head": expected_head,
                "target_head": target,
                "recovered": True,
            }
        if pr:
            observed = self.pr_adapter.get(repo, pr, source_branch, target_branch)
            commit = (observed.get("mergeCommit") or {}).get("oid")
            if (
                observed["state"].upper() == "MERGED"
                and commit
                and observed.get("headRefOid") == expected_head
                and observed.get("headRefName") == source_branch
                and observed.get("baseRefName") == target_branch
                and self._ancestor(repo["path"], commit, target)
            ):
                return {
                    "status": "merged",
                    "head": expected_head,
                    "target_head": target,
                    "recovered": True,
                }
        return {"status": "review_required", "reason": "merge_not_observed"}

    def merge(
        self,
        repo: dict,
        source_branch: str,
        target_branch: str,
        expected_head: str,
        worktree=None,
        pr=None,
    ) -> dict:
        repo = self._repo(repo)
        for branch in (source_branch, target_branch):
            self._branch(repo["path"], branch)
        if source_branch == target_branch or not re.fullmatch(r"[0-9a-f]{40,64}", expected_head):
            raise GitOpsError("INVALID_MERGE", "合并输入无效")
        with self._lock(repo, target_branch):
            target = self._fetch(repo, target_branch)
            if self._ancestor(repo["path"], expected_head, target):
                return {
                    "status": "merged",
                    "head": expected_head,
                    "target_head": target,
                    "recovered": True,
                }
            if pr:
                observed = self.pr_adapter.get(repo, pr, source_branch, target_branch)
                if observed["state"].upper() == "MERGED":
                    commit = (observed.get("mergeCommit") or {}).get("oid")
                    if (
                        commit
                        and observed.get("headRefOid") == expected_head
                        and observed.get("headRefName") == source_branch
                        and observed.get("baseRefName") == target_branch
                        and self._ancestor(repo["path"], commit, target)
                    ):
                        return {
                            "status": "merged",
                            "head": expected_head,
                            "target_head": target,
                            "recovered": True,
                        }
                    raise GitOpsError("PR_MERGE_UNCONFIRMED", "平台合并结果未在 target 验证")
            source = self._fetch(repo, source_branch)
            if source != expected_head:
                return {"status": "review_required", "head": source, "reason": "source_changed"}
            if not self._ancestor(repo["path"], target, source):
                if not worktree:
                    return {
                        "status": "review_required",
                        "head": source,
                        "reason": "target_changed",
                        "target_head": target,
                    }
                path = str(Path(worktree).resolve())
                if (
                    self._common(path) != self._common(repo["path"])
                    or self._text(path, "symbolic-ref", "--short", "HEAD") != source_branch
                    or self._text(path, "rev-parse", "HEAD") != expected_head
                    or self._text(path, "status", "--porcelain=v1", "--untracked-files=all")
                ):
                    raise GitOpsError("INVALID_WORKTREE", "无法在该 worktree 更新 source")
                p = self._git(path, "merge", "--no-edit", target, check=False)
                if p.returncode:
                    conflicts = self._text(path, "diff", "--name-only", "--diff-filter=U")
                    if not conflicts:
                        raise GitOpsError("MERGE_FAILED", p.stderr)
                    return {
                        "status": "conflict",
                        "head": source,
                        "target_head": target,
                        "files": conflicts.splitlines(),
                        "worktree": path,
                    }
                updated = self._text(path, "rev-parse", "HEAD")
                self._push(repo, source_branch, source, updated)
                return {
                    "status": "review_required",
                    "head": updated,
                    "reason": "target_merged_into_source",
                    "target_head": target,
                }
            if self._remote_head(repo, source_branch) != expected_head:
                return {"status": "review_required", "reason": "source_changed"}
            self._push(repo, target_branch, target, expected_head)
            return {"status": "merged", "head": expected_head, "target_head": expected_head}

    def cleanup(self, repo: dict, dd: dict) -> dict:
        if dd.get("status") not in {"merged", "done", "cancelled", "failed"}:
            return {"status": "skipped", "reason": "not_terminal"}
        repo = self._repo(repo)
        source = dd["source_branch"]
        self._branch(repo["path"], source)
        if source in {dd["target_branch"], repo["target_branch"], "main", "master"}:
            raise GitOpsError("PROTECTED_BRANCH", "不可清理目标分支")
        path = Path(dd["worktree"]).resolve()
        if path == Path(repo["path"]).resolve():
            raise GitOpsError("PROTECTED_WORKTREE", "不可删除主 checkout")
        expected = dd.get("head") or (dd.get("merge") or {}).get("head") or dd.get("cleanup_head")
        result = {}
        dirty = False
        with self._lock(repo, source):
            if path.exists():
                if self._common(path) != self._common(repo["path"]):
                    raise GitOpsError("WRONG_REPOSITORY", "不可清理外部 worktree")
                if self._text(path, "symbolic-ref", "--short", "HEAD") != source:
                    raise GitOpsError("WRONG_BRANCH", "worktree 分支不匹配")
                dirty = bool(
                    self._text(
                        path, "status", "--porcelain=v1", "--untracked-files=all", "--ignored"
                    )
                )
                if expected and self._text(path, "rev-parse", "HEAD") != expected:
                    return {"status": "skipped", "reason": "local_head_changed"}
            if not expected or not re.fullmatch(r"[0-9a-f]{40,64}", expected):
                return {
                    "status": "skipped",
                    "reason": "dirty_worktree" if dirty else "missing_expected_head",
                }
            remote_head = self._remote_head(repo, source, missing=True)
            if remote_head and remote_head != expected:
                return {"status": "skipped", "reason": "remote_head_changed"}
            ref = f"refs/heads/{source}"
            local = self._git(repo["path"], "rev-parse", "--verify", ref, check=False)
            local_head = local.stdout.strip() if local.returncode == 0 else None
            if local_head and local_head != expected:
                return {"status": "skipped", "reason": "local_head_changed"}
            if dd.get("pr"):
                result["pr"] = self.pr_adapter.close(
                    repo,
                    dd["pr"],
                    source,
                    dd["target_branch"],
                    expected_head=expected,
                    merged=dd["status"] in {"merged", "done"},
                )
            if dirty:
                return {**result, "status": "skipped", "reason": "dirty_worktree"}
            if path.exists():
                self._git(repo["path"], "worktree", "remove", str(path))
            worktrees = self._text(repo["path"], "worktree", "list", "--porcelain")
            if f"branch {ref}" in worktrees.splitlines():
                return {**result, "status": "skipped", "reason": "branch_in_use"}
            if remote_head:
                self._git(
                    repo["path"],
                    "push",
                    f"--force-with-lease={ref}:{expected}",
                    repo["remote_url"],
                    f":{ref}",
                    check=False,
                )
                if self._remote_head(repo, source, missing=True) is not None:
                    raise GitOpsError("REMOTE_HEAD_CONFLICT", "远端分支清理未完成，保留新提交")
            if local_head:
                self._git(repo["path"], "update-ref", "-d", ref, expected)
            return {**result, "status": "cleaned", "head": expected}
