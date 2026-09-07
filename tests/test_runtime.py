"""Runtime bridge 单元测试：使用内存 CLI 替身，不启动引擎或模型。"""

import asyncio
import json

import pytest

from fleet_graph.runtime import RoleConfig, Runtime, RuntimeErrorContract


class FakeRuntime(Runtime):
    def __init__(self, root, **kwargs):
        super().__init__(
            agent_run="/group/bin/agent-run",
            agent_session="/group/bin/agent-session",
            state_root=str(root / "bridge"),
            session_root=str(root / "sessions"),
            roles=kwargs.get("roles", {"worker": RoleConfig("codex", "test", "readonly")}),
        )
        self.commands = []
        self.status = {"run_id": "run-1", "state": "running"}
        self.fail_spawn = False

    async def _command(self, argv):
        self.commands.append(argv)
        if argv[1] == "status":
            return [self.status]
        if argv[1] == "history":
            return {"items": [{"event": {"type": "tool_call"}}], "next_offset": 1, "total": 2}
        if argv[1] == "stop":
            return {"run_id": argv[2], "state": "stopping"}
        if self.fail_spawn:
            raise RuntimeErrorContract("模拟 spawn 后、回执前崩溃")
        return {"run_id": argv[argv.index("--run-id") + 1], "state": "running", "pid": 100}


def start(runtime, **changes):
    request = dict(
        run_id="run-1",
        role="worker",
        workspace="/group/work",
        prompt={"task": "测试"},
        output_schema={"type": "array"},
        session_scope="goal-1",
    )
    request.update(changes)
    return asyncio.run(runtime.start(**request))


def test_start_is_durable_and_idempotent_after_restart(tmp_path):
    runtime = FakeRuntime(tmp_path)
    ticket = start(runtime)
    restored = FakeRuntime(tmp_path)
    adopted = start(restored)
    assert ticket["adopted"] is False
    assert adopted["adopted"] is True
    assert restored.commands == []
    intent = json.loads((tmp_path / "bridge/run-1/intent.json").read_text())
    assert intent["stage"] == "started"
    assert intent["launch"]["pid"] == 100
    assert "--output-schema" in runtime.commands[0]
    assert "--hooks" in runtime.commands[0]
    assert "--write" not in runtime.commands[0]


def test_spawn_window_never_blindly_relaunches(tmp_path):
    runtime = FakeRuntime(tmp_path)
    runtime.fail_spawn = True
    with pytest.raises(RuntimeErrorContract):
        start(runtime)
    restored = FakeRuntime(tmp_path)
    ticket = start(restored)
    restored.status["state"] = "unknown"
    status = asyncio.run(restored.inspect(ticket))
    assert status["status"] == "lost"
    assert status["error"] == "launch_uncertain"
    assert all(command[1] == "status" for command in restored.commands)


def test_run_id_request_binding_and_path_validation(tmp_path):
    runtime = FakeRuntime(tmp_path)
    start(runtime)
    with pytest.raises(RuntimeErrorContract, match="不同请求"):
        start(runtime, prompt={"task": "另一个请求"})
    with pytest.raises(ValueError):
        start(runtime, run_id="../escaped")


def test_concurrent_start_dispatches_once(tmp_path):
    runtime = FakeRuntime(tmp_path)

    async def concurrent():
        args = ("run-1", "worker", "/group/work", {}, {}, "scope")
        return await asyncio.gather(runtime.start(*args), runtime.start(*args))

    tickets = asyncio.run(concurrent())
    assert sum(not ticket["adopted"] for ticket in tickets) == 1
    assert len(runtime.commands) == 1


def test_protocol_failure_is_not_business_verdict(tmp_path):
    runtime = FakeRuntime(tmp_path)
    ticket = start(runtime)
    runtime.status.update(state="succeeded", result={"stdout": "looks good"})
    status = asyncio.run(runtime.inspect(ticket))
    assert status["status"] == "failed"
    assert status["output"] is None
    assert status["error"] == "protocol_error"


def test_success_preserves_array_and_scope_resume(tmp_path):
    roles = {"worker": RoleConfig("codex", "test", "readonly", session_policy="resume")}
    runtime = FakeRuntime(tmp_path, roles=roles)
    ticket = start(runtime)
    output = [{"type": "result", "verdict": "pass"}]
    runtime.status.update(
        state="succeeded",
        result={"structured_result": output, "run_dir": "/group/sessions/previous"},
    )
    assert asyncio.run(runtime.inspect(ticket))["output"] == output
    start(runtime, run_id="run-2")
    assert "--resume" in runtime.commands[-1]
    assert "/group/sessions/previous" in runtime.commands[-1]


def test_session_retains_raw_events_and_pagination(tmp_path):
    runtime = FakeRuntime(tmp_path)
    ticket = start(runtime)
    page = asyncio.run(runtime.session(ticket, 10, 20))
    assert page["items"][0]["event"]["type"] == "tool_call"
    assert runtime.commands[-1][-4:] == ["--offset", "10", "--limit", "20"]
    with pytest.raises(ValueError):
        asyncio.run(runtime.session(ticket, -1))
    assert asyncio.run(runtime.stop(ticket))["state"] == "stopping"


def test_readonly_review_role_cannot_request_write(tmp_path):
    with pytest.raises(ValueError, match="只读"):
        FakeRuntime(tmp_path, roles={"scribe": RoleConfig("codex", "test", "worker", write=True)})


def test_schema_rejection_never_becomes_success(tmp_path):
    runtime = FakeRuntime(tmp_path)
    ticket = start(
        runtime,
        output_schema={
            "type": "array",
            "items": {
                "type": "object",
                "required": ["verdict"],
                "properties": {"verdict": {"enum": ["pass"]}},
            },
        },
    )
    runtime.status.update(state="succeeded", result={"structured_result": [{"verdict": "made-up"}]})
    status = asyncio.run(runtime.inspect(ticket))
    assert status["status"] == "failed"
    assert status["error"] == "protocol_error"
    assert status["output"] is None


def test_cli_ticket_validation(tmp_path):
    runtime = FakeRuntime(tmp_path)
    ticket = start(runtime)
    runtime.status["run_id"] = "another-run"
    with pytest.raises(RuntimeErrorContract, match="不匹配"):
        asyncio.run(runtime.inspect(ticket))


def test_compact_is_explicit_and_fresh_never_resumes(tmp_path):
    roles = {
        "worker": RoleConfig(
            "claude", "test", "readonly", session_policy="compact", compact_after=10000
        )
    }
    runtime = FakeRuntime(tmp_path, roles=roles)
    ticket = start(runtime)
    runtime.status.update(
        state="succeeded", result={"structured_result": [], "run_dir": "/group/sessions/previous"}
    )
    asyncio.run(runtime.inspect(ticket))
    start(runtime, run_id="run-2")
    assert "--compact" in runtime.commands[-1]
    assert runtime.commands[-1][-2:] == ["--compact-after", "10000"]
    runtime.roles["worker"] = RoleConfig("claude", "test", "readonly", session_policy="fresh")
    start(runtime, run_id="run-3")
    assert "--resume" not in runtime.commands[-1]


def test_system_prompt_and_readonly_recovery(tmp_path):
    roles = {
        "worker": RoleConfig(
            "codex", "test", "readonly", system_prompt_file="/group/prompts/worker.md"
        )
    }
    runtime = FakeRuntime(tmp_path, roles=roles)
    assert asyncio.run(runtime.recover("run-1")) is None
    start(runtime)
    assert runtime.commands[0][-2:] == ["--system-prompt-file", "/group/prompts/worker.md"]
    recovered = asyncio.run(runtime.recover("run-1"))
    assert recovered["adopted"] is True
    assert len(runtime.commands) == 1


def test_detach_parent_lost_does_not_prove_descendants_absent(tmp_path):
    runtime = FakeRuntime(tmp_path)
    ticket = start(runtime)
    runtime.status["state"] = "lost"
    result = asyncio.run(runtime.inspect(ticket))
    assert result["status"] == "lost"
    assert result["error"] == "launch_uncertain"
