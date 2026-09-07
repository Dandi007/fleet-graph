"""独立验证返工、程序封存与跨代链还原边界。"""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import git, head
from fleet_graph.dd.control_plane import DdControlPlane
from fleet_graph.dd.upstream_constants import compute_json_digest
from fleet_graph.graphs.dd_pipeline import StageOutcome, StageRefused
from fleet_graph.graphs.dd_scripts import WorkspaceSealer
from test_dd_runner import plugin_seals  # noqa: F401
from test_engine_gate_delivery import waiting_gate  # noqa: F401


def test_red_evidence_allows_reject_but_not_approve(waiting_gate):  # noqa: F811
    node, plane, config, action, launches = waiting_gate
    node._evidence = [replace(item, passed=False) for item in node._evidence]
    refused = node.consume(action, folder_id="wf-owner", round_no=1)
    assert refused["status"] == "failed"
    assert not launches
    action["payload"].update(
        verdict="REJECT",
        board_decision={
            "problem": "真实验收失败",
            "suggested_answer": "修复实现",
            "cost_of_no_answer": "无法交付",
        },
    )
    receipt = node.consume(action, folder_id="wf-owner", round_no=2)
    assert receipt["status"] == "consumed", receipt
    assert launches[0]["terminal"] == "refused"
    rework = plane._seal_gate_rework(json.loads(Path(config.record_path).read_text()), 2)
    assert "真实验收失败" in rework["rationale"]
    assert "FAIL" in rework["rationale"]


def test_program_seal_refuses_staged_product_change(repo):
    (repo / "product.txt").write_text("产品变更")
    git(repo, "add", "product.txt")
    before = head(repo)
    with pytest.raises(StageRefused, match="产品文件"):
        WorkspaceSealer(repo).materialize(SimpleNamespace(id="acceptance"), {}, StageOutcome())
    assert head(repo) == before


def test_program_seal_excludes_untracked_cache(repo):
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__/test.pyc").write_bytes(b"cache")
    machine = repo / ".dev-dispatch/new-proof.json"
    machine.parent.mkdir(exist_ok=True)
    machine.write_text("{}")
    WorkspaceSealer(repo).materialize(SimpleNamespace(id="acceptance"), {}, StageOutcome())
    assert "__pycache__" not in git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    assert ".dev-dispatch/new-proof.json" in git(repo, "ls-tree", "-r", "--name-only", "HEAD")


def test_configure_receipt_uses_actual_git_parent_after_recovery(repo, tmp_path):
    old_tail = head(repo)
    git(repo, "commit", "--allow-empty", "-qm", "代际恢复提交")
    actual_input = head(repo)
    git(repo, "commit", "--allow-empty", "-qm", "configure")
    configured = head(repo)
    plane = DdControlPlane(root=tmp_path / "dd", board_factory=lambda: None)
    record = {"development_id": "dev", "bootstrap_commit": old_tail, "root_handoff_digest": "root"}
    chain = plane._receipt_chain(
        record,
        repo,
        [{"stage": "configure", "event": "success", "output_commit": configured}],
        {},
        generation=2,
        seed_commit=old_tail,
    )
    expected = {"stage": "configure", "input_commit": actual_input, "output_commit": configured}
    assert chain[0]["receipt"] == expected
    assert chain[0]["receipt_digest"] == compute_json_digest(expected)
    assert chain[0]["input_commit"] == actual_input
