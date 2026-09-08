"""minimal 常驻控制面：mcptools 九工具的 stdio 传输层 + 引擎进程 spawn 执行。

GO-7.2 / GO-10 / GO-11：MCP 是这套系统唯一常驻的服务。``mcptools`` 已把
protocol §10/§12 的九个工具落成声明表 + 读写 handler，但显式不绑传输层；
本模块就是那层传输：JSON-RPC 2.0 over stdio，逐行读 JSON、逐行写 JSON，
只实现 ``initialize`` / ``tools/list`` / ``tools/call`` 三个方法，不引第三方
MCP 框架（仓依赖里没有独立的 ``mcp`` 包，spec 允许手写循环）。

核心是纯函数 :func:`handle_request`（dict 进、dict 出，不碰任何流），
:func:`serve` 只是它的 stdio 薄壳，测试全打在纯函数上。``tools/list`` 直接由
``mcptools.TOOLS`` 声明表生成（名字 / 描述 / inputSchema），本模块不另抄
一份工具表；``tools/call`` 参数先过 ``mcptools.validate_tool_call``，字段级
错误按 JSON-RPC error（-32602，message / data 都带）返回。

本模块是 minimal 包里唯一出现 ``subprocess`` 的地方：mcptools 的写 handler
只产 :class:`~fleet_graph.minimal.mcptools.SpawnPlan`（只描述 spawn），由
这里的可注入 spawner seam（默认 :func:`popen_spawner`）真正 ``Popen`` 引擎
进程，argv 逐字为 ``[sys.executable, "-m", "fleet_graph.minimal.engine",
"--goal-id", <goal_id>, "--engine-root", <engine_root>]``（flag 契约由并行
的 engine.py DD 定义），``start_new_session=True`` 脱离 MCP 的进程组，
stdout/stderr 重定向到 ``<goal_run_root>/engine.log``。GO-16：崩溃的 goal
只在 ``goal_list`` 里被标出来，服务器绝不自动重起；spawn 只发生在显式
``goal_enroll`` / ``goal_resume`` 调用里。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from fleet_graph.minimal import mcptools, runroot
from fleet_graph.minimal.enroll import GitProbe
from fleet_graph.minimal.mcptools import SpawnPlan, ToolSpec

JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO: dict[str, Any] = {"name": "fleet-graph-minimal-mcp", "version": "1.0.0"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_ENGINE_MODULE = "fleet_graph.minimal.engine"

# 声明表字段类型 → JSON Schema 类型（ToolSpec.fields 的第二元）。
_JSON_SCHEMA_TYPES: dict[str, str] = {"str": "string", "int": "integer", "dict": "object"}

# 描述由 kind 机械生成（protocol §10 的读写铁律），不逐工具手抄。
_KIND_SUMMARY: dict[str, str] = {
    "read": "read-only; touches only that goal's event log "
    "(goal_observations reads observations.jsonl)",
    "write": "write; appends exactly one line to that goal's control.jsonl, "
    "or spawns the engine process",
}


class Spawner(Protocol):
    """spawn seam：默认实现 :func:`popen_spawner`，测试注入 stub、绝不起进程。"""

    def __call__(self, argv: list[str], *, log_path: Path) -> int: ...


def popen_spawner(argv: list[str], *, log_path: Path) -> int:
    """默认 spawner：``subprocess.Popen`` 起 ``argv``，``start_new_session=True``
    脱离 MCP 的进程组，stdout/stderr 都重定向到 ``log_path``；返回新进程 pid。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    return process.pid


@dataclass(frozen=True)
class ServerContext:
    """:func:`handle_request` 的全部环境：引擎根 + 注入 seam（spawner、git
    probe、进程存活探测）。纯数据 + 回调，不持任何流。"""

    engine_root: str | Path = runroot.DEFAULT_ENGINE_ROOT
    spawner: Spawner = popen_spawner
    git_probe: GitProbe | None = None
    alive_probe: Callable[[int | None], bool] | None = None


def engine_argv(goal_id: str, engine_root: str | Path) -> list[str]:
    """引擎进程 argv，逐字契约（flag 由并行的 engine.py DD 定义，即使那张
    DD 还没合也照这串发）：``[sys.executable, "-m", "fleet_graph.minimal.engine",
    "--goal-id", <goal_id>, "--engine-root", <engine_root>]``。"""
    return [
        sys.executable,
        "-m",
        _ENGINE_MODULE,
        "--goal-id",
        goal_id,
        "--engine-root",
        str(engine_root),
    ]


# --- tools/list：由 mcptools 声明表生成，不重抄工具表 -----------------------


def _tool_description(spec: ToolSpec) -> str:
    params = ", ".join(f"{name}: {_JSON_SCHEMA_TYPES[ftype]}" for name, ftype in spec.fields)
    summary = _KIND_SUMMARY.get(spec.kind, spec.kind)
    return f"{spec.name}({params}) [{spec.kind}] — {summary}"


def _input_schema(spec: ToolSpec) -> dict[str, Any]:
    # 不带 additionalProperties:false：协议 §0.6 允许未列出的键（如给
    # goal_enroll 附带 goal_id），校验由 validate_tool_call 做形状门槛。
    return {
        "type": "object",
        "properties": {name: {"type": _JSON_SCHEMA_TYPES[ftype]} for name, ftype in spec.fields},
        "required": list(spec.required),
    }


def tools_list() -> list[dict[str, Any]]:
    """``tools/list`` 的 tools 数组：一条对应 ``mcptools.TOOLS`` 里的一条。"""
    return [
        {
            "name": spec.name,
            "description": _tool_description(spec),
            "inputSchema": _input_schema(spec),
        }
        for spec in mcptools.TOOLS
    ]


# --- tools/call：分派到 mcptools 同名 handler --------------------------------


def _call_goal_enroll(args: dict[str, Any], ctx: ServerContext) -> Any:
    return mcptools.goal_enroll(
        args["enroll"],
        goal_id=args.get("goal_id"),
        engine_root=ctx.engine_root,
        git_probe=ctx.git_probe,
    )


def _call_goal_list(args: dict[str, Any], ctx: ServerContext) -> Any:
    return mcptools.goal_list(ctx.engine_root, alive_probe=ctx.alive_probe)


def _call_goal_status(args: dict[str, Any], ctx: ServerContext) -> Any:
    return mcptools.goal_status(
        ctx.engine_root,
        args["goal_id"],
        tail=args.get("tail", 20),
        alive_probe=ctx.alive_probe,
    )


def _call_goal_events(args: dict[str, Any], ctx: ServerContext) -> Any:
    return mcptools.goal_events(
        ctx.engine_root,
        args["goal_id"],
        since_seq=args.get("since_seq", 0),
    )


def _call_goal_observations(args: dict[str, Any], ctx: ServerContext) -> Any:
    return mcptools.goal_observations(
        ctx.engine_root,
        args["goal_id"],
        since_ts=args.get("since_ts"),
        severity=args.get("severity"),
    )


def _call_goal_message(args: dict[str, Any], ctx: ServerContext) -> Any:
    return mcptools.goal_message(ctx.engine_root, args["goal_id"], args["text"])


def _call_goal_steer(args: dict[str, Any], ctx: ServerContext) -> Any:
    return mcptools.goal_steer(ctx.engine_root, args["goal_id"], args["patch"])


def _call_goal_stop(args: dict[str, Any], ctx: ServerContext) -> Any:
    return mcptools.goal_stop(ctx.engine_root, args["goal_id"], args["mode"])


def _call_goal_resume(args: dict[str, Any], ctx: ServerContext) -> Any:
    return mcptools.goal_resume(ctx.engine_root, args["goal_id"])


_DISPATCH: dict[str, Callable[[dict[str, Any], ServerContext], Any]] = {
    "goal_enroll": _call_goal_enroll,
    "goal_list": _call_goal_list,
    "goal_status": _call_goal_status,
    "goal_events": _call_goal_events,
    "goal_message": _call_goal_message,
    "goal_steer": _call_goal_steer,
    "goal_stop": _call_goal_stop,
    "goal_resume": _call_goal_resume,
    "goal_observations": _call_goal_observations,
}


# --- JSON-RPC 信封 -----------------------------------------------------------


def _ok(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, *, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def _tool_result(value: Any) -> dict[str, Any]:
    """MCP tools/call 的成功结果信封：一个 text block + 同值的 structuredContent。"""
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "structuredContent": value,
        "isError": False,
    }


def _execute_spawn(plan: SpawnPlan, ctx: ServerContext) -> dict[str, Any]:
    """把 SpawnPlan 变成真正的 spawn——这里、也只有这里调 spawner。"""
    argv = engine_argv(plan.goal_id, ctx.engine_root)
    log_path = Path(plan.run_root) / "engine.log"
    pid = ctx.spawner(argv, log_path=log_path)
    return {
        "spawned": True,
        "goal_id": plan.goal_id,
        "action": plan.action,
        "pid": pid,
        "argv": argv,
        "engine_log": str(log_path),
    }


def _tools_call(params: Any, request_id: Any, ctx: ServerContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        return _error(request_id, INVALID_PARAMS, "tools/call params must be a JSON object")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _error(
            request_id, INVALID_PARAMS, "tools/call params must carry a non-empty string 'name'"
        )
    arguments = params.get("arguments", {})
    errors = mcptools.validate_tool_call(name, arguments)
    if errors:
        # 字段级信息同时进 message 与 data.fieldErrors。
        return _error(request_id, INVALID_PARAMS, "; ".join(errors), data={"fieldErrors": errors})
    try:
        value = _DISPATCH[name](arguments, ctx)
        if isinstance(value, SpawnPlan):
            value = _execute_spawn(value, ctx)
        result = _tool_result(value)
    except ValueError as exc:
        # handler 的值级校验（enroll 校验、steer 禁改字段、stop kill、goal_id
        # 形状）都抛 ValueError，字段级文案原样进 error。
        return _error(request_id, INVALID_PARAMS, str(exc))
    except Exception as exc:
        # 常驻服务：单条调用炸了只能变成一条 error 响应，循环不崩。
        return _error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
    return _ok(request_id, result)


def handle_request(req: dict[str, Any], ctx: ServerContext) -> dict[str, Any]:
    """「一条请求 → 一条响应」的纯函数核心（不碰任何流）。

    信封不合 → -32600；方法未知 → -32601；``tools/call`` 参数不过
    ``mcptools.validate_tool_call`` 或 handler 值级校验 → -32602（带字段级
    信息）；其余异常 → -32603。合法的 ``goal_enroll`` / ``goal_resume`` 在
    这里真正 spawn 引擎进程（经 ctx.spawner）。
    """
    if not isinstance(req, dict):
        return _error(
            None, INVALID_REQUEST, f"request must be a JSON object, got {type(req).__name__}"
        )
    request_id = req.get("id")
    if req.get("jsonrpc") != JSONRPC_VERSION:
        return _error(request_id, INVALID_REQUEST, f"jsonrpc must be exactly {JSONRPC_VERSION!r}")
    method = req.get("method")
    if not isinstance(method, str) or not method:
        return _error(request_id, INVALID_REQUEST, "method must be a non-empty string")
    if method == "initialize":
        return _ok(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": dict(SERVER_INFO),
            },
        )
    if method == "tools/list":
        return _ok(request_id, {"tools": tools_list()})
    if method == "tools/call":
        return _tools_call(req.get("params"), request_id, ctx)
    return _error(request_id, METHOD_NOT_FOUND, f"unknown method {method!r}")


def handle_line(line: str, ctx: ServerContext) -> dict[str, Any]:
    """一行输入 → 一条响应：JSON 解析失败 → parse error（-32700，id 为 null）。"""
    try:
        req = json.loads(line)
    except json.JSONDecodeError as exc:
        return _error(None, PARSE_ERROR, f"parse error: {exc}")
    return handle_request(req, ctx)


def serve(stdin: Iterable[str], stdout: TextIO, ctx: ServerContext) -> None:
    """stdio 薄壳：逐行读 JSON、逐行写 JSON；空行跳过，任何一条请求的失败
    （含 parse error）都只变成一条错误响应，循环继续。"""
    for line in stdin:
        stripped = line.strip()
        if not stripped:
            continue
        stdout.write(json.dumps(handle_line(stripped, ctx), ensure_ascii=False) + "\n")
        stdout.flush()


def main() -> None:
    """``python -m fleet_graph.minimal.mcpserver`` 的入口。"""
    engine_root = os.environ.get("FLEET_ENGINE_ROOT", runroot.DEFAULT_ENGINE_ROOT)
    serve(sys.stdin, sys.stdout, ServerContext(engine_root=engine_root))


__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSONRPC_VERSION",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "SERVER_INFO",
    "ServerContext",
    "Spawner",
    "engine_argv",
    "handle_line",
    "handle_request",
    "main",
    "popen_spawner",
    "serve",
    "tools_list",
]


if __name__ == "__main__":
    main()
