"""纯协议单元测试，不启动引擎或外部服务。"""

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from fleet_graph.cli import load_config
from fleet_graph.protocol import (
    ACTION_SCHEMA,
    ENROLL,
    IMPL_SCHEMA,
    REVIEW_SCHEMA,
    validate,
    validate_actions,
)
from fleet_graph.runtime import RoleConfig


def test_output_list_can_review_dispatch_and_wait():
    actions = [
        {"type": "approve", "dd_id": "a", "review_ref": "r", "summary": "通过"},
        {
            "type": "dispatch",
            "repo_ref": "repo",
            "source_branch": "dd/b",
            "target_branch": "release/x",
            "worktree": "/tmp/b",
            "spec_path": "SPEC.md",
            "summary": "后续任务",
        },
        {"type": "waiting", "summary": "等待"},
    ]
    assert validate_actions(actions) == actions


@pytest.mark.parametrize(
    "value", [[], {"type": "waiting"}, [{"type": "approve", "dd_id": "x"}], [{"type": "unknown"}]]
)
def test_invalid_goal_schema_rejected(value):
    with pytest.raises(ValidationError):
        validate(value, ACTION_SCHEMA)


def test_review_failure_requires_major_or_blocker_and_evidence():
    result = {
        "type": "fail",
        "summary": "发现问题",
        "findings": [{"severity": "minor", "message": "样式"}],
        "evidence_refs": ["event:1"],
    }
    with pytest.raises(ValidationError):
        validate(result, REVIEW_SCHEMA)
    result["findings"][0]["severity"] = "major"
    assert validate(result, REVIEW_SCHEMA) == result


def test_needs_goal_is_not_committed_or_review_pass():
    result = {"type": "needs_goal", "message": "依赖缺失", "evidence_refs": ["log:1"]}
    validate(result, IMPL_SCHEMA)
    with pytest.raises(ValidationError):
        validate(result, REVIEW_SCHEMA)


def test_enroll_opaque_folder_and_one_source_many_repos():
    repo = {
        "path": "/repo",
        "remote": "origin",
        "target_branch": "target",
        "acceptance": ["make verify"],
    }
    request = {
        "schema": "goal.enroll/2",
        "request_id": "request",
        "work_folder": "opaque token with no path rules",
        "title": "测试",
        "source_branch": "release/test",
        "repos": [repo, {**repo, "path": "/another"}],
    }
    validate(request, ENROLL)
    request["repos"][0]["acceptance"] = []
    with pytest.raises(ValidationError):
        validate(request, ENROLL)


def test_shipped_role_configuration_is_constructible():
    config = load_config("config/codex.json")
    for role, item in config["roles"].items():
        value = RoleConfig(**item)
        assert Path(value.system_prompt_file).is_file()
        assert "# References" in Path(value.system_prompt_file).read_text()
        assert value.write is (role != "scribe")
    assert config["agent_run"].startswith("/data/code/fleet-comparison/codex/")


def test_invalid_configuration_rejected_without_starting_runtime(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"roles": {}}))
    with pytest.raises(ValueError, match="缺少配置"):
        load_config(path)


def test_stopping_uncertain_runs_release_engine_lock_without_claiming_stopped():
    from fleet_graph.cli import should_exit

    state = {"status": "stopping", "stop": "immediate", "runs": {"a": {"status": "uncertain"}}}
    assert should_exit(state)
    assert state["status"] == "stopping"
    state["runs"]["b"] = {"status": "running"}
    assert not should_exit(state)
    state["runs"].pop("b")
    state["stop"] = None
    assert not should_exit(state)


def test_done_collects_already_started_scribe_before_engine_exit():
    from fleet_graph.cli import should_exit

    state = {"status": "done", "stop": None, "runs": {"s": {"role": "scribe", "status": "running"}}}
    assert not should_exit(state)
    state["runs"]["s"]["status"] = "finished"
    assert should_exit(state)
    state["runs"]["s"]["status"] = "uncertain"
    assert should_exit(state)
