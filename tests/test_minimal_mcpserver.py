"""Tests for fleet_graph.minimal.mcpserver: JSON-RPC stdio transport + engine spawn."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

from fleet_graph.minimal import mcpserver, mcptools
from fleet_graph.minimal.events import EventLog
from fleet_graph.minimal.mcpserver import (
    PROTOCOL_VERSION,
    ServerContext,
    engine_argv,
    handle_request,
    popen_spawner,
    serve,
    tools_list,
)


class StubSpawner:
    """注入式 stub spawner：只记录收到的 argv / log_path，绝不起进程。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], *, log_path: Path) -> int:
        self.calls.append({"argv": list(argv), "log_path": Path(log_path)})
        return 4242


class _AllTrueProbe:
    def is_worktree(self, path: str) -> bool:
        return True

    def branch_exists(self, path: str, branch: str) -> bool:
        return True

    def bash_parses(self, command: str) -> bool:
        return True


def base_enroll() -> dict:
    return {
        "schema": "goal.enroll/2",
        "work_folder": "wf-ab12cd",
        "title": "重做最小系统",
        "goal_text": "把 fleet-graph 重建为最小系统。",
        "source_branch": "release/loopx-minimal",
        "repos": [
            {
                "path": "/data/wt/alpha",
                "remote": "origin",
                "target_branch": "main",
                "acceptance": ["make verify"],
            }
        ],
    }


def make_ctx(tmp_path: Path, stub: StubSpawner) -> ServerContext:
    return ServerContext(
        engine_root=tmp_path,
        spawner=stub,
        git_probe=_AllTrueProbe(),
    )


def rpc(request_id: Any, method: str, params: dict | None = None) -> dict:
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    return request


def call_tool(request_id: Any, name: str, arguments: dict, ctx: ServerContext) -> dict:
    return handle_request(
        rpc(request_id, "tools/call", {"name": name, "arguments": arguments}), ctx
    )


def seed_running(
    engine_root: Path,
    goal_id: str,
    title: str | None = None,
    *,
    engine_pid: int | None = None,
) -> None:
    elog = EventLog(engine_root / goal_id)
    elog.append("goal.enrolled", {"title": title} if title else {})
    elog.append("goal.turn.started", {})
    if engine_pid is not None:
        elog.append("engine.started", {"pid": engine_pid})


# --- initialize -------------------------------------------------------------


def test_initialize_returns_valid_jsonrpc_result(tmp_path: Path) -> None:
    ctx = ServerContext(engine_root=tmp_path)
    resp = handle_request(rpc(1, "initialize", {}), ctx)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "error" not in resp
    result = resp["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert isinstance(result["serverInfo"]["name"], str) and result["serverInfo"]["name"]


# --- tools/list -------------------------------------------------------------


def test_tools_list_names_equal_declaration_table() -> None:
    entries = tools_list()
    # 集合断言，防本模块与 mcptools 声明表漂移。
    assert {entry["name"] for entry in entries} == set(mcptools.TOOL_NAMES)


def test_tools_list_entries_generated_from_specs() -> None:
    by_name = {spec.name: spec for spec in mcptools.TOOLS}
    for entry in tools_list():
        spec = by_name[entry["name"]]
        assert isinstance(entry["description"], str) and entry["description"]
        schema = entry["inputSchema"]
        assert schema["type"] == "object"
        assert set(schema["properties"]) == {name for name, _ in spec.fields}
        assert schema["required"] == list(spec.required)
        for prop in schema["properties"].values():
            assert prop["type"] in {"string", "integer", "object"}


def test_tools_list_over_jsonrpc_matches_table(tmp_path: Path) -> None:
    ctx = ServerContext(engine_root=tmp_path)
    resp = handle_request(rpc(7, "tools/list"), ctx)
    assert "error" not in resp
    assert {tool["name"] for tool in resp["result"]["tools"]} == set(mcptools.TOOL_NAMES)


# --- tools/call：参数非法 → 字段级 error，且不 spawn ------------------------


def test_tools_call_wrong_type_field_level_error_no_spawn(tmp_path: Path) -> None:
    stub = StubSpawner()
    ctx = make_ctx(tmp_path, stub)
    resp = call_tool(1, "goal_message", {"goal_id": "g-000001", "text": 123}, ctx)
    error = resp["error"]
    assert error["code"] == mcpserver.INVALID_PARAMS
    assert "text" in error["message"] and "string" in error["message"]
    assert error["data"]["fieldErrors"]
    assert stub.calls == []
    assert not (tmp_path / "g-000001" / "control.jsonl").exists()


def test_tools_call_missing_required_field_level_error(tmp_path: Path) -> None:
    stub = StubSpawner()
    ctx = make_ctx(tmp_path, stub)
    resp = call_tool(2, "goal_status", {}, ctx)
    assert resp["error"]["code"] == mcpserver.INVALID_PARAMS
    assert "goal_id" in resp["error"]["message"]
    assert stub.calls == []


def test_tools_call_unknown_tool_is_field_level_error(tmp_path: Path) -> None:
    stub = StubSpawner()
    ctx = make_ctx(tmp_path, stub)
    resp = call_tool(3, "nope", {}, ctx)
    assert resp["error"]["code"] == mcpserver.INVALID_PARAMS
    assert "unknown tool" in resp["error"]["message"]
    assert stub.calls == []


def test_tools_call_invalid_enroll_payload_no_spawn(tmp_path: Path) -> None:
    stub = StubSpawner()
    ctx = make_ctx(tmp_path, stub)
    resp = call_tool(4, "goal_enroll", {"enroll": {"title": "缺字段"}}, ctx)
    assert resp["error"]["code"] == mcpserver.INVALID_PARAMS
    assert "goal.enroll/2" in resp["error"]["message"]  # enroll 校验的字段级文案透传
    assert stub.calls == []


def test_goal_stop_kill_surfaces_handler_error_no_spawn(tmp_path: Path) -> None:
    stub = StubSpawner()
    ctx = make_ctx(tmp_path, stub)
    resp = call_tool(5, "goal_stop", {"goal_id": "g-000001", "mode": "kill"}, ctx)
    assert resp["error"]["code"] == mcpserver.INVALID_PARAMS
    assert "kill" in resp["error"]["message"]
    assert stub.calls == []
    assert not (tmp_path / "g-000001" / "control.jsonl").exists()


# --- spawn：argv 逐字契约 ----------------------------------------------------


def test_goal_enroll_spawns_engine_with_verbatim_argv(tmp_path: Path) -> None:
    stub = StubSpawner()
    ctx = make_ctx(tmp_path, stub)
    resp = call_tool(6, "goal_enroll", {"enroll": base_enroll(), "goal_id": "g-7f3a2c"}, ctx)
    assert "error" not in resp, resp
    assert len(stub.calls) == 1
    argv = stub.calls[0]["argv"]
    assert argv == [
        sys.executable,
        "-m",
        "fleet_graph.minimal.engine",
        "--goal-id",
        "g-7f3a2c",
        "--engine-root",
        str(tmp_path),
    ]
    assert argv == engine_argv("g-7f3a2c", tmp_path)
    assert stub.calls[0]["log_path"] == tmp_path / "g-7f3a2c" / "engine.log"
    result = resp["result"]["structuredContent"]
    assert result["spawned"] is True
    assert result["goal_id"] == "g-7f3a2c"
    assert result["action"] == "enroll"
    assert result["pid"] == 4242
    block = resp["result"]["content"][0]
    assert block["type"] == "text"
    assert json.loads(block["text"])["goal_id"] == "g-7f3a2c"


def test_goal_resume_spawns_engine_with_verbatim_argv(tmp_path: Path) -> None:
    stub = StubSpawner()
    ctx = make_ctx(tmp_path, stub)
    seed_running(tmp_path, "g-000001")
    resp = call_tool(8, "goal_resume", {"goal_id": "g-000001"}, ctx)
    assert "error" not in resp, resp
    assert len(stub.calls) == 1
    assert stub.calls[0]["argv"] == [
        sys.executable,
        "-m",
        "fleet_graph.minimal.engine",
        "--goal-id",
        "g-000001",
        "--engine-root",
        str(tmp_path),
    ]
    assert resp["result"]["structuredContent"]["action"] == "resume"


# --- 读类工具与直接调 mcptools 一致 ------------------------------------------


def test_read_tools_match_direct_mcptools_calls(tmp_path: Path) -> None:
    stub = StubSpawner()
    ctx = make_ctx(tmp_path, stub)
    # os.getpid() 保证默认存活探测确定性地判活（就是我们自己）。
    seed_running(tmp_path, "g-000001", title="甲", engine_pid=os.getpid())
    seed_running(tmp_path, "g-000002", title="乙")

    resp = call_tool(10, "goal_list", {}, ctx)
    assert resp["result"]["structuredContent"] == mcptools.goal_list(tmp_path)
    assert [row["goal_id"] for row in resp["result"]["structuredContent"]] == [
        "g-000001",
        "g-000002",
    ]

    resp = call_tool(11, "goal_status", {"goal_id": "g-000001", "tail": 2}, ctx)
    assert resp["result"]["structuredContent"] == mcptools.goal_status(tmp_path, "g-000001", tail=2)

    resp = call_tool(12, "goal_events", {"goal_id": "g-000001", "since_seq": 1}, ctx)
    assert resp["result"]["structuredContent"] == mcptools.goal_events(
        tmp_path, "g-000001", since_seq=1
    )

    resp = call_tool(13, "goal_observations", {"goal_id": "g-000001"}, ctx)
    assert resp["result"]["structuredContent"] == mcptools.goal_observations(tmp_path, "g-000001")

    assert stub.calls == []  # 读工具绝不 spawn


def test_goal_message_via_tools_call_appends_one_control_line(tmp_path: Path) -> None:
    stub = StubSpawner()
    ctx = make_ctx(tmp_path, stub)
    resp = call_tool(20, "goal_message", {"goal_id": "g-000001", "text": "继续推进"}, ctx)
    assert "error" not in resp, resp
    assert resp["result"]["structuredContent"]["op"] == "message"
    lines = (tmp_path / "g-000001" / "control.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "继续推进"
    assert stub.calls == []


# --- GO-16：崩溃只标出来，绝不自动 resume ------------------------------------


def test_crashed_goal_marked_but_never_auto_resumed(tmp_path: Path) -> None:
    stub = StubSpawner()
    ctx = ServerContext(
        engine_root=tmp_path,
        spawner=stub,
        git_probe=_AllTrueProbe(),
        alive_probe=lambda pid: False,
    )
    seed_running(tmp_path, "g-000001", engine_pid=4242)
    resp = call_tool(30, "goal_list", {}, ctx)
    rows = resp["result"]["structuredContent"]
    assert rows[0]["state"] == "crashed"
    assert rows[0]["pid"] == 4242
    assert stub.calls == []  # 服务器自己绝不 spawn，只有显式 goal_resume 才会
    assert not (tmp_path / "g-000001" / "control.jsonl").exists()


# --- stdio 循环 --------------------------------------------------------------


def test_stdio_loop_multiline_output_all_valid_json(tmp_path: Path) -> None:
    stub = StubSpawner()
    ctx = make_ctx(tmp_path, stub)
    seed_running(tmp_path, "g-000001", engine_pid=os.getpid())
    lines = [
        json.dumps(rpc(1, "initialize", {})),
        "{this is not json",
        "",
        json.dumps(rpc(2, "tools/list")),
        json.dumps(rpc(3, "tools/call", {"name": "goal_list", "arguments": {}})),
        json.dumps(rpc(4, "no/such/method")),
    ]
    out = io.StringIO()
    serve(lines, out, ctx)  # 非法 JSON 行之后循环必须继续
    output_lines = out.getvalue().splitlines()
    assert len(output_lines) == 5  # 空行跳过，其余一条输入一条输出
    parsed = [json.loads(line) for line in output_lines]  # 逐行都是合法 JSON
    assert "result" in parsed[0]
    assert parsed[1]["id"] is None
    assert parsed[1]["error"]["code"] == mcpserver.PARSE_ERROR
    assert {tool["name"] for tool in parsed[2]["result"]["tools"]} == set(mcptools.TOOL_NAMES)
    assert [row["goal_id"] for row in parsed[3]["result"]["structuredContent"]] == ["g-000001"]
    assert parsed[4]["error"]["code"] == mcpserver.METHOD_NOT_FOUND


# --- 信封与默认 spawner ------------------------------------------------------


def test_request_envelope_errors(tmp_path: Path) -> None:
    ctx = ServerContext(engine_root=tmp_path)
    assert (
        handle_request({"id": 1, "method": "initialize"}, ctx)["error"]["code"]
        == mcpserver.INVALID_REQUEST
    )
    assert (
        handle_request({"jsonrpc": "1.0", "id": 2, "method": "initialize"}, ctx)["error"]["code"]
        == mcpserver.INVALID_REQUEST
    )
    assert handle_request({"jsonrpc": "2.0", "id": 3}, ctx)["error"]["code"] == (
        mcpserver.INVALID_REQUEST
    )
    assert handle_request(rpc(4, "bogus"), ctx)["error"]["code"] == mcpserver.METHOD_NOT_FOUND
    # 合法 JSON 但不是对象 → invalid request，循环不崩。
    resp = mcpserver.handle_line("[1,2,3]", ctx)
    assert resp["error"]["code"] == mcpserver.INVALID_REQUEST


def test_popen_spawner_detaches_session_and_redirects_logs(tmp_path: Path) -> None:
    log_path = tmp_path / "g-000001" / "engine.log"
    argv = [sys.executable, "-c", "import os, time; print(os.getpid(), flush=True); time.sleep(2)"]
    pid = popen_spawner(argv, log_path=log_path)
    try:
        # start_new_session=True：引擎进程组脱离 MCP（本测试进程）。
        assert os.getsid(pid) != os.getsid(os.getpid())
    finally:
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
    assert log_path.exists()
    assert str(pid) in log_path.read_text(encoding="utf-8")
