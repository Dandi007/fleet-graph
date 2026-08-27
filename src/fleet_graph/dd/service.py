"""The localhost dev-dispatch MCP surface backed by fleet-graph APIs.

The service deliberately contains no lifecycle policy.  It preserves the public
tool contracts and forwards requests to the graph control-plane, whose API owns
admission, revision, idempotency, receipts, and evidence validation.
"""

from __future__ import annotations

import json
import socket
from typing import Any, cast

import httpx

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5610
DEFAULT_GRAPH_API_URL = "http://127.0.0.1:5611"


class GraphApiError(RuntimeError):
    """The graph control-plane refused a request or could not be reached."""


class GraphApi:
    """Small HTTP boundary so MCP remains a transport adapter, not a second engine."""

    def __init__(self, base_url: str = DEFAULT_GRAPH_API_URL) -> None:
        self.base_url = base_url.rstrip("/")

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, json=body)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = httpx.request(method, f"{self.base_url}{path}", timeout=30.0, **kwargs)
        except httpx.HTTPError as exc:
            raise GraphApiError(f"graph API {method} {path} failed: {exc}") from exc
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text
        if not response.is_success:
            detail = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
            raise GraphApiError(
                f"graph API {method} {path} returned HTTP {response.status_code}: {detail}"
            )
        if not isinstance(payload, dict):
            raise GraphApiError(
                f"graph API {method} {path} returned {type(payload).__name__}, expected object"
            )
        return cast(dict[str, Any], payload)


def port_is_available(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Bind-test the selected loopback port before FastMCP tries to serve it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def build_mcp_server(api: GraphApi | None = None) -> Any:
    """Build all active dev-dispatch tools without importing a legacy engine."""
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    graph = api or GraphApi()
    mcp = FastMCP("fleet-graph-dev-dispatch")

    def get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return graph.get(path, params)
        except GraphApiError as exc:
            raise ToolError(str(exc)) from exc

    def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return graph.post(path, body)
        except GraphApiError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    def deployment_create(request: dict[str, Any]) -> dict[str, Any]:
        """Admit one verified deployment compatibility operation."""
        return post("/v1/deployments", {"request": request})

    @mcp.tool()
    def deployment_status(operation_id: str) -> dict[str, Any]:
        """Return the durable projection for a deployment operation."""
        return get(f"/v1/deployments/{operation_id}")

    @mcp.tool()
    def development_list(
        state: str | None = None,
        repo: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List development summaries using graph-owned pagination."""
        return get(
            "/v1/developments", {"state": state, "repo": repo, "limit": limit, "cursor": cursor}
        )

    @mcp.tool()
    def development_get(development_id: str) -> dict[str, Any]:
        return get(f"/v1/developments/{development_id}")

    @mcp.tool()
    def development_events(
        development_id: str, after: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        return get(f"/v1/developments/{development_id}/events", {"after": after, "limit": limit})

    @mcp.tool()
    def development_evidence(development_id: str) -> dict[str, Any]:
        return get(f"/v1/developments/{development_id}/evidence")

    @mcp.tool()
    def development_create(
        name: str,
        goal: str,
        idempotency_key: str,
        reason: str,
        initial_handoff: dict[str, Any],
        phase: str = "development",
        acceptance_commands: list[dict[str, Any]] | None = None,
        setup_commands: list[dict[str, Any]] | None = None,
        host_verify_commands: list[dict[str, Any]] | None = None,
        profile: str = "default",
        policy: str = "isolated-release-auto",
        work_folder: str | None = None,
        role_target_patch: dict[str, Any] | None = None,
        auto_start: bool = False,
    ) -> dict[str, Any]:
        body = {
            "name": name,
            "goal": goal,
            "idempotency_key": idempotency_key,
            "reason": reason,
            "initial_handoff": initial_handoff,
            "phase": phase,
            "profile": profile,
            "policy": policy,
            "auto_start": auto_start,
        }
        for key, value in {
            "acceptance_commands": acceptance_commands,
            "setup_commands": setup_commands,
            "host_verify_commands": host_verify_commands,
            "work_folder": work_folder,
            "role_target_patch": role_target_patch,
        }.items():
            if value is not None:
                body[key] = value
        return post("/v1/developments", body)

    def mutation(
        name: str,
        development_id: str,
        idempotency_key: str,
        expected_revision: int,
        reason: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return post(
            f"/v1/developments/{development_id}/commands/{name}",
            {
                "idempotency_key": idempotency_key,
                "expected_revision": expected_revision,
                "reason": reason,
                **{key: value for key, value in extra.items() if value is not None},
            },
        )

    @mcp.tool()
    def development_start(
        development_id: str, idempotency_key: str, expected_revision: int, reason: str = ""
    ) -> dict[str, Any]:
        return mutation("start", development_id, idempotency_key, expected_revision, reason)

    @mcp.tool()
    def development_steer(
        development_id: str,
        instruction: str,
        idempotency_key: str,
        expected_revision: int,
        reason: str = "",
        urgency: str = "next_safe_boundary",
    ) -> dict[str, Any]:
        return mutation(
            "steer",
            development_id,
            idempotency_key,
            expected_revision,
            reason,
            instruction=instruction,
            urgency=urgency,
        )

    @mcp.tool()
    def development_reconfigure(
        development_id: str,
        idempotency_key: str,
        expected_revision: int,
        reason: str = "",
        profile: str | None = None,
        role_target_patch: dict[str, Any] | None = None,
        policy: str | None = None,
        acceptance_commands: list[dict[str, Any]] | None = None,
        setup_commands: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return mutation(
            "reconfigure",
            development_id,
            idempotency_key,
            expected_revision,
            reason,
            profile=profile,
            role_target_patch=role_target_patch,
            policy=policy,
            acceptance_commands=acceptance_commands,
            setup_commands=setup_commands,
        )

    @mcp.tool()
    def development_gate(
        development_id: str,
        gate_id: str,
        decision: str,
        idempotency_key: str,
        expected_revision: int,
        operator_identity: str,
        reason: str = "",
    ) -> dict[str, Any]:
        return mutation(
            "gate",
            development_id,
            idempotency_key,
            expected_revision,
            reason,
            gate_id=gate_id,
            decision=decision,
            operator_identity=operator_identity,
        )

    @mcp.tool()
    def development_control(
        development_id: str,
        action: str,
        idempotency_key: str,
        expected_revision: int,
        reason: str = "",
    ) -> dict[str, Any]:
        return mutation(
            "control", development_id, idempotency_key, expected_revision, reason, action=action
        )

    @mcp.tool()
    def development_relock(
        development_id: str,
        plugin_commit: str,
        idempotency_key: str,
        expected_revision: int,
        reason: str = "",
    ) -> dict[str, Any]:
        return mutation(
            "relock",
            development_id,
            idempotency_key,
            expected_revision,
            reason,
            plugin_commit=plugin_commit,
        )

    return mcp


def serve(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, api_url: str = DEFAULT_GRAPH_API_URL
) -> None:
    if not port_is_available(host, port):
        raise RuntimeError(f"fleet-graph dev-dispatch port {host}:{port} is unavailable")
    build_mcp_server(GraphApi(api_url)).run(
        transport="streamable-http", host=host, port=port, path="/mcp"
    )


__all__ = [
    "DEFAULT_GRAPH_API_URL",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "GraphApi",
    "GraphApiError",
    "build_mcp_server",
    "port_is_available",
    "serve",
]
