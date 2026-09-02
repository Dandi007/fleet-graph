"""M3 交付 A：收割写白名单（allowlist 先行）。

测试覆盖 spec 交付 D.1 的全部拒绝路径与非拒绝路径，以及「默认 deny-all」铁律：
非白名单 repo / 分支 / 部署脚本 -> 拒绝 + 留痕（reasons），绝不静默放行；空
白名单不授予任何写权限。纯函数测试，不碰任何真实 git 或部署。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.supervise.harvest_allowlist import (
    DEFAULT_GOVERNED_ROOT,
    HarvestAllowlist,
    HarvestAllowlistEntry,
    HarvestAllowlistError,
    load_harvest_allowlist,
    parse_harvest_allowlist,
)


def allowlist(**overrides: Any) -> HarvestAllowlist:
    raw: dict[str, Any] = {
        "entries": [
            {
                "repo_path": "/data/code/self/fleet-graph",
                "allowed_branches": ["refs/heads/main", "refs/heads/harvest/"],
                "allowed_deploy": [["/data/apps/fleet-graph/current/deploy/release.sh"]],
            }
        ]
    }
    raw.update(overrides)
    return parse_harvest_allowlist(raw)


class TestDefaultDenyAll:
    def test_empty_allowlist_grants_nothing(self) -> None:
        empty = HarvestAllowlist.default()
        auth = empty.authorize(
            repo_path="/data/code/self/fleet-graph",
            branch="refs/heads/main",
            deploy=("/data/apps/fleet-graph/current/deploy/release.sh",),
        )
        assert auth.granted is False
        assert any("默认 deny-all" in r for r in auth.reasons)

    def test_default_factory_is_deny_all(self) -> None:
        assert HarvestAllowlist().entries == ()


class TestRepoPath:
    def test_repo_in_allowlist_grants(self) -> None:
        auth = allowlist().authorize(
            repo_path="/data/code/self/fleet-graph",
            branch="refs/heads/main",
            deploy=(),
        )
        assert auth.granted is True

    def test_repo_outside_allowlist_is_refused_with_trace(self) -> None:
        auth = allowlist().authorize(
            repo_path="/data/code/self/other-repo",
            branch="refs/heads/main",
            deploy=(),
        )
        assert auth.granted is False
        assert any("不在收割写白名单" in r for r in auth.reasons)


class TestBranchAllowlist:
    def test_allowed_branch_prefix_grants(self) -> None:
        auth = allowlist().authorize(
            repo_path="/data/code/self/fleet-graph",
            branch="refs/heads/harvest/feature-x",
            deploy=(),
        )
        assert auth.granted is True

    def test_branch_outside_prefix_is_refused(self) -> None:
        auth = allowlist().authorize(
            repo_path="/data/code/self/fleet-graph",
            branch="refs/heads/other/x",
            deploy=(),
        )
        assert auth.granted is False
        assert any("不在白名单" in r for r in auth.reasons)

    def test_empty_branch_is_refused(self) -> None:
        auth = allowlist().authorize(
            repo_path="/data/code/self/fleet-graph",
            branch="",
            deploy=(),
        )
        assert auth.granted is False


class TestDeployAllowlist:
    def test_allowed_deploy_argv_grants(self) -> None:
        auth = allowlist().authorize(
            repo_path="/data/code/self/fleet-graph",
            branch="refs/heads/main",
            deploy=("/data/apps/fleet-graph/current/deploy/release.sh",),
        )
        assert auth.granted is True

    def test_deploy_outside_allowlist_is_refused_with_trace(self) -> None:
        auth = allowlist().authorize(
            repo_path="/data/code/self/fleet-graph",
            branch="refs/heads/main",
            deploy=("/bin/rm", "-rf", "/"),
        )
        assert auth.granted is False
        assert any("部署命令" in r for r in auth.reasons)

    def test_no_deploy_requested_is_fine(self) -> None:
        auth = allowlist().authorize(
            repo_path="/data/code/self/fleet-graph",
            branch="refs/heads/main",
            deploy=(),
        )
        assert auth.granted is True


class TestConfigParsing:
    def test_relative_repo_path_is_refused(self) -> None:
        with pytest.raises(HarvestAllowlistError, match="绝对路径"):
            parse_harvest_allowlist(
                {
                    "entries": [
                        {
                            "repo_path": "relative/path",
                            "allowed_branches": ["refs/heads/main"],
                        }
                    ]
                }
            )

    def test_empty_allowed_branches_is_refused(self) -> None:
        with pytest.raises(HarvestAllowlistError, match="allowed_branches"):
            parse_harvest_allowlist({"entries": [{"repo_path": "/data/x", "allowed_branches": []}]})

    def test_wildcard_in_branch_prefix_is_refused(self) -> None:
        with pytest.raises(HarvestAllowlistError, match="非法字符"):
            parse_harvest_allowlist(
                {"entries": [{"repo_path": "/data/x", "allowed_branches": ["refs/heads/*"]}]}
            )

    def test_malformed_deploy_argv_is_refused(self) -> None:
        with pytest.raises(HarvestAllowlistError, match="部署命令"):
            parse_harvest_allowlist(
                {
                    "entries": [
                        {
                            "repo_path": "/data/x",
                            "allowed_branches": ["refs/heads/main"],
                            "allowed_deploy": [["script.sh", 3]],
                        }
                    ]
                }
            )

    def test_missing_file_loads_deny_all(self, tmp_path: Path) -> None:
        loaded = load_harvest_allowlist(tmp_path / "absent.json")
        assert loaded == HarvestAllowlist.default()
        auth = loaded.authorize(repo_path="/data/x", branch="refs/heads/main")
        assert auth.granted is False

    def test_invalid_json_loads_deny_all(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_harvest_allowlist(path) == HarvestAllowlist.default()

    def test_valid_file_loads_and_grants(self, tmp_path: Path) -> None:
        path = tmp_path / "allowlist.json"
        path.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "repo_path": "/data/code/self/fleet-graph",
                            "allowed_branches": ["refs/heads/main"],
                            "allowed_deploy": [["make", "deploy"]],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        loaded = load_harvest_allowlist(path)
        auth = loaded.authorize(
            repo_path="/data/code/self/fleet-graph",
            branch="refs/heads/main",
            deploy=("make", "deploy"),
        )
        assert auth.granted is True

    def test_authorization_is_a_machine_readable_trace(self) -> None:
        auth = allowlist().authorize(
            repo_path="/data/code/self/fleet-graph",
            branch="refs/heads/other/x",
            deploy=("/bin/rm", "-rf", "/"),
        )
        as_dict = auth.as_dict()
        assert as_dict["granted"] is False
        assert isinstance(as_dict["reasons"], list)
        assert any("不在白名单" in r for r in as_dict["reasons"])
        assert any("部署命令" in r for r in as_dict["reasons"])


class TestEntryShape:
    def test_entry_as_dict_round_trips_fields(self) -> None:
        entry = HarvestAllowlistEntry(
            repo_path="/data/x",
            allowed_branches=("refs/heads/main",),
            allowed_deploy=(("make", "deploy"),),
        )
        out = entry.as_dict()
        assert out["repo_path"] == "/data/x"
        assert out["allowed_branches"] == ["refs/heads/main"]
        assert out["allowed_deploy"] == [["make", "deploy"]]

    def test_entry_as_dict_includes_signing_fields_when_present(self) -> None:
        entry = HarvestAllowlistEntry(
            repo_path="/data/x",
            allowed_branches=("refs/heads/main",),
            allowed_deploy=(("make", "deploy"),),
            signed_by="supervisor/seed",
            expires_at="2999-01-01T00:00:00Z",
        )
        out = entry.as_dict()
        assert out["signed_by"] == "supervisor/seed"
        assert out["expires_at"] == "2999-01-01T00:00:00Z"

    def test_top_level_signing_block_is_parsed(self) -> None:
        allowlist = parse_harvest_allowlist(
            {
                "signed_by": "supervisor/seed",
                "expires_at": "2999-01-01T00:00:00Z",
                "entries": [
                    {
                        "repo_path": "/data/x",
                        "allowed_branches": ["refs/heads/main"],
                        "allowed_deploy": [["make", "deploy"]],
                    }
                ],
            }
        )
        assert allowlist.signed_by == "supervisor/seed"
        assert allowlist.expires_at == "2999-01-01T00:00:00Z"
        assert allowlist.entries[0].signed_by is None
        assert allowlist.entries[0].expires_at is None


# --- authorize_repo：真实 git 机械核验（扩围安全判据） ---------------------


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def _init_repo(repo: Path, branch: str = "main") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")


def signed_allowlist(
    repo_path: str,
    *,
    signed_by: str = "supervisor/seed",
    expires_at: str = "2999-01-01T00:00:00Z",
    allowed_branches: list[str] | None = None,
    **overrides: Any,
) -> HarvestAllowlist:
    raw: dict[str, Any] = {
        "entries": [
            {
                "repo_path": repo_path,
                "allowed_branches": allowed_branches or ["refs/heads/main"],
                "allowed_deploy": [["make", "deploy"]],
                "signed_by": signed_by,
                "expires_at": expires_at,
            }
        ]
    }
    raw.update(overrides)
    return parse_harvest_allowlist(raw)


class TestAuthorizeRepoPositive:
    def test_clean_signed_repo_grants(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "fleet-sentinel"
        _init_repo(repo)
        auth = signed_allowlist(str(repo)).authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is True, auth.reasons

    def test_top_level_signing_block_grants(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "fleet-harvest-sandbox"
        _init_repo(repo)
        raw: dict[str, Any] = {
            "signed_by": "supervisor/seed",
            "expires_at": "2999-01-01T00:00:00Z",
            "entries": [
                {
                    "repo_path": str(repo),
                    "allowed_branches": ["refs/heads/main"],
                    "allowed_deploy": [["make", "deploy"]],
                }
            ],
        }
        auth = parse_harvest_allowlist(raw).authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is True, auth.reasons

    def test_entry_signing_overrides_top_level_block(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "override"
        _init_repo(repo)
        raw: dict[str, Any] = {
            "signed_by": "supervisor/seed",
            "expires_at": "2000-01-01T00:00:00Z",
            "entries": [
                {
                    "repo_path": str(repo),
                    "allowed_branches": ["refs/heads/main"],
                    "allowed_deploy": [["make", "deploy"]],
                    "expires_at": "2999-01-01T00:00:00Z",
                }
            ],
        }
        auth = parse_harvest_allowlist(raw).authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is True, auth.reasons

    def test_reasons_empty_trace_when_granted(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "trace"
        _init_repo(repo)
        auth = signed_allowlist(str(repo)).authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.as_dict() == {"granted": True, "reasons": []}


class TestAuthorizeRepoGovernedRoot:
    def test_outside_governed_root_is_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "outside-repo"
        _init_repo(repo)
        root = tmp_path / "governed"
        root.mkdir(parents=True, exist_ok=True)
        auth = signed_allowlist(str(repo)).authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(root)
        )
        assert auth.granted is False
        assert any("受治代码根" in r for r in auth.reasons)

    def test_governed_root_equals_repo_is_not_inside(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        auth = signed_allowlist(str(repo)).authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(repo)
        )
        assert auth.granted is False
        assert any("受治代码根" in r for r in auth.reasons)

    def test_default_governed_root_is_data_code_self(self) -> None:
        assert DEFAULT_GOVERNED_ROOT == "/data/code/self"


class TestAuthorizeRepoGitProbe:
    def test_nonexistent_path_is_refused(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        auth = signed_allowlist(str(missing)).authorize_repo(
            repo_path=str(missing), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("不存在" in r for r in auth.reasons)

    def test_non_git_path_is_refused(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir(parents=True, exist_ok=True)
        auth = signed_allowlist(str(plain)).authorize_repo(
            repo_path=str(plain), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("不是真实 git worktree" in r for r in auth.reasons)

    def test_dirty_worktree_is_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "dirty"
        _init_repo(repo)
        (repo / "uncommitted.txt").write_text("x\n", encoding="utf-8")
        auth = signed_allowlist(str(repo)).authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("不干净" in r for r in auth.reasons)

    def test_modified_tracked_file_makes_worktree_dirty(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "modified"
        _init_repo(repo)
        (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
        auth = signed_allowlist(str(repo)).authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("不干净" in r for r in auth.reasons)

    def test_default_branch_not_covered_is_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "develop-default"
        _init_repo(repo, branch="develop")
        auth = signed_allowlist(str(repo)).authorize_repo(
            repo_path=str(repo), branch="refs/heads/develop", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("默认分支" in r for r in auth.reasons)

    def test_detached_head_is_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "detached"
        _init_repo(repo)
        _git(repo, "checkout", "--detach", "HEAD")
        auth = signed_allowlist(str(repo)).authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("默认分支" in r for r in auth.reasons)

    def test_subdirectory_of_git_repo_is_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "repo"
        _init_repo(repo)
        sub = repo / "subdir"
        sub.mkdir(parents=True, exist_ok=True)
        auth = signed_allowlist(str(sub)).authorize_repo(
            repo_path=str(sub), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("top-level" in r for r in auth.reasons)


class TestAuthorizeRepoSigning:
    def test_missing_signed_by_is_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "unsigned"
        _init_repo(repo)
        auth = signed_allowlist(str(repo), signed_by="").authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("签发出处" in r for r in auth.reasons)

    def test_missing_expires_at_is_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "no-expiry"
        _init_repo(repo)
        auth = signed_allowlist(str(repo), expires_at="").authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("期限" in r for r in auth.reasons)

    def test_expired_entry_is_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "expired"
        _init_repo(repo)
        auth = signed_allowlist(str(repo), expires_at="2000-01-01T00:00:00Z").authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("已过期" in r for r in auth.reasons)

    def test_naive_expiry_is_refused_as_unreadable(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "naive"
        _init_repo(repo)
        auth = signed_allowlist(str(repo), expires_at="2999-01-01").authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("期限" in r for r in auth.reasons)

    def test_garbage_expiry_is_refused_as_unreadable(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "garbage"
        _init_repo(repo)
        auth = signed_allowlist(str(repo), expires_at="not-a-date").authorize_repo(
            repo_path=str(repo), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("期限" in r for r in auth.reasons)


class TestAuthorizeRepoSelfWrite:
    def test_fleet_graph_own_product_root_is_refused(self, tmp_path: Path) -> None:
        import fleet_graph.supervise.harvest_allowlist as ha_module

        env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
        self_root = subprocess.run(
            [
                "git",
                "-C",
                str(Path(ha_module.__file__).resolve().parent),
                "rev-parse",
                "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()
        assert self_root
        governed = str(Path(self_root).parent)
        auth = signed_allowlist(self_root).authorize_repo(
            repo_path=self_root, branch="refs/heads/main", governed_root=governed
        )
        assert auth.granted is False
        assert any("自写禁止" in r for r in auth.reasons)


class TestAuthorizeRepoConfigDenial:
    def test_outside_allowlist_is_still_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "listed"
        other = tmp_path / "repos" / "other"
        _init_repo(repo)
        _init_repo(other)
        auth = signed_allowlist(str(repo)).authorize_repo(
            repo_path=str(other), branch="refs/heads/main", governed_root=str(tmp_path)
        )
        assert auth.granted is False
        assert any("不在收割写白名单" in r for r in auth.reasons)

    def test_deploy_mismatch_is_still_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repos" / "listed"
        _init_repo(repo)
        auth = signed_allowlist(str(repo)).authorize_repo(
            repo_path=str(repo),
            branch="refs/heads/main",
            deploy=("/bin/rm", "-rf", "/"),
            governed_root=str(tmp_path),
        )
        assert auth.granted is False
        assert any("部署命令" in r for r in auth.reasons)
