"""Contract coverage for the graph-backed dev-dispatch MCP surface."""

from __future__ import annotations

import asyncio

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
