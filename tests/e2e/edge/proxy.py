"""测试专用出网边界：固定网关 TCP relay 与 GitHub CONNECT allowlist。"""

import asyncio
import os
import signal

ALLOWED = frozenset(
    {
        "github.com",
        "api.github.com",
        "uploads.github.com",
        "raw.githubusercontent.com",
        "codeload.github.com",
    }
)


async def pipe(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()


async def connection(reader, writer):
    upstream = None
    try:
        mode = os.environ["EDGE_MODE"]
        if mode == "proxy":
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
            method, authority, _ = header.split(b"\r\n", 1)[0].decode("ascii").split()
            host, port = authority.rsplit(":", 1)
            if method != "CONNECT" or host.lower() not in ALLOWED or port != "443":
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            remote_reader, upstream = await asyncio.open_connection(host, 443)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            print(f"允许 GitHub CONNECT: {host}", flush=True)
        else:
            remote_reader, upstream = await asyncio.open_connection(
                os.environ["UPSTREAM_HOST"], int(os.environ["UPSTREAM_PORT"])
            )
        tasks = [
            asyncio.create_task(pipe(reader, upstream)),
            asyncio.create_task(pipe(remote_reader, writer)),
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
        UnicodeError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        # 不打印请求头，避免 Authorization 泄漏。
        pass
    finally:
        if upstream:
            upstream.close()
        writer.close()


async def main():
    server = await asyncio.start_server(
        connection, os.environ.get("BIND_HOST", "0.0.0.0"), int(os.environ["BIND_PORT"])
    )
    stopped = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, stopped.set)
    async with server:
        await stopped.wait()


if __name__ == "__main__":
    asyncio.run(main())
