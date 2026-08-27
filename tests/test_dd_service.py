"""Contract coverage for the graph-backed dev-dispatch MCP surface."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fleet_graph.dd.service import DEFAULT_PORT, build_mcp_server, port_is_available


class FakeGraphApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def get(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append(("GET", path, params))
        return {"path": path}

    def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        self.calls.append(("POST", path, body))
        return {"path": path}


def test_selected_port_is_free_and_not_a_legacy_port() -> None:
    assert DEFAULT_PORT == 5610
    assert port_is_available(port=DEFAULT_PORT)


def test_all_thirteen_tools_are_reachable() -> None:
    server = build_mcp_server(FakeGraphApi())
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "deployment_create",
        "deployment_status",
        "development_control",
        "development_create",
        "development_list",
        "development_get",
        "development_events",
        "development_evidence",
        "development_gate",
        "development_start",
        "development_steer",
        "development_reconfigure",
        "development_relock",
    }


@contextmanager
def running_server(server: object) -> Iterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    app = server.http_app(path="/mcp", transport="streamable-http")  # type: ignore[attr-defined]
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(100):
            try:
                httpx.get(url, timeout=0.1)
            except httpx.HTTPError:
                time.sleep(0.01)
            else:
                break
        else:
            pytest.fail("MCP endpoint did not become reachable")
        yield url
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=5)


def test_every_tool_forwards_requests_over_the_running_mcp_endpoint() -> None:
    graph = FakeGraphApi()
    server = build_mcp_server(graph)
    calls = [
        ("deployment_create", {"request": {"operation": "deploy"}}),
        ("deployment_status", {"operation_id": "operation-1"}),
        ("development_list", {"state": "running", "repo": "repo", "limit": 4, "cursor": "c1"}),
        ("development_get", {"development_id": "dev-1"}),
        ("development_events", {"development_id": "dev-1", "after": "e1", "limit": 3}),
        ("development_evidence", {"development_id": "dev-1"}),
        (
            "development_create",
            {
                "name": "name",
                "goal": "goal",
                "idempotency_key": "create-key",
                "reason": "reason",
                "initial_handoff": {"kind": "handoff"},
            },
        ),
        (
            "development_start",
            {"development_id": "dev-1", "idempotency_key": "start-key", "expected_revision": 7},
        ),
        (
            "development_steer",
            {
                "development_id": "dev-1",
                "instruction": "continue",
                "idempotency_key": "steer-key",
                "expected_revision": 8,
            },
        ),
        (
            "development_reconfigure",
            {"development_id": "dev-1", "idempotency_key": "config-key", "expected_revision": 9},
        ),
        (
            "development_gate",
            {
                "development_id": "dev-1",
                "gate_id": "gate-1",
                "decision": "approve",
                "idempotency_key": "gate-key",
                "expected_revision": 10,
                "operator_identity": "operator",
            },
        ),
        (
            "development_control",
            {
                "development_id": "dev-1",
                "action": "pause",
                "idempotency_key": "control-key",
                "expected_revision": 11,
            },
        ),
        (
            "development_relock",
            {
                "development_id": "dev-1",
                "plugin_commit": "abc123",
                "idempotency_key": "relock-key",
                "expected_revision": 12,
            },
        ),
    ]

    async def exercise_endpoint(url: str) -> None:
        async with Client(url) as client:
            for name, arguments in calls:
                await client.call_tool(name, arguments)
            for name, _ in calls:
                if name != "development_list":
                    with pytest.raises(ToolError):
                        await client.call_tool(name, {})

    with running_server(server) as url:
        asyncio.run(exercise_endpoint(url))

    assert graph.calls == [
        ("POST", "/v1/deployments", {"request": {"operation": "deploy"}}),
        ("GET", "/v1/deployments/operation-1", None),
        (
            "GET",
            "/v1/developments",
            {"state": "running", "repo": "repo", "limit": 4, "cursor": "c1"},
        ),
        ("GET", "/v1/developments/dev-1", None),
        ("GET", "/v1/developments/dev-1/events", {"after": "e1", "limit": 3}),
        ("GET", "/v1/developments/dev-1/evidence", None),
        (
            "POST",
            "/v1/developments",
            {
                "name": "name",
                "goal": "goal",
                "idempotency_key": "create-key",
                "reason": "reason",
                "initial_handoff": {"kind": "handoff"},
                "phase": "development",
                "profile": "default",
                "policy": "isolated-release-auto",
                "auto_start": False,
            },
        ),
        (
            "POST",
            "/v1/developments/dev-1/commands/start",
            {"idempotency_key": "start-key", "expected_revision": 7, "reason": ""},
        ),
        (
            "POST",
            "/v1/developments/dev-1/commands/steer",
            {
                "idempotency_key": "steer-key",
                "expected_revision": 8,
                "reason": "",
                "instruction": "continue",
                "urgency": "next_safe_boundary",
            },
        ),
        (
            "POST",
            "/v1/developments/dev-1/commands/reconfigure",
            {"idempotency_key": "config-key", "expected_revision": 9, "reason": ""},
        ),
        (
            "POST",
            "/v1/developments/dev-1/commands/gate",
            {
                "idempotency_key": "gate-key",
                "expected_revision": 10,
                "reason": "",
                "gate_id": "gate-1",
                "decision": "approve",
                "operator_identity": "operator",
            },
        ),
        (
            "POST",
            "/v1/developments/dev-1/commands/control",
            {
                "idempotency_key": "control-key",
                "expected_revision": 11,
                "reason": "",
                "action": "pause",
            },
        ),
        (
            "POST",
            "/v1/developments/dev-1/commands/relock",
            {
                "idempotency_key": "relock-key",
                "expected_revision": 12,
                "reason": "",
                "plugin_commit": "abc123",
            },
        ),
    ]
