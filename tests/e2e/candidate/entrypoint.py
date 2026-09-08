"""运行真实 Fleet CLI，并为容器网络提供透明 HTTP 流式 relay。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path

from configure import configure
from fastmcp import Client

STATE = Path("/state")
REQUIRED_TOOLS = {"goal_enroll", "goal_status", "goal_events", "goal_session", "goal_artifact"}


def inject_secret(name: str, default_path: str):
    path = Path(os.environ.get(name + "_FILE", default_path))
    value = path.read_text().strip()
    if not value:
        raise ValueError(f"测试 secret 文件为空：{name}")
    os.environ[name] = value


async def health():
    async with Client("http://127.0.0.1:15612/mcp", timeout=4) as client:
        names = {tool.name for tool in await client.list_tools()}
    missing = sorted(REQUIRED_TOOLS - names)
    return {
        "ready": not missing,
        "upstream": "fleet_cli",
        "tools": sorted(names),
        "missing_tools": missing,
        "model_call_verified": False,
    }


async def json_response(writer, status, value):
    body = json.dumps(value, ensure_ascii=False).encode()
    reason = "OK" if status == 200 else "Service Unavailable"
    writer.write(
        (
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode()
        + body
    )
    await writer.drain()


async def relay(reader, writer):
    upstream = None
    try:
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        first = header.split(b"\r\n", 1)[0].split()
        path = first[1].split(b"?", 1)[0]
        if path == b"/health":
            try:
                result = await asyncio.wait_for(health(), 5)
                await json_response(writer, 200 if result["ready"] else 503, result)
            except Exception as error:
                await json_response(writer, 503, {"ready": False, "error": str(error)})
            return
        if path == b"/candidate-manifest":
            await json_response(
                writer, 200, json.loads((STATE / "candidate-manifest.json").read_text())
            )
            return
        upstream_reader, upstream = await asyncio.open_connection("127.0.0.1", 15612)
        # Host 与实际 loopback endpoint 一致；其他 MCP session / SSE / chunked 字节保持原样。
        lines = [
            b"Host: 127.0.0.1:15612" if line.lower().startswith(b"host:") else line
            for line in header.split(b"\r\n")
        ]
        upstream.write(b"\r\n".join(lines))
        await upstream.drain()

        async def pipe(source, destination):
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()

        tasks = [
            asyncio.create_task(pipe(reader, upstream)),
            asyncio.create_task(pipe(upstream_reader, writer)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (
        TimeoutError,
        OSError,
        ValueError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        pass
    finally:
        for stream in (upstream, writer):
            if stream:
                stream.close()
                with contextlib.suppress(OSError):
                    await stream.wait_closed()


async def bootstrap_runtime_bus():
    """执行冻结 runtime 的真实协议注册；失败不能启动 Fleet。"""
    log_path = STATE / "logs/runtime-bus-bootstrap.log"
    with log_path.open("ab", buffering=0) as log:
        process = await asyncio.create_subprocess_exec(
            "bun", "/opt/e2e/bootstrap_runtime_bus.ts", stdout=log, stderr=log
        )
        error = None
        try:
            exit_code = await asyncio.wait_for(process.wait(), timeout=60)
        except TimeoutError:
            process.kill()
            exit_code = await process.wait()
            error = "独立 runtime bus 初始化超时"
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
    result = {
        "exit_code": exit_code,
        "log": str(log_path),
        "report": str(STATE / "runtime-bus-bootstrap.json"),
        "error": error,
    }
    manifest_path = STATE / "candidate-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["agent_bus"]["bootstrap"] = result
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    if exit_code or error:
        print(f"独立 runtime bus 初始化失败，Fleet 未启动；证据：{log_path}", file=sys.stderr)
        raise SystemExit(exit_code if exit_code > 0 else 1)


async def main():
    os.umask(0o077)
    inject_secret("NEW_API_GATEWAY_TOKEN_OPENAI", "/run/secrets/gateway_token")
    inject_secret("GH_TOKEN", "/run/secrets/gh_token")
    STATE.mkdir(parents=True, exist_ok=True)
    git_config = STATE / "gitconfig"
    git_config.write_text(
        "[user]\n\tname = Fleet E2E\n\temail = fleet-e2e@example.invalid\n"
        '[credential "https://github.com"]\n\thelper =\n\thelper = !gh auth git-credential\n'
    )
    os.environ["GIT_CONFIG_GLOBAL"] = str(git_config)
    config = configure()
    (STATE / "logs").mkdir(exist_ok=True)
    await bootstrap_runtime_bus()
    with (STATE / "logs/fleet.log").open("ab", buffering=0) as log:
        fleet = await asyncio.create_subprocess_exec(
            "fleet-graph",
            "serve",
            "--config",
            str(config),
            "--root",
            str(STATE / "fleet"),
            "--host",
            "127.0.0.1",
            "--port",
            "15612",
            stdout=log,
            stderr=log,
        )
        server = await asyncio.start_server(relay, "0.0.0.0", 15611)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stopped.set)
        wait_process = asyncio.create_task(fleet.wait())
        wait_stop = asyncio.create_task(stopped.wait())
        try:
            async with server:
                await asyncio.wait([wait_process, wait_stop], return_when=asyncio.FIRST_COMPLETED)
        finally:
            wait_stop.cancel()
            if fleet.returncode is None:
                fleet.terminate()
                try:
                    await asyncio.wait_for(fleet.wait(), 10)
                except TimeoutError:
                    fleet.kill()
                    await fleet.wait()
        if wait_process.done() and not stopped.is_set():
            raise SystemExit(fleet.returncode or 1)


if __name__ == "__main__":
    asyncio.run(main())
