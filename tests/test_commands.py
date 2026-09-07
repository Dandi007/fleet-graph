"""验收执行器的纯 fake 进程测试，不执行真实命令。"""

import asyncio
import json
import signal
from types import SimpleNamespace

import pytest

from fleet_graph import commands
from fleet_graph.commands import Commands, worker, write_json


def test_spawn_idempotency_input_binding_and_recover(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        commands.subprocess,
        "Popen",
        lambda *a, **kw: calls.append((a, kw)) or SimpleNamespace(pid=123),
    )
    monkeypatch.setattr(commands, "identity", lambda pid: "boot:123")
    port = Commands(tmp_path)
    ticket = asyncio.run(port.start("run", "/workspace", ["command"]))
    assert asyncio.run(port.start("run", "/workspace", ["command"])) == ticket
    assert len(calls) == 1
    assert asyncio.run(port.recover("run")) == ticket
    assert asyncio.run(port.recover("absent")) is None
    with pytest.raises(ValueError):
        asyncio.run(port.start("run", "/workspace", ["different"]))
    with pytest.raises(ValueError):
        asyncio.run(port.start("../bad", "/workspace", ["command"]))


def test_spawn_failure_is_lost_not_acceptance_failure(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(commands.subprocess, "Popen", fail)
    port = Commands(tmp_path)
    ticket = asyncio.run(port.start("run", "/workspace", ["command"]))
    assert asyncio.run(port.inspect(ticket))["status"] == "lost"


def test_reused_pid_is_never_signalled(tmp_path, monkeypatch):
    folder = tmp_path / "run"
    folder.mkdir()
    write_json(folder / "process.json", {"pid": 12, "identity": "old"})
    monkeypatch.setattr(commands, "identity", lambda pid: "new")
    monkeypatch.setattr(commands.os, "killpg", lambda *args: pytest.fail("禁止杀复用 PID"))
    assert asyncio.run(Commands(tmp_path).stop({"run_id": "run"}))["stopped"] is False


@pytest.mark.parametrize("interrupted", [False, True])
def test_worker_cleans_command_group_even_during_spawn(tmp_path, monkeypatch, interrupted):
    write_json(
        tmp_path / "input.json", {"workspace": "/workspace", "commands": ["fake"], "timeout": 1}
    )
    handlers, killed = {}, []

    def set_signal(sig, fn):
        old = handlers.get(sig)
        handlers[sig] = fn
        return old

    monkeypatch.setattr(commands.signal, "signal", set_signal)
    monkeypatch.setattr(commands, "identity", lambda pid: "identity")
    monkeypatch.setattr(commands.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    class Child:
        pid = 222

        def wait(self, timeout=None):
            return 0

    def spawn(*args, **kwargs):
        if interrupted:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        return Child()

    monkeypatch.setattr(commands.subprocess, "Popen", spawn)
    worker(tmp_path)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == ("lost" if interrupted else "succeeded")
    assert (222, signal.SIGKILL) in killed


def test_timeout_kills_child_and_records_exit_124(tmp_path, monkeypatch):
    write_json(
        tmp_path / "input.json", {"workspace": "/workspace", "commands": ["fake"], "timeout": 1}
    )
    monkeypatch.setattr(commands.signal, "signal", lambda *args: None)
    monkeypatch.setattr(commands, "identity", lambda pid: "identity")
    killed = []
    monkeypatch.setattr(commands.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    times = iter([0, 2])
    monkeypatch.setattr(commands.time, "monotonic", lambda: next(times))

    class Child:
        pid = 333

        def wait(self, timeout=None):
            return -9

    monkeypatch.setattr(commands.subprocess, "Popen", lambda *a, **kw: Child())
    worker(tmp_path)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "failed"
    assert result["results"][0]["exit_code"] == 124
    assert (333, signal.SIGKILL) in killed
