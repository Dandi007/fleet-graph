"""发布失败恢复必须复用原审查，不能制造通过或覆盖竞争提交。"""

import base64
import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.dd.vendor import plugin_adapter
from fleet_graph.graphs.dd_materializer import MaterializationFailed, PluginMaterializer
from fleet_graph.graphs.dd_pipeline import (
    Sealed,
    StageOutcome,
    build_dd_pipeline_graph,
    initial_state,
)
from fleet_graph.graphs.dd_publication_recovery import recover_review_publication
from test_dd_pipeline import ContractActor, Sealer, make_deps


@pytest.mark.parametrize("failures", [1, 2, 3])
def test_bounded_same_request_publication_retry(monkeypatch, failures):
    materializer = PluginMaterializer(None, None, None)
    request = {"frozen": "intent", "review_result": {"verdict": "APPROVE"}}
    calls = []
    monkeypatch.setattr(materializer, "request", lambda *args: request)
    failure = {k: None for k in plugin_adapter.IMPLEMENT_FAILURE_FIELDS}
    failure.update(failure_code="PUBLISH_FAILED", detail="TLS", retryable=True)

    def invoke(*args, **kwargs):
        calls.append(args[1])
        return failure if len(calls) <= failures else {"receipt": True}

    def read(stage, result):
        if result is failure:
            raise MaterializationFailed("PUBLISH_FAILED", "TLS", retryable=True)
        return Sealed(commit="a" * 40, receipt={"real": True})

    monkeypatch.setattr(plugin_adapter, "invoke_review_materializer", invoke)
    monkeypatch.setattr(materializer, "_read", read)
    stage = Lifecycle.load().stages["final_review"]
    if failures == 3:
        with pytest.raises(MaterializationFailed):
            materializer.materialize(stage, {}, StageOutcome(event="APPROVE"))
    else:
        assert materializer.materialize(stage, {}, StageOutcome(event="APPROVE")).commit == "a" * 40
    assert len(calls) == min(failures + 1, 3)
    assert all(call is request for call in calls)


def test_competing_remote_is_not_retried(monkeypatch):
    materializer = PluginMaterializer(None, None, None)
    monkeypatch.setattr(materializer, "request", lambda *args: {})
    calls = []
    failure = {k: None for k in plugin_adapter.IMPLEMENT_FAILURE_FIELDS}
    failure.update(failure_code="REMOTE_HEAD_CONFLICT", detail="competitor", retryable=False)

    def invoke(*args, **kwargs):
        calls.append(1)
        return failure

    monkeypatch.setattr(plugin_adapter, "invoke_review_materializer", invoke)
    with pytest.raises(MaterializationFailed):
        materializer.materialize(
            Lifecycle.load().stages["final_review"], {}, StageOutcome(event="APPROVE")
        )
    assert len(calls) == 1


def fixture(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (repo / "product").write_text("preserved product")
    git("add", ".")
    git("commit", "-qm", "product")
    parent = git("rev-parse", "HEAD")
    review = {"attempt_id": "original-attempt", "subject_commit": parent, "verdict": "APPROVE"}
    raw = json.dumps(review).encode()
    (repo / "review.json").write_bytes(raw)
    git("add", ".")
    git("commit", "-qm", "original FR")
    output = git("rev-parse", "HEAD")
    state_root = tmp_path / "g8" / "state"
    (state_root / "intents").mkdir(parents=True)
    intent_path = state_root / "intents" / "original.json"
    intent_path.write_text(
        json.dumps(
            dict(
                kind="review_materialization_intent",
                review_phase="final",
                development_id="dev-test",
                input_commit=parent,
                remote_url="remote",
                remote_ref="audit",
                artifact_bytes_base64=base64.b64encode(raw).decode(),
                artifact_digest="sha256:" + hashlib.sha256(raw).hexdigest(),
                attempt_id="original-attempt",
                verdict="APPROVE",
                artifact_path="review.json",
                materialization_intent_id="original",
            )
        )
    )
    config = SimpleNamespace(
        thread_id="dev-test:g8",
        development_id="dev-test",
        generation=8,
        state_root=state_root,
        run_root=tmp_path / "run",
        workspace_path=repo,
        remote_url="remote",
        audit_ref="audit",
        remote_ref="release",
    )
    return config, intent_path, parent, output, review


@pytest.mark.parametrize("conflict", [False, True])
def test_same_generation_original_fr_advances_acceptance_without_actor(tmp_path, conflict):
    config, intent, parent, output, review = fixture(tmp_path)
    actor = ContractActor()

    class PreservedSealer(Sealer):
        def materialize(self, stage, dispatch, outcome):
            if stage.id == "final_review":
                assert outcome.receipt == {"review_result": review}
                assert dispatch["input_commit"] == parent
                if conflict:
                    raise MaterializationFailed("REMOTE_HEAD_CONFLICT", "competing commit")
                return Sealed(
                    commit=output, receipt={"verdict": "APPROVE", "output_commit": output}
                )
            return super().materialize(stage, dispatch, outcome)

    deps = make_deps(actor=actor, materializer=PreservedSealer())
    compiled = build_dd_pipeline_graph(deps).compile(checkpointer=InMemorySaver())
    state = initial_state(
        development_id="dev-test",
        stage="final_review",
        head_commit=parent,
        artifacts={"spec": parent, "implementation": parent, "review": parent},
    )
    state.update(
        generation=8,
        attempt=2,
        steps=6,
        terminal="fault",
        fault=True,
        terminal_reason="materialize failed on final_review: PUBLISH_FAILED: TLS",
    )
    checkpoint = {"configurable": {"thread_id": config.thread_id}}
    compiled.update_state(checkpoint, state, as_node="run_stage")
    before = compiled.get_state(checkpoint)
    assert not before.next
    if conflict:
        with pytest.raises(MaterializationFailed):
            recover_review_publication(compiled, deps, config, intent, output)
        assert compiled.get_state(checkpoint).config == before.config
        assert actor.calls == []
        assert not (config.run_root / "publication-recovery.json").exists()
    else:
        recover_review_publication(compiled, deps, config, intent, output)
        recovered = compiled.get_state(checkpoint)
        assert recovered.next == ("advance",)
        result = compiled.invoke(None, config={**checkpoint, "recursion_limit": 100})
        assert actor.calls[0][0] == "acceptance"
        assert not any(stage in {"implement", "final_review"} for stage, _ in actor.calls)
        assert result["generation"] == 8
        assert result["terminal"] == "complete"
        receipt = json.loads((config.run_root / "publication-recovery.json").read_text())
        assert receipt["output_commit"] == output


@pytest.mark.parametrize("scenario", ["unsent", "lost_ack", "competing"])
def test_pinned_plugin_exact_old_reconciliation(monkeypatch, scenario):
    import ast
    import os
    from pathlib import Path

    source = os.environ.get("FLEET_TEST_PINNED_PLUGIN")
    if not source:
        pytest.skip("requires explicitly selected pinned plugin source")
    tree = ast.parse(Path(source).read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "protocol_publish_commit"
    )
    heads = iter(
        {"unsent": ["old", "old"], "lost_ack": ["old", "new", "new"], "competing": ["competitor"]}[
            scenario
        ]
    )
    pushes = []

    class Failure(Exception):
        pass

    def fail(code, detail, retryable=False):
        raise Failure(code, retryable)

    def push(*args):
        pushes.append(args)
        return SimpleNamespace(returncode=128, stderr=b"TLS handshake terminated")

    ns = dict(
        protocol_git=lambda *a, **k: SimpleNamespace(returncode=0),
        protocol_remote_head=lambda *a: next(heads),
        protocol_remote_git=push,
        protocol_fail=fail,
        PROTOCOL_REMOTE_NAME="origin",
    )
    exec(compile(ast.Module(body=[function], type_ignores=[]), source, "exec"), ns)
    if scenario == "lost_ack":
        ns["protocol_publish_commit"]("worktree", "old", "new", "remote", "audit")
    else:
        with pytest.raises(Failure) as error:
            ns["protocol_publish_commit"]("worktree", "old", "new", "remote", "audit")
        assert error.value.args == (
            ("PUBLISH_FAILED", True) if scenario == "unsent" else ("REMOTE_HEAD_CONFLICT", False)
        )
    assert len(pushes) == (0 if scenario == "competing" else 1)
    if pushes:
        assert "--force-with-lease=audit:old" in pushes[0]
