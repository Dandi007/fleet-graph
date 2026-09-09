"""独立验收：真实审单节点、控制面、本地裁决与持久化 pipeline 串联。"""

import json
from pathlib import Path

import pytest

from fleet_graph.bus.board import GateTicket
from fleet_graph.dd.control_plane import CHECKPOINT_FILE, DdControlPlane
from fleet_graph.dd.gate_store import GateStore
from fleet_graph.dd.service import GateAutoResumer
from fleet_graph.graphs.dd_gate import GraphGateNode
from fleet_graph.graphs.dd_runner import run_pipeline
from test_dd_control_plane import RecordingLauncher
from test_dd_runner import AgentRunStub, make_config, plugin_seals  # noqa: F401
from test_m2_dd_gate_delivery import _passing_evidence


@pytest.fixture
def waiting_gate(repo: Path, tmp_path: Path, plugin_seals, monkeypatch):  # noqa: F811
    config = make_config(repo, tmp_path)
    root = tmp_path / "dd"
    admission = root / config.development_id
    admission.mkdir(parents=True)
    config.record_path = str(admission / "record.json")
    config.checkpoint_path = str(admission / CHECKPOINT_FILE)
    config.run_root = admission
    config.run_config = {"acceptance_commands": [["true"]]}
    record = {
        "development_id": config.development_id,
        "generation": 1,
        "repo_path": str(repo),
        "remote_url": config.remote_url,
        "remote_ref": config.remote_ref,
        # The gate's validity binding (spec L3) needs the order-private audit
        # branch alongside the durable target -- an attributed single carries both.
        "audit_ref": config.audit_ref,
        "target_base_commit": config.target_base_commit,
        "root_handoff_digest": config.root_handoff_digest,
        "bootstrap_commit": config.head_commit,
        "spec_digest": "fixture",
        "acceptance_commands": [["true"]],
        "dispatched_by": "wf-owner",
    }
    Path(config.record_path).write_text(json.dumps(record))
    result = run_pipeline(
        config,
        launcher=AgentRunStub({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}),
    )
    assert result["awaiting"]
    plane = DdControlPlane(root=root, board_factory=lambda: None, unit_probe=lambda _: False)
    launches = []

    def launch(record, *, resume, generation):
        assert resume and generation == 1
        launcher = AgentRunStub()
        final = run_pipeline(config, launcher=launcher, resume=True)
        assert launcher.dispatched == []
        launches.append(final)
        return {"generation": generation, "mode": "resume", "unit": "fixture"}

    monkeypatch.setattr(plane, "_launch", launch)
    node = GraphGateNode(plane, evidence=_passing_evidence())
    action = {
        "kind": "dd.gate_release.v1",
        "idempotency_key": "review-one",
        "payload": {
            "development_id": config.development_id,
            "generation": 1,
            "question_note_id": result["awaiting"]["question_note_id"],
            "decided_by": "wf-owner",
            "verdict": "APPROVE",
        },
    }
    return node, plane, config, action, launches


def test_owner_approve_reaches_merge(waiting_gate):
    node, plane, config, action, launches = waiting_gate
    receipt = node.consume(action, folder_id="wf-owner", round_no=1)
    assert receipt["status"] == "consumed", receipt.get("detail", receipt)
    assert launches[0]["terminal"] == "complete"
    assert plane.get(config.development_id)["state"] == "complete"


def test_foreign_line_cannot_impersonate_dispatcher(waiting_gate):
    node, plane, config, action, launches = waiting_gate
    receipt = node.consume(action, folder_id="wf-foreign", round_no=1)
    assert receipt["status"] == "failed", receipt
    assert launches == []
    assert plane.get(config.development_id)["state"] == "awaiting_gate"


def test_stale_request_cannot_release_current_gate(waiting_gate):
    node, plane, config, action, launches = waiting_gate
    action["payload"]["question_note_id"] = "engine:dd-gate:dev-001:g0"
    action["payload"]["generation"] = 0
    receipt = node.consume(action, folder_id="wf-owner", round_no=1)
    assert receipt["status"] == "failed", receipt.get("detail", receipt)
    assert launches == []
    assert plane.get(config.development_id)["state"] == "awaiting_gate"


def test_redelivery_recovers_after_publish_before_resume(waiting_gate, monkeypatch):
    node, plane, config, action, launches = waiting_gate
    actual_launch = plane._launch

    def interrupted_launch(*args, **kwargs):
        raise RuntimeError("模拟裁决已持久化但进程尚未启动时崩溃")

    monkeypatch.setattr(plane, "_launch", interrupted_launch)
    first = node.consume(action, folder_id="wf-owner", round_no=1)
    assert first["status"] == "failed"
    assert launches == []
    store = GateStore(Path(config.record_path).parent / "gate-requests")
    ticket = GateTicket(question_note_id=action["payload"]["question_note_id"], card_entity_id="")
    committed = store.decision_for(ticket)
    assert committed is not None
    monkeypatch.setattr(plane, "_launch", actual_launch)
    replay = node.consume(action, folder_id="wf-owner", round_no=2)
    assert replay["status"] == "consumed", replay.get("detail", replay)
    assert len(launches) == 1
    assert launches[0]["terminal"] == "complete"
    assert store.decision_for(ticket).message_id == committed.message_id


def test_auto_resumer_does_not_run_before_workspace_seal(waiting_gate, monkeypatch):
    node, plane, _config, action, launches = waiting_gate
    actual_seal = node._seal_decision_file
    observations = []

    def seal_after_poll(**kwargs):
        observations.append(GateAutoResumer(plane).tick())
        assert launches == [], "本地裁决可见但 workspace seal 尚未提交，不应恢复"
        return actual_seal(**kwargs)

    monkeypatch.setattr(node, "_seal_decision_file", seal_after_poll)
    receipt = node.consume(action, folder_id="wf-owner", round_no=1)
    assert observations
    assert observations[0]["resumed"] == []
    assert receipt["status"] == "consumed", receipt.get("detail", receipt)
    assert len(launches) == 1


def test_auto_resumer_recovers_after_seal_before_launch(waiting_gate, monkeypatch):
    node, plane, config, action, launches = waiting_gate
    actual_launch = plane._launch

    def interrupted_launch(*args, **kwargs):
        raise RuntimeError("模拟 seal 已提交后启动失败")

    monkeypatch.setattr(plane, "_launch", interrupted_launch)
    receipt = node.consume(action, folder_id="wf-owner", round_no=1)
    assert receipt["status"] == "failed"
    assert launches == []
    monkeypatch.setattr(plane, "_launch", actual_launch)
    polled = GateAutoResumer(plane).tick()
    assert polled["resumed"] == [config.development_id], polled
    assert len(launches) == 1
    assert launches[0]["terminal"] == "complete"


def test_publish_replay_keeps_original_evidence_after_seal_crash(waiting_gate, monkeypatch):
    node, _plane, config, action, launches = waiting_gate
    actual_seal = node._seal_decision_file

    def interrupted_seal(**kwargs):
        raise RuntimeError("模拟 publish 后 seal 前崩溃")

    monkeypatch.setattr(node, "_seal_decision_file", interrupted_seal)
    first = node.consume(action, folder_id="wf-owner", round_no=1)
    assert first["status"] == "failed"
    store = GateStore(Path(config.record_path).parent / "gate-requests")
    ticket = GateTicket(question_note_id=action["payload"]["question_note_id"], card_entity_id="")
    original = store.decision_for(ticket)
    assert original is not None
    from dataclasses import replace

    node._evidence = [
        replace(item, detail=item.detail + "；本次运行耗时变化") for item in _passing_evidence()
    ]
    monkeypatch.setattr(node, "_seal_decision_file", actual_seal)
    replay = node.consume(action, folder_id="wf-owner", round_no=2)
    assert replay["status"] == "consumed", replay.get("detail", replay)
    assert len(launches) == 1
    assert store.decision_for(ticket).rationale == original.rationale


def test_reject_preserves_problem_in_consumed_decision(waiting_gate, monkeypatch):
    node, plane, config, action, launches = waiting_gate
    problem = "并发写覆盖另一个调用的结果"
    suggestion = "按请求标识隔离写入并添加竞争测试"
    consequence = "成功回执可能对应丢失的数据"
    action["payload"].update(
        verdict="REJECT",
        board_decision={
            "problem": problem,
            "suggested_answer": suggestion,
            "cost_of_no_answer": consequence,
        },
    )
    receipt = node.consume(action, folder_id="wf-owner", round_no=1)
    assert receipt["status"] == "consumed", receipt.get("detail", receipt)
    assert launches[0]["terminal"] == "refused"
    store = GateStore(Path(config.record_path).parent / "gate-requests")
    decision = store.decision_for(
        GateTicket(question_note_id=action["payload"]["question_note_id"], card_entity_id="")
    )
    for text in (problem, suggestion, consequence):
        assert text in decision.rationale
    sealed = json.loads((config.workspace_path / ".dev-dispatch/gate/decision-g1.json").read_text())
    for text in (problem, suggestion, consequence):
        assert text in sealed["rationale"]
    rework = plane._seal_gate_rework(json.loads(Path(config.record_path).read_text()), 2)
    assert rework["decision_message_id"] == decision.message_id
    for text in (problem, suggestion, consequence):
        assert text in rework["rationale"]
    binding = Path(config.record_path).parent / "plugin-binding.json"
    binding.write_text("{}")
    record = json.loads(Path(config.record_path).read_text())
    record["plugin_binding_path"] = str(binding)
    Path(config.record_path).write_text(json.dumps(record))
    plane.plugin_binding = binding
    recorded_launches = RecordingLauncher()
    plane.launcher = recorded_launches
    monkeypatch.setattr(plane, "_launch", DdControlPlane._launch.__get__(plane))
    started = plane.start(config.development_id)
    assert started["generation"] == 2
    launch_spec = recorded_launches.specs[0]
    assert not launch_spec.resume
    assert launch_spec.gate_reject_file
    config.generation = 2
    config.run_root = launch_spec.run_root
    config.gate_reject = json.loads(Path(launch_spec.gate_reject_file).read_text())
    from conftest import head

    config.head_commit = head(config.workspace_path)
    launcher = AgentRunStub({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
    second = run_pipeline(config, launcher=launcher)
    assert "implement" in launcher.dispatched
    assert second["awaiting"]["question_note_id"].endswith(":g2"), second
    prompt = (config.run_root / "stages/implement-g2-a1-prompt.md").read_text()
    assert decision.message_id in prompt
    for text in (problem, suggestion, consequence):
        assert text in prompt
