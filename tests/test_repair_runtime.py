"""复现运行态缺口：运行中无 result、命令启动失败、引擎审单恢复。"""

import json
from pathlib import Path

import pytest

from fleet_graph.dd.gate_store import GateStore
from fleet_graph.graphs.dd_runner import run_pipeline
from fleet_graph.graphs.dd_scripts import AcceptanceStage
from fleet_graph.scheduler.wake import LiveDdWakeFacts
from test_dd_runner import AgentRunStub, make_config, plugin_seals  # noqa: F401


def test_running_dd_without_result_parks_and_dead_runner_wakes(tmp_path: Path) -> None:
    root = tmp_path / "dev"
    root.mkdir()
    (root / "record.json").write_text(json.dumps({"generation": 2}))
    (root / "launches.jsonl").write_text(
        json.dumps(
            {
                "generation": 2,
                "started": True,
                "unit": "test-dd-r2",
            }
        )
        + "\n"
    )
    assert LiveDdWakeFacts(tmp_path, unit_probe=lambda _: True).dd_fact("dev") is None
    assert LiveDdWakeFacts(tmp_path, unit_probe=lambda _: False).dd_fact("dev") == "terminal"
    gen = root / "g2"
    gen.mkdir()
    (gen / "result.json").write_text(json.dumps({"awaiting": {"question_note_id": "q"}}))
    assert LiveDdWakeFacts(tmp_path).dd_fact("dev") == "awaiting_gate"


def test_command_start_failure_is_evidence(tmp_path: Path) -> None:
    result = AcceptanceStage(repo=tmp_path)._run([str(tmp_path / "missing-executable")])
    assert result["exit_code"] == 127
    assert result["error_type"] == "FileNotFoundError"
    assert "missing-executable" in result["stderr_tail"]


def test_command_timeout_is_evidence(tmp_path: Path) -> None:
    result = AcceptanceStage(repo=tmp_path, timeout_seconds=0.01)._run(["sleep", "1"])
    assert result["exit_code"] == 124
    assert result["error_type"] == "TimeoutExpired"


def test_phase_heartbeat_updates_during_call_and_stops() -> None:
    from threading import Event
    from types import SimpleNamespace

    from fleet_graph.graphs.phase_heartbeat import phase_heartbeat

    beat = Event()
    observed = []

    def heartbeat(round_no, phase, force=False):
        observed.append((round_no, phase, force))
        beat.set()

    with phase_heartbeat(SimpleNamespace(heartbeat=heartbeat), 2, "worker", interval=0.01):
        assert beat.wait(timeout=1)
    assert observed and observed[0] == (2, "worker", True)


def test_atomic_json_failure_preserves_old_value(tmp_path: Path, monkeypatch) -> None:
    from fleet_graph.state import run_artifacts

    path = tmp_path / "state.json"
    run_artifacts.write_json_durable(path, {"version": 1})

    def interrupted(*args):
        raise OSError("模拟提交中断")

    monkeypatch.setattr(run_artifacts.os, "replace", interrupted)
    with pytest.raises(OSError):
        run_artifacts.write_json_durable(path, {"version": 2})
    assert json.loads(path.read_text()) == {"version": 1}


@pytest.mark.parametrize("verdict, terminal", [("APPROVE", "complete"), ("REJECT", "refused")])
@pytest.mark.usefixtures("plugin_seals")
def test_engine_gate_waits_and_resumes_without_board(
    repo: Path, tmp_path: Path, verdict: str, terminal: str
) -> None:
    config = make_config(repo, tmp_path)
    config.record_path = str(tmp_path / "admission" / "record.json")
    config.checkpoint_path = str(tmp_path / "checkpoint.sqlite")
    config.run_config = {"acceptance_commands": [["true"]]}
    launcher = AgentRunStub({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
    first = run_pipeline(config, launcher=launcher)
    assert first["terminal"] is None
    request = first["awaiting"]["question_note_id"]
    store = GateStore(tmp_path / "admission" / "gate-requests")
    decision = dict(decision=verdict, decided_by="test-goal", reason="验证", action_key="action-1")
    assert store.decide(request, **decision) == store.decide(request, **decision)
    with pytest.raises(ValueError, match="不同裁决"):
        store.decide(
            request, **{**decision, "decision": "REJECT" if verdict == "APPROVE" else "APPROVE"}
        )
    resumed_launcher = AgentRunStub()
    final = run_pipeline(config, launcher=resumed_launcher, resume=True)
    assert final["terminal"] == terminal, final
    assert resumed_launcher.dispatched == []


def test_unsealed_retries_do_not_borrow_future_success_receipt(tmp_path):
    from fleet_graph.dd.control_plane import DdControlPlane

    plane = DdControlPlane(root=tmp_path, board_factory=lambda: None)
    record = {"development_id": "d", "bootstrap_commit": "base", "root_handoff_digest": "root"}
    history = [
        {"stage": "configure", "event": "success", "output_commit": "configured"},
        {"stage": "implement", "event": "failed", "output_commit": "configured"},
        {"stage": "implement", "event": "failed", "output_commit": "configured"},
        {"stage": "implement", "event": "success", "output_commit": "implemented"},
    ]
    calls = []

    def receipt(state, attempt, stage, repo, output, generation):
        calls.append((stage, output))
        return None, "", ""

    plane._sealed_receipt = receipt
    chain = plane._receipt_chain(record, tmp_path, history, {})
    assert calls == [("configure", "configured"), ("implement", "implemented")]
    assert len(chain) == 2
    assert chain[1]["parent_handoff_receipt_digest"] == chain[0]["receipt_digest"]
    # 真正改变 head 的失败仍必须出现在审计链上，不能一律跳过失败。
    history[1]["output_commit"] = "unexpected-change"
    calls.clear()
    chain = plane._receipt_chain(record, tmp_path, history, {})
    assert ("implement", "unexpected-change") in calls
