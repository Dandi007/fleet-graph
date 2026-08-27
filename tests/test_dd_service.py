"""Contract coverage for the graph-backed dev-dispatch MCP surface.

Tool-surface shape follows the wf-a08949 2026-08-27 use-case-family ruling:
the full 13-name legacy surface stays reachable, only the consumed family
(list/get/events/evidence/create/start/gate) forwards, and every legacy-only
name refuses with an explicit NOT_SUPPORTED structure and zero graph calls.
"""

from __future__ import annotations

import asyncio
import json
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

from fleet_graph.dd.service import (
    DEFAULT_PORT,
    NOT_SUPPORTED_TOOLS,
    SUPPORTED_TOOLS,
    build_mcp_server,
    port_is_available,
)

ALL_TOOLS = {
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

# Arguments that satisfy each legacy-only tool's schema, so the refusal we
# observe is the NOT_SUPPORTED structure and not an argument-validation error.
NOT_SUPPORTED_CALLS: dict[str, dict[str, object]] = {
    "deployment_create": {"request": {"operation": "deploy"}},
    "deployment_status": {"operation_id": "operation-1"},
    "development_steer": {
        "development_id": "dev-1",
        "instruction": "continue",
        "idempotency_key": "steer-key",
        "expected_revision": 8,
    },
    "development_reconfigure": {
        "development_id": "dev-1",
        "idempotency_key": "config-key",
        "expected_revision": 9,
    },
    "development_control": {
        "development_id": "dev-1",
        "action": "pause",
        "idempotency_key": "control-key",
        "expected_revision": 11,
    },
    "development_relock": {
        "development_id": "dev-1",
        "plugin_commit": "abc123",
        "idempotency_key": "relock-key",
        "expected_revision": 12,
    },
}


@pytest.fixture(autouse=True)
def _loopback_needs_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host proxy env must not leak into loopback MCP clients.

    httpx builds a transport for every env-declared proxy eagerly, so an
    `all_proxy=socks5://...` on the host machine makes every client raise
    ImportError (socksio) before NO_PROXY is even consulted."""
    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)


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
    assert {tool.name for tool in tools} == ALL_TOOLS


def test_the_surface_split_is_exactly_the_ruling() -> None:
    """Supported + refused partitions the 13 names, with no overlap."""
    assert SUPPORTED_TOOLS | set(NOT_SUPPORTED_TOOLS) == ALL_TOOLS
    assert not SUPPORTED_TOOLS & set(NOT_SUPPORTED_TOOLS)
    assert {
        "development_list",
        "development_get",
        "development_events",
        "development_evidence",
        "development_create",
        "development_start",
        "development_gate",
    } == SUPPORTED_TOOLS


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


def test_every_supported_tool_forwards_requests_over_the_running_mcp_endpoint() -> None:
    graph = FakeGraphApi()
    server = build_mcp_server(graph)
    calls = [
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
    ]
    assert {name for name, _ in calls} == set(SUPPORTED_TOOLS)

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
    ]


@pytest.mark.parametrize("tool", sorted(NOT_SUPPORTED_TOOLS))
def test_every_legacy_only_tool_refuses_with_the_explicit_structure(tool: str) -> None:
    """The refusal carries a machine-readable payload and touches no graph API."""
    graph = FakeGraphApi()
    server = build_mcp_server(graph)

    async def call(url: str) -> str:
        async with Client(url) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.call_tool(tool, NOT_SUPPORTED_CALLS[tool])
            return str(excinfo.value)

    with running_server(server) as url:
        message = asyncio.run(call(url))

    payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
    assert payload["code"] == "NOT_SUPPORTED"
    assert payload["tool"] == tool
    assert payload["reason"] == NOT_SUPPORTED_TOOLS[tool]
    assert payload["supported_tools"] == sorted(SUPPORTED_TOOLS)
    assert graph.calls == [], "a refused tool must never reach the graph API"
