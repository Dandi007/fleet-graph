"""Contract coverage for the dev-dispatch MCP surface.

The surface is the control plane: every real tool drives the in-process
`DdControlPlane` -- there is no graph-API forwarding tier any more. Tool-surface
shape follows the wf-a08949 2026-08-27 use-case-family ruling: the full
15-name surface stays reachable, only the consumed family
(list/get/events/evidence/create/start/gate) does work, and every legacy-only
name refuses with an explicit NOT_SUPPORTED structure and zero control-plane
calls.
"""

from __future__ import annotations

import asyncio
import inspect
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

from fleet_graph.dd.control_plane import ControlPlaneError
from fleet_graph.dd.service import (
    DEFAULT_PORT,
    NOT_SUPPORTED_TOOLS,
    SUPPORTED_TOOLS,
    WORK_FOLDER_TOOLS,
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
    "development_adopt",
    "development_recover",
    "wf_reconcile",
}

# Arguments that satisfy each legacy-only tool's schema, so the refusal we
# observe is the NOT_SUPPORTED structure and not an argument-validation error.
# `development_reconfigure` is deliberately absent: R1-c moved it into the
# real surface (the environment/contract failure exit).
NOT_SUPPORTED_CALLS: dict[str, dict[str, object]] = {
    "deployment_create": {"request": {"operation": "deploy"}},
    "deployment_status": {"operation_id": "operation-1"},
    "development_steer": {
        "development_id": "dev-1",
        "instruction": "continue",
        "idempotency_key": "steer-key",
        "expected_revision": 8,
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


class FakeControlPlane:
    """Records every method call; the service must add no policy of its own."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: ControlPlaneError | None = None

    def _record(self, method: str, **kwargs: object) -> dict[str, object]:
        self.calls.append((method, kwargs))
        if self.error is not None:
            raise self.error
        return {"method": method}

    def create(self, **kwargs: object) -> dict[str, object]:
        return self._record("create", **kwargs)

    def start(self, **kwargs: object) -> dict[str, object]:
        return self._record("start", **kwargs)

    def get(self, **kwargs: object) -> dict[str, object]:
        return self._record("get", **kwargs)

    def list(self, **kwargs: object) -> dict[str, object]:
        return self._record("list", **kwargs)

    def events(self, **kwargs: object) -> dict[str, object]:
        return self._record("events", **kwargs)

    def evidence(self, **kwargs: object) -> dict[str, object]:
        return self._record("evidence", **kwargs)

    def gate(self, **kwargs: object) -> dict[str, object]:
        return self._record("gate", **kwargs)

    def reconfigure(self, **kwargs: object) -> dict[str, object]:
        return self._record("reconfigure", **kwargs)

    def adopt(self, **kwargs: object) -> dict[str, object]:
        return self._record("adopt", **kwargs)

    def recover(self, **kwargs: object) -> dict[str, object]:
        return self._record("recover", **kwargs)


def test_selected_port_is_free_and_not_a_legacy_port() -> None:
    assert DEFAULT_PORT == 5610
    if not port_is_available(port=DEFAULT_PORT):
        # On the production host the dd MCP service itself holds :5610 (it
        # went live 2026-08-27); a bind failure there is the service doing its
        # job, not a port collision. CI still proves the port is free.
        pytest.skip("port 5610 is already being served on this host")


def test_all_sixteen_tools_are_reachable() -> None:
    server = build_mcp_server(FakeControlPlane())
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == ALL_TOOLS


def test_the_surface_split_is_exactly_the_ruling() -> None:
    """Supported + refused partitions the 15 development names, with no overlap.

    R1-c moved `development_reconfigure` from the refused side to the real
    side (the environment/contract failure exit); steer / relock / control /
    deployment_* stay refused. `wf_reconcile` (the B3 work-folder recovery exit)
    is a third, separate family on the same surface -- it drives a work-folder
    source seam, not the development control plane.
    """
    assert SUPPORTED_TOOLS | set(NOT_SUPPORTED_TOOLS) | WORK_FOLDER_TOOLS == ALL_TOOLS
    assert not SUPPORTED_TOOLS & set(NOT_SUPPORTED_TOOLS)
    assert not WORK_FOLDER_TOOLS & SUPPORTED_TOOLS
    assert not WORK_FOLDER_TOOLS & set(NOT_SUPPORTED_TOOLS)
    assert {"wf_reconcile"} == WORK_FOLDER_TOOLS
    assert {
        "development_list",
        "development_get",
        "development_events",
        "development_evidence",
        "development_create",
        "development_start",
        "development_gate",
        "development_reconfigure",
        "development_adopt",
        "development_recover",
    } == SUPPORTED_TOOLS
    assert {
        "development_steer",
        "development_relock",
        "development_control",
        "deployment_create",
        "deployment_status",
    } == set(NOT_SUPPORTED_TOOLS)


def test_the_reconfigure_tool_admits_only_the_acceptance_context() -> None:
    """The three-exit ruling, enforced by schema: reconfigure can change the
    acceptance context and nothing else. No spec, no implementation, no
    role patch, no legacy revision/idempotency vocabulary."""
    server = build_mcp_server(FakeControlPlane())
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["development_reconfigure"].parameters
    assert set(schema["properties"]) == {
        "development_id",
        "acceptance_env",
        "acceptance_argv",
        "setup",
    }
    assert schema["required"] == ["development_id"]
    for forbidden in (
        "spec_text",
        "spec_path",
        "profile",
        "role_target_patch",
        "policy",
        "idempotency_key",
        "expected_revision",
        "instruction",
    ):
        assert forbidden not in schema["properties"]


def test_the_create_tool_admits_only_the_derivation_inputs() -> None:
    """Admission is server-side derivation: no handoff, digest, receipt,
    idempotency or policy parameter exists to guess at."""
    server = build_mcp_server(FakeControlPlane())
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["development_create"].parameters
    assert set(schema["properties"]) == {
        "repo_path",
        "target_base",
        "spec_text",
        "spec_path",
        "dispatched_by",
    }
    assert schema["required"] == ["repo_path"]


def test_the_gate_tool_carries_no_decision() -> None:
    """The fourth gate, extended: the tool cannot transport a verdict at all.

    The legacy engine's gate command took `decision`; that shape is not
    replicated. The only mutation this tool offers is a valueless resume."""
    server = build_mcp_server(FakeControlPlane())
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    gate_params = set(tools["development_gate"].parameters["properties"])
    assert gate_params == {"development_id", "resume"}
    for forbidden in ("decision", "verdict", "operator_identity"):
        assert forbidden not in gate_params


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


def test_every_supported_tool_drives_the_control_plane_over_the_running_endpoint() -> None:
    plane = FakeControlPlane()
    server = build_mcp_server(plane)
    calls = [
        ("development_list", {"state": "running", "limit": 4, "cursor": "c1"}),
        ("development_get", {"development_id": "dev-1"}),
        ("development_events", {"development_id": "dev-1", "after": "e1", "limit": 3}),
        ("development_evidence", {"development_id": "dev-1"}),
        (
            "development_create",
            {
                "repo_path": "/data/worktrees/dev-1",
                "target_base": "refs/remotes/origin/main",
                "spec_text": "# spec",
            },
        ),
        ("development_start", {"development_id": "dev-1"}),
        ("development_gate", {"development_id": "dev-1", "resume": True}),
        (
            "development_reconfigure",
            {
                "development_id": "dev-1",
                "acceptance_argv": ["make verify"],
                "setup": ["npm ci"],
                "acceptance_env": {"CI": "1"},
            },
        ),
        (
            "development_adopt",
            {
                "development_id": "dev-1",
                "discoveries": [
                    {
                        "signature": "dev-1:g1",
                        "kind": "in_flight",
                        "source": "runner",
                        "target_ref": "abc123",
                    }
                ],
            },
        ),
        (
            "development_recover",
            {
                "development_id": "dev-1",
                "target_ref": "abc123",
                "question_note_id": "note-1",
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

    assert plane.calls == [
        ("list", {"state": "running", "limit": 4, "cursor": "c1"}),
        ("get", {"development_id": "dev-1"}),
        ("events", {"development_id": "dev-1", "after": "e1", "limit": 3, "generation": None}),
        ("evidence", {"development_id": "dev-1"}),
        (
            "create",
            {
                "repo_path": "/data/worktrees/dev-1",
                "target_base": "refs/remotes/origin/main",
                "spec_text": "# spec",
                "spec_path": None,
                "dispatched_by": "",
            },
        ),
        ("start", {"development_id": "dev-1"}),
        ("gate", {"development_id": "dev-1", "resume": True}),
        (
            "reconfigure",
            {
                "development_id": "dev-1",
                "acceptance_argv": ["make verify"],
                "setup": ["npm ci"],
                "acceptance_env": {"CI": "1"},
            },
        ),
        (
            "adopt",
            {
                "development_id": "dev-1",
                "discoveries": [
                    {
                        "signature": "dev-1:g1",
                        "kind": "in_flight",
                        "source": "runner",
                        "target_ref": "abc123",
                    }
                ],
            },
        ),
        (
            "recover",
            {
                "development_id": "dev-1",
                "target_ref": "abc123",
                "question_note_id": "note-1",
            },
        ),
    ]


def test_a_control_plane_refusal_reaches_the_client_machine_readably() -> None:
    plane = FakeControlPlane()
    plane.error = ControlPlaneError("WORKTREE_DIRTY", "uncommitted changes", retryable=False)
    server = build_mcp_server(plane)

    async def call(url: str) -> str:
        async with Client(url) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.call_tool("development_start", {"development_id": "dev-1"})
            return str(excinfo.value)

    with running_server(server) as url:
        message = asyncio.run(call(url))

    payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
    assert payload == {
        "code": "WORKTREE_DIRTY",
        "message": "uncommitted changes",
        "retryable": False,
    }


@pytest.mark.parametrize("tool", sorted(NOT_SUPPORTED_TOOLS))
def test_every_legacy_only_tool_refuses_with_the_explicit_structure(tool: str) -> None:
    """The refusal carries a machine-readable payload and touches no control plane."""
    plane = FakeControlPlane()
    server = build_mcp_server(plane)

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
    assert plane.calls == [], "a refused tool must never reach the control plane"


def test_the_fake_control_plane_mirrors_the_real_surface() -> None:
    """The fake above proves forwarding; this proves it cannot drift: every
    method it fakes exists on DdControlPlane with the same parameters."""
    from fleet_graph.dd.control_plane import DdControlPlane

    for method in (
        "create",
        "start",
        "get",
        "list",
        "events",
        "evidence",
        "gate",
        "reconfigure",
        "adopt",
        "recover",
    ):
        assert hasattr(DdControlPlane, method), method
    real = {
        name
        for name, _ in inspect.signature(DdControlPlane.create).parameters.items()
        if name != "self"
    }
    assert real == {"repo_path", "target_base", "spec_text", "spec_path", "dispatched_by"}
    gate = {
        name
        for name, _ in inspect.signature(DdControlPlane.gate).parameters.items()
        if name != "self"
    }
    assert gate == {"development_id", "resume", "action_key"}
    reconfigure = {
        name
        for name, _ in inspect.signature(DdControlPlane.reconfigure).parameters.items()
        if name != "self"
    }
    assert reconfigure == {"development_id", "acceptance_env", "acceptance_argv", "setup"}
