"""MCP 控制面进程级 e2e（dd-37）：真 stdio JSON-RPC 管道 + 真 Popen spawn。

补 GO-10 / protocol §10 缺的最后一层证据：dd-22 的 17 个单测都是
``handle_request`` 纯函数级（spawner 是 stub），dd-26 的 e2e 直接调
``engine.run_engine``、绕过整条 MCP 路径。本文件两段都不起真 agent、不联网：

1. **进程级传输 e2e**：用 ``subprocess.Popen`` 起一个真 ``mcpserver.main``
   服务器（stdin/stdout 都是管道），逐行喂 JSON-RPC——``initialize`` →
   ``tools/list`` → 三个只读工具（空根 ``goal_list`` → ``[]``；预置
   ``events.jsonl`` 的 ``goal_status``；预置 ``observations.jsonl`` 的
   ``goal_observations``）→ 一行非法 JSON 与一个未知 method（断言 error
   形状且服务器不退出、后续请求仍能应答）。每次读响应都有超时上界
   （selector + deadline），绝不允许挂死。

2. **真 spawn e2e（进程内）**：真 git 世界（本地 bare origin + 真 clone +
   真 linked worktree，remote 指向本地 bare 路径，acceptance 用 ``true``），
   ``ServerContext(spawner=mcpserver.popen_spawner)`` 用真 spawner、真
   ``start_new_session``；只 monkeypatch ``engine_argv`` 指向一个往
   ``events.jsonl`` 追加一行的 stub 命令，走一次真 ``goal_enroll``，断言
   spawn 结果（pid / engine.log）与 stub 写下的 event 能被随后的
   ``goal_list`` / ``goal_status`` 读出来。结束时子进程全部 reap。
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.minimal import mcpserver, mcptools, runroot
from fleet_graph.minimal.events import EventLog
from fleet_graph.minimal.mcpserver import ServerContext, handle_request

# 子进程 ``-c`` 脚本靠它 import fleet_graph：裸 pytest 的解释器没装本包，
# pyproject 的 pythonpath=["src"] 只对 pytest 进程生效。
_SRC = str(Path(__file__).resolve().parents[1] / "src")

_READ_TIMEOUT_S = 10.0

_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

# stub 引擎进程：往 argv[1] 的 events.jsonl 追加恰好一行 engine.started
# event（payload 带自己的 pid），随后立刻退出。最小脚本、零第三方依赖。
_STUB_ENGINE_SCRIPT = r"""
import json, os, sys
from datetime import datetime, timezone
events_path, goal_id = sys.argv[1], sys.argv[2]
event = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "goal_id": goal_id,
    "dd_id": None,
    "kind": "engine.started",
    "seq": 1,
    "payload": {"pid": os.getpid()},
}
with open(events_path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
"""


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.name=e2e",
            "-c",
            "user.email=e2e@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=True,
    )
    return proc.stdout.strip()


def _rpc(request_id: Any, method: str, params: dict | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    return request


def _call_tool(ctx: ServerContext, request_id: Any, name: str, arguments: dict) -> dict[str, Any]:
    return handle_request(
        _rpc(request_id, "tools/call", {"name": name, "arguments": arguments}), ctx
    )


# ---------------------------------------------------------------------------
# 段 1：真 stdio 管道上的 JSON-RPC 传输
# ---------------------------------------------------------------------------


class StdioServer:
    """一个真 ``mcpserver.main`` 进程（stdin/stdout 管道）。

    ``read_line`` 是 selector + deadline 的逐行读：管道上永远有超时上界，
    服务器死了或不应答时给出带 stderr 的诊断信息而不是挂死。
    """

    def __init__(self, engine_root: Path, stderr_path: Path) -> None:
        script = (
            "import sys\n"
            f"sys.path.insert(0, {_SRC!r})\n"
            "from fleet_graph.minimal import mcpserver\n"
            "mcpserver.main(sys.argv[1:])\n"
        )
        self._stderr_path = stderr_path
        self._stderr_file = stderr_path.open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", script, "--engine-root", str(engine_root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
        )
        self._stdout_fd = self.proc.stdout.fileno()
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._stdout_fd, selectors.EVENT_READ)
        self._buf = bytearray()
        self.returncode: int | None = None

    def __enter__(self) -> StdioServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _diagnose(self, what: str) -> str:
        try:
            stderr = self._stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stderr = "<unreadable>"
        return f"{what}; returncode={self.proc.poll()} stderr={stderr!r}"

    def send_line(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def read_line(self, *, timeout_s: float = _READ_TIMEOUT_S) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line = bytes(self._buf[: newline + 1])
                del self._buf[: newline + 1]
                return line.decode("utf-8").rstrip("\n")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._selector.select(remaining):
                raise AssertionError(self._diagnose("timed out waiting for a response line"))
            chunk = os.read(self._stdout_fd, 65536)
            assert chunk, self._diagnose("server closed stdout before a full line arrived")
            self._buf += chunk

    def request(
        self,
        request_id: Any,
        method: str,
        params: dict | None = None,
        *,
        timeout_s: float = _READ_TIMEOUT_S,
    ) -> dict[str, Any]:
        self.send_line(json.dumps(_rpc(request_id, method, params)))
        return json.loads(self.read_line(timeout_s=timeout_s))

    def close(self, *, timeout_s: float = _READ_TIMEOUT_S) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()  # EOF → serve 的 for-line 循环结束 → main 返回 0
        try:
            self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=timeout_s)
        finally:
            self._selector.close()
            self._stderr_file.close()
        self.returncode = self.proc.returncode


def test_stdio_transport_end_to_end(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    engine_root.mkdir()

    with StdioServer(engine_root, tmp_path / "server.stderr") as server:
        # initialize：握手返回协议版本与服务器信息
        resp = server.request(1, "initialize", {})
        assert resp["jsonrpc"] == "2.0" and resp["id"] == 1, resp
        result = resp["result"]
        assert result["protocolVersion"] == mcpserver.PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == mcpserver.SERVER_INFO["name"]
        assert "tools" in result["capabilities"]

        # tools/list：工具名与 mcptools.TOOL_NAMES 逐一相等（含顺序）
        resp = server.request(2, "tools/list")
        assert [tool["name"] for tool in resp["result"]["tools"]] == list(mcptools.TOOL_NAMES)

        # goal_list：空根 → []
        resp = server.request(3, "tools/call", {"name": "goal_list", "arguments": {}})
        assert resp["result"]["structuredContent"] == []
        assert resp["result"]["isError"] is False

        # 预置一个 goal 的 events.jsonl / observations.jsonl（服务器每次调用
        # 都现读磁盘，之后 goal_status / goal_observations 看得见）
        goal_root = engine_root / "g-000001"
        elog = EventLog(goal_root)
        elog.append("goal.enrolled", {"title": "stdio e2e 预置", "work_folder": "wf-e2e001"})
        elog.append("engine.started", {"pid": os.getpid()})  # 测试进程活着 → 状态确定是 running
        (goal_root / "observations.jsonl").write_text(
            json.dumps(
                {"ts": "2026-09-09T00:00:00+00:00", "severity": "info", "note": "obs-1"},
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {"ts": "2026-09-09T01:00:00+00:00", "severity": "warn", "note": "obs-2"},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        # goal_status：对预置 events.jsonl 派生
        resp = server.request(
            4, "tools/call", {"name": "goal_status", "arguments": {"goal_id": "g-000001"}}
        )
        view = resp["result"]["structuredContent"]
        assert view["goal_id"] == "g-000001"
        assert view["state"] == "running"
        assert [ev["kind"] for ev in view["tail"]] == ["goal.enrolled", "engine.started"]

        # goal_observations：对预置 observations.jsonl（全量 + severity 过滤）
        resp = server.request(
            5, "tools/call", {"name": "goal_observations", "arguments": {"goal_id": "g-000001"}}
        )
        assert [obs["note"] for obs in resp["result"]["structuredContent"]] == ["obs-1", "obs-2"]
        resp = server.request(
            6,
            "tools/call",
            {"name": "goal_observations", "arguments": {"goal_id": "g-000001", "severity": "warn"}},
        )
        assert [obs["note"] for obs in resp["result"]["structuredContent"]] == ["obs-2"]

        # 一行非法 JSON → parse error（id 为 null），服务器不退出
        server.send_line('{"jsonrpc": "2.0", "id": 99')
        resp = json.loads(server.read_line())
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] is None
        assert resp["error"]["code"] == mcpserver.PARSE_ERROR
        assert resp["error"]["message"]

        # 未知 method → method not found，服务器不退出
        resp = server.request(7, "no/such/method")
        assert resp["jsonrpc"] == "2.0" and resp["id"] == 7
        assert resp["error"]["code"] == mcpserver.METHOD_NOT_FOUND
        assert "no/such/method" in resp["error"]["message"]

        # 两种错误之后服务器仍能应答
        resp = server.request(8, "tools/list")
        assert [tool["name"] for tool in resp["result"]["tools"]] == list(mcptools.TOOL_NAMES)

    # stdin EOF → serve 循环结束、进程干净退出
    assert server.returncode == 0


# ---------------------------------------------------------------------------
# 段 2：goal_enroll → 真 popen_spawner spawn（进程内 ServerContext）
# ---------------------------------------------------------------------------

_test_goal_enroll_real_spawn = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="real git world needs git, and enroll validation probes acceptance with bash -n",
)


@_test_goal_enroll_real_spawn
def test_goal_enroll_spawns_real_engine_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 真 git 世界：本地 bare origin + 真 clone + 真 linked worktree（无网络）
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(repo)],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=True,
    )
    (repo / "README.md").write_text("mcp e2e\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed on main")
    _git(repo, "push", "-q", "-u", "origin", "main")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", str(worktree), "-b", "wt-1")
    # remote 指本地 bare 路径（worktree 与 clone 共享同一 remote 配置）
    assert _git(worktree, "remote", "get-url", "origin") == str(origin)

    engine_root = tmp_path / "engine"
    goal_id = "g-0e2e01"

    def stub_engine_argv(stub_goal_id: str, stub_engine_root: str | Path) -> list[str]:
        events_path = (
            runroot.goal_run_root(stub_goal_id, engine_root=stub_engine_root).root / "events.jsonl"
        )
        return [sys.executable, "-c", _STUB_ENGINE_SCRIPT, str(events_path), stub_goal_id]

    monkeypatch.setattr(mcpserver, "engine_argv", stub_engine_argv)

    # 真 spawner、真 start_new_session；alive_probe 固定 False 让后续断言确定
    ctx = ServerContext(
        engine_root=engine_root,
        spawner=mcpserver.popen_spawner,
        alive_probe=lambda pid: False,
    )

    enroll = {
        "schema": "goal.enroll/2",
        "work_folder": "wf-e2e001",
        "title": "mcp e2e：真 spawn 一次引擎进程",
        "goal_text": "验证 goal_enroll 经真 stdio 控制面用真 Popen spawn 引擎进程。",
        "source_branch": "release/e2e",
        "repos": [
            {
                "path": str(worktree),
                "remote": "origin",
                "target_branch": "main",
                "acceptance": ["true"],
            }
        ],
    }
    resp = _call_tool(ctx, 1, "goal_enroll", {"enroll": enroll, "goal_id": goal_id})
    assert "error" not in resp, resp
    result = resp["result"]["structuredContent"]
    assert result["spawned"] is True
    assert result["goal_id"] == goal_id
    assert result["action"] == "enroll"
    assert isinstance(result["pid"], int) and result["pid"] > 0
    assert result["argv"] == stub_engine_argv(goal_id, engine_root)
    log_path = Path(result["engine_log"])
    assert log_path == engine_root / goal_id / "engine.log"
    assert log_path.is_file()
    # MCP 的写只有 spawn 这一件：没写 control.jsonl、没写 goal.enroll.json
    assert not (engine_root / goal_id / "control.jsonl").exists()
    assert not (engine_root / goal_id / "goal.enroll.json").exists()

    pid = result["pid"]

    # 等 stub 引擎把那一行 event 落盘（带 deadline，绝不挂死）
    events_path = engine_root / goal_id / "events.jsonl"
    deadline = time.monotonic() + _READ_TIMEOUT_S
    while True:
        if events_path.exists() and events_path.read_text(encoding="utf-8").strip():
            break
        assert time.monotonic() < deadline, "stub engine did not append its event line in time"
        time.sleep(0.01)

    # reap stub 子进程，确保无残留
    deadline = time.monotonic() + _READ_TIMEOUT_S
    while True:
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            break
        if reaped == pid:
            break
        assert time.monotonic() < deadline, "stub engine child did not exit in time"
        time.sleep(0.01)

    # stub 写下的 event 能被随后的 goal_list / goal_status 读出来
    resp = _call_tool(ctx, 2, "goal_list", {})
    rows = resp["result"]["structuredContent"]
    assert [row["goal_id"] for row in rows] == [goal_id]
    assert rows[0]["pid"] == pid
    assert rows[0]["state"] == "crashed"  # alive_probe=False + 非终态 → 只报告（GO-16）

    resp = _call_tool(ctx, 3, "goal_status", {"goal_id": goal_id})
    view = resp["result"]["structuredContent"]
    assert view["goal_id"] == goal_id
    assert view["state"] == "crashed"
    assert [ev["kind"] for ev in view["tail"]] == ["engine.started"]
    assert view["tail"][0]["payload"]["pid"] == pid
    assert view["tail"][0]["seq"] == 1
