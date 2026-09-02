"""M3 交付 A：收割写白名单（allowlist 先行）。

测试覆盖 spec 交付 D.1 的全部拒绝路径与非拒绝路径，以及「默认 deny-all」铁律：
非白名单 repo / 分支 / 部署脚本 -> 拒绝 + 留痕（reasons），绝不静默放行；空
白名单不授予任何写权限。纯函数测试，不碰任何真实 git 或部署。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.supervise.harvest_allowlist import (
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

    def test_entry_as_dict_includes_optional_per_repo_fields(self) -> None:
        entry = HarvestAllowlistEntry(
            repo_path="/data/x",
            allowed_branches=("refs/heads/main",),
            allowed_deploy=(("make", "deploy"),),
            default_branch="main",
            deploy_command=("make", "deploy"),
        )
        out = entry.as_dict()
        assert out["default_branch"] == "main"
        assert out["deploy_command"] == ["make", "deploy"]


class TestPerRepoEffectiveValues:
    """案 A 改写任务 1/2：逐仓生效值（default_branch / deploy_command 取自条目）。"""

    def test_optional_default_branch_is_parsed(self) -> None:
        wl = parse_harvest_allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "allowed_branches": ["refs/heads/main"],
                        "default_branch": "main",
                    }
                ]
            }
        )
        entry = wl.entry_for("/data/code/self/fleet-graph")
        assert entry is not None
        assert entry.default_branch == "main"
        assert entry.deploy_command is None

    def test_optional_deploy_command_is_parsed(self) -> None:
        wl = parse_harvest_allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "allowed_branches": ["refs/heads/main"],
                        "allowed_deploy": [["make", "deploy"]],
                        "deploy_command": ["make", "deploy"],
                    }
                ]
            }
        )
        entry = wl.entry_for("/data/code/self/fleet-graph")
        assert entry is not None
        assert entry.deploy_command == ("make", "deploy")

    def test_entries_without_new_fields_are_backward_compatible(self) -> None:
        entry = allowlist().entry_for("/data/code/self/fleet-graph")
        assert entry is not None
        assert entry.default_branch is None
        assert entry.deploy_command is None

    def test_bad_default_branch_is_refused(self) -> None:
        with pytest.raises(HarvestAllowlistError, match="default_branch"):
            parse_harvest_allowlist(
                {
                    "entries": [
                        {
                            "repo_path": "/data/x",
                            "allowed_branches": ["refs/heads/main"],
                            "default_branch": "",
                        }
                    ]
                }
            )

    def test_bad_deploy_command_is_refused(self) -> None:
        with pytest.raises(HarvestAllowlistError, match="deploy_command"):
            parse_harvest_allowlist(
                {
                    "entries": [
                        {
                            "repo_path": "/data/x",
                            "allowed_branches": ["refs/heads/main"],
                            "deploy_command": ["make", 3],
                        }
                    ]
                }
            )

    def test_effective_default_branch_prefers_entry(self) -> None:
        wl = parse_harvest_allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "allowed_branches": ["refs/heads/main"],
                        "default_branch": "main",
                    }
                ]
            }
        )
        assert (
            wl.effective_default_branch(
                "/data/code/self/fleet-graph", global_default_branch="master"
            )
            == "main"
        )

    def test_effective_default_branch_falls_back_to_global(self) -> None:
        entry = allowlist().entry_for("/data/code/self/fleet-graph")
        assert entry is not None and entry.default_branch is None
        assert (
            allowlist().effective_default_branch(
                "/data/code/self/fleet-graph", global_default_branch="master"
            )
            == "master"
        )

    def test_effective_deploy_empty_for_merge_only_entry(self) -> None:
        """任务 2：allowed_deploy=[]（只授合并权）-> 生效 deploy 命令为空。"""
        wl = parse_harvest_allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "allowed_branches": ["refs/heads/main"],
                        "allowed_deploy": [],
                    }
                ]
            }
        )
        assert (
            wl.effective_deploy_command(
                "/data/code/self/fleet-graph", global_deploy_command=["make", "deploy"]
            )
            == []
        )

    def test_effective_deploy_prefers_entry_command(self) -> None:
        wl = parse_harvest_allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "allowed_branches": ["refs/heads/main"],
                        "allowed_deploy": [["make", "deploy"]],
                        "deploy_command": ["make", "deploy"],
                    }
                ]
            }
        )
        assert wl.effective_deploy_command(
            "/data/code/self/fleet-graph", global_deploy_command=["bash", "other.sh"]
        ) == ["make", "deploy"]

    def test_effective_deploy_falls_back_to_global(self) -> None:
        assert allowlist().effective_deploy_command(
            "/data/code/self/fleet-graph", global_deploy_command=["make", "deploy"]
        ) == ["make", "deploy"]

    def test_effective_values_for_repo_not_in_allowlist_use_global(self) -> None:
        wl = parse_harvest_allowlist(
            {
                "entries": [
                    {
                        "repo_path": "/data/code/self/fleet-graph",
                        "allowed_branches": ["refs/heads/main"],
                        "default_branch": "main",
                    }
                ]
            }
        )
        assert wl.effective_default_branch("/data/other", "master") == "master"
        assert wl.effective_deploy_command("/data/other", ["make", "deploy"]) == ["make", "deploy"]
