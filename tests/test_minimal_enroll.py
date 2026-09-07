"""Tests for the minimal enroll validation node (goal.enroll/2).

All filesystem/git checks go through a fake probe — nothing here touches real
git or spawns processes.
"""

from __future__ import annotations

import re

import pytest

from fleet_graph.minimal.enroll import (
    EnrollValidation,
    normalize_enroll,
    validate_enroll,
)


class FakeProbe:
    def __init__(self, *, worktrees=None, branches=None, bad_commands=None):
        self.worktrees = set(worktrees or [])
        # branches: set of (path, branch) pairs that exist
        self.branches = set(branches or [])
        # bad_commands: commands that fail bash -n
        self.bad_commands = set(bad_commands or [])
        self.branch_queries = []

    def is_worktree(self, path):
        return path in self.worktrees

    def branch_exists(self, path, branch):
        self.branch_queries.append((path, branch))
        return (path, branch) in self.branches

    def bash_parses(self, command):
        return command not in self.bad_commands


def base_payload():
    return {
        "schema": "goal.enroll/2",
        "work_folder": None,
        "title": "Rebuild the minimal system",
        "goal_text": "Rebuild fleet-graph as the minimal system per docs/specs/minimal/.",
        "source_branch": "release/loopx-minimal",
        "repos": [
            {
                "path": "/data/wt/alpha",
                "remote": "git@github.com:Dandi007/fleet-graph.git",
                "target_branch": "main",
                "acceptance": ["make verify"],
            },
            {
                "path": "/data/wt/beta",
                "remote": "git@github.com:Dandi007/ops.git",
                "target_branch": "release/prod",
                "acceptance": ["make test"],
            },
        ],
    }


def full_probe():
    return FakeProbe(
        worktrees={"/data/wt/alpha", "/data/wt/beta"},
        branches={
            ("/data/wt/alpha", "main"),
            ("/data/wt/beta", "release/prod"),
        },
    )


def errors_mention(result, needle):
    return any(needle in e for e in result.errors)


class TestValidPayloads:
    def test_full_valid_two_repo_request(self):
        result = validate_enroll(base_payload(), git_probe=full_probe())
        assert result == EnrollValidation(ok=True, errors=[])
        assert result.ok is True
        assert result.errors == []

    def test_missing_work_folder_key_is_valid(self):
        payload = base_payload()
        del payload["work_folder"]
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is True

    def test_null_work_folder_is_valid(self):
        result = validate_enroll(base_payload(), git_probe=full_probe())
        assert result.ok is True

    def test_string_work_folder_is_valid(self):
        payload = base_payload()
        payload["work_folder"] = "wf-ab12cd"
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is True


class TestSchemaAndRemovedFields:
    def test_wrong_schema_rejected(self):
        payload = base_payload()
        payload["schema"] = "goal.enroll/1"
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "schema")

    @pytest.mark.parametrize("removed", ["goal_path", "sessions", "warn", "repo"])
    def test_removed_fields_rejected(self, removed):
        payload = base_payload()
        payload[removed] = {"whatever": 1}
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, f"{removed}: removed field")

    def test_unknown_top_level_key_rejected(self):
        payload = base_payload()
        payload["models"] = {"goal": "claude-opus-5"}
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "models: unknown top-level key")

    def test_source_branch_inside_repo_entry_rejected(self):
        payload = base_payload()
        payload["repos"][0]["source_branch"] = "release/loopx-minimal"
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos[0].source_branch")
        assert errors_mention(result, "unknown key in repo entry")


class TestTitleAndGoalText:
    @pytest.mark.parametrize("bad", ["", None, 42, [], {}])
    def test_bad_title_rejected(self, bad):
        payload = base_payload()
        payload["title"] = bad
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "title")

    def test_missing_title_rejected(self):
        payload = base_payload()
        del payload["title"]
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "title: missing")

    @pytest.mark.parametrize("bad", ["", None, 42, [], {}])
    def test_bad_goal_text_rejected(self, bad):
        payload = base_payload()
        payload["goal_text"] = bad
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "goal_text")


class TestSourceBranch:
    @pytest.mark.parametrize("bad", [None, "", 42, []])
    def test_bad_source_branch_rejected(self, bad):
        payload = base_payload()
        payload["source_branch"] = bad
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "source_branch")

    @pytest.mark.parametrize(
        "bad",
        [
            "release loopx",  # space
            "-release/loopx",  # leading dash
            "release/../loopx",  # ..
            "release/loopx/",  # trailing slash
            "release/loop\x00x",  # control char
            "release/loopx\n",  # newline (control char)
        ],
    )
    def test_invalid_branch_names_rejected(self, bad):
        payload = base_payload()
        payload["source_branch"] = bad
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "not a valid git branch name")

    @pytest.mark.parametrize(
        "bad",
        [
            "release/x^",  # caret
            "a:b",  # colon
            "x~1",  # tilde
            "foo*",  # asterisk
            "x[y",  # open bracket
            "x\\y",  # backslash
            "x?y",  # question mark
        ],
    )
    def test_branch_names_with_special_chars_rejected(self, bad):
        payload = base_payload()
        payload["source_branch"] = bad
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "not a valid git branch name")

    @pytest.mark.parametrize(
        "bad",
        ["release/x^", "a:b", "x~1", "foo*", "x[y"],
    )
    def test_target_branch_special_chars_rejected(self, bad):
        payload = base_payload()
        payload["repos"][0]["target_branch"] = bad
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "not a valid git branch name")

    def test_missing_source_branch_rejected(self):
        payload = base_payload()
        del payload["source_branch"]
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "source_branch: missing")


class TestRepos:
    def test_empty_repos_rejected(self):
        payload = base_payload()
        payload["repos"] = []
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos: must be a non-empty array")

    def test_missing_repos_rejected(self):
        payload = base_payload()
        del payload["repos"]
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos: missing")

    def test_repos_not_a_list_rejected(self):
        payload = base_payload()
        payload["repos"] = {"path": "/data/wt/alpha"}
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos: must be an array")

    def test_repo_not_an_object_rejected(self):
        payload = base_payload()
        payload["repos"][0] = "/data/wt/alpha"
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos[0]: must be an object")

    def test_missing_remote_rejected_with_go32(self):
        payload = base_payload()
        del payload["repos"][1]["remote"]
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos[1].remote: missing")
        assert errors_mention(result, "GO-32")

    @pytest.mark.parametrize("bad", ["", None, 42])
    def test_empty_remote_rejected(self, bad):
        payload = base_payload()
        payload["repos"][0]["remote"] = bad
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos[0].remote")

    def test_path_not_a_worktree_rejected(self):
        probe = full_probe()
        probe.worktrees.discard("/data/wt/beta")
        result = validate_enroll(base_payload(), git_probe=probe)
        assert result.ok is False
        assert errors_mention(result, "repos[1].path")
        assert errors_mention(result, "not a git worktree")

    @pytest.mark.parametrize("bad", ["", None, 42])
    def test_bad_path_rejected(self, bad):
        payload = base_payload()
        payload["repos"][0]["path"] = bad
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos[0].path")

    def test_target_branch_missing_in_repo_rejected(self):
        probe = full_probe()
        probe.branches.discard(("/data/wt/alpha", "main"))
        result = validate_enroll(base_payload(), git_probe=probe)
        assert result.ok is False
        assert errors_mention(result, "repos[0].target_branch")
        assert errors_mention(result, "not found in repo")

    def test_missing_target_branch_key_rejected(self):
        payload = base_payload()
        del payload["repos"][0]["target_branch"]
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos[0].target_branch: missing")

    @pytest.mark.parametrize("bad", ["", None, "has space", "-main", "a..b", "main/"])
    def test_invalid_target_branch_name_rejected(self, bad):
        payload = base_payload()
        payload["repos"][0]["target_branch"] = bad
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos[0].target_branch")

    def test_empty_acceptance_rejected(self):
        payload = base_payload()
        payload["repos"][0]["acceptance"] = []
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos[0].acceptance: must be a non-empty array")

    def test_missing_acceptance_rejected(self):
        payload = base_payload()
        del payload["repos"][0]["acceptance"]
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos[0].acceptance: missing")

    def test_acceptance_not_a_list_rejected(self):
        payload = base_payload()
        payload["repos"][0]["acceptance"] = "make verify"
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos[0].acceptance: must be an array")

    @pytest.mark.parametrize("bad", ["", None, 42])
    def test_non_string_acceptance_entry_rejected(self, bad):
        payload = base_payload()
        payload["repos"][0]["acceptance"] = [bad]
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "repos[0].acceptance[0]")

    def test_syntactically_invalid_acceptance_rejected(self):
        probe = full_probe()
        probe.bad_commands.add("if ; then fi")
        payload = base_payload()
        payload["repos"][0]["acceptance"] = ["if ; then fi"]
        result = validate_enroll(payload, git_probe=probe)
        assert result.ok is False
        assert errors_mention(result, "repos[0].acceptance[0]")
        assert errors_mention(result, "does not parse")

    def test_duplicate_paths_rejected(self):
        payload = base_payload()
        payload["repos"][1]["path"] = payload["repos"][0]["path"]
        probe = full_probe()
        probe.branches.add(("/data/wt/alpha", "release/prod"))
        result = validate_enroll(payload, git_probe=probe)
        assert result.ok is False
        assert errors_mention(result, "repos[1].path: duplicate path already used by repos[0]")


class TestWorkFolder:
    @pytest.mark.parametrize("bad", ["", 42, [], {}])
    def test_bad_work_folder_rejected(self, bad):
        payload = base_payload()
        payload["work_folder"] = bad
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "work_folder")

    @pytest.mark.parametrize("bad", ["ab12cd", "wf", "folder-1", "WF-ab12cd"])
    def test_wrong_prefix_work_folder_rejected(self, bad):
        payload = base_payload()
        payload["work_folder"] = bad
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is False
        assert errors_mention(result, "work_folder: must start with 'wf-'")

    def test_wf_prefix_work_folder_accepted(self):
        payload = base_payload()
        payload["work_folder"] = "wf-ab12cd"
        result = validate_enroll(payload, git_probe=full_probe())
        assert result.ok is True


class TestValidateProbeWiring:
    def test_worktree_probe_called_for_every_repo(self):
        probe = full_probe()
        validate_enroll(base_payload(), git_probe=probe)
        assert probe.branch_queries == [
            ("/data/wt/alpha", "main"),
            ("/data/wt/beta", "release/prod"),
        ]

    def test_branch_not_probed_when_path_invalid(self):
        payload = base_payload()
        payload["repos"][0]["path"] = ""
        probe = full_probe()
        result = validate_enroll(payload, git_probe=probe)
        assert result.ok is False
        assert ("/data/wt/alpha", "main") not in probe.branch_queries
        assert ("/data/wt/beta", "release/prod") in probe.branch_queries


class TestNormalizeEnroll:
    def test_generates_stable_shaped_goal_id(self):
        payload = base_payload()
        normalized = normalize_enroll(payload, git_probe=full_probe())
        assert re.fullmatch(r"g-[0-9a-f]{6}", normalized["goal_id"])

    @pytest.mark.parametrize("work_folder", [None, "wf-ab12cd"])
    def test_validate_ok_payload_never_raises(self, work_folder):
        payload = base_payload()
        payload["work_folder"] = work_folder
        assert validate_enroll(payload, git_probe=full_probe()).ok is True
        normalized = normalize_enroll(payload, git_probe=full_probe())
        assert normalized["work_folder"] == work_folder

    def test_missing_work_folder_key_normalize_does_not_raise(self):
        payload = base_payload()
        del payload["work_folder"]
        assert validate_enroll(payload, git_probe=full_probe()).ok is True
        normalized = normalize_enroll(payload, git_probe=full_probe())
        assert normalized["work_folder"] is None

    def test_given_goal_id_preserved(self):
        normalized = normalize_enroll(base_payload(), goal_id="g-7f3a2c", git_probe=full_probe())
        assert normalized["goal_id"] == "g-7f3a2c"

    def test_fixed_field_order(self):
        normalized = normalize_enroll(base_payload(), git_probe=full_probe())
        assert list(normalized) == [
            "schema",
            "goal_id",
            "work_folder",
            "title",
            "goal_text",
            "source_branch",
            "repos",
        ]
        assert list(normalized["repos"][0]) == [
            "path",
            "remote",
            "target_branch",
            "acceptance",
        ]

    def test_repos_semantics_unchanged(self):
        payload = base_payload()
        normalized = normalize_enroll(payload, git_probe=full_probe())
        assert normalized["repos"] == payload["repos"]
        assert normalized["repos"][0]["acceptance"] == ["make verify"]

    def test_top_level_fields_preserved(self):
        payload = base_payload()
        payload["work_folder"] = "wf-ab12cd"
        normalized = normalize_enroll(payload, git_probe=full_probe())
        assert normalized["schema"] == "goal.enroll/2"
        assert normalized["work_folder"] == "wf-ab12cd"
        assert normalized["title"] == payload["title"]
        assert normalized["goal_text"] == payload["goal_text"]
        assert normalized["source_branch"] == payload["source_branch"]

    def test_invalid_payload_raises(self):
        payload = base_payload()
        del payload["repos"][0]["remote"]
        with pytest.raises(ValueError, match="repos\\[0\\]\\.remote"):
            normalize_enroll(payload, git_probe=full_probe())

    def test_default_probe_is_subprocess_probe(self):
        from fleet_graph.minimal.enroll import SubprocessProbe, _default_probe

        assert isinstance(_default_probe(), SubprocessProbe)
