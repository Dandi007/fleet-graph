"""The localhost dev-dispatch MCP surface. The service *is* the control plane.

The supervision plane struck the separate graph-API tier this surface used to
forward to (:5611): there is no second service behind these tools. Every real
tool drives `fleet_graph.dd.control_plane` in-process -- admission derivation,
transient-unit launches, and read-side assembly from git + checkpoint + run
artifacts all happen right here.

Tool surface (wf-a08949 goal.md 2026-08-27 use-case-family ruling; wf-13ff9e
plan.md §1 R1-d, extended by R1-c): the consumed use-case family does work --
``development_list / get / events / evidence / create / start / gate /
reconfigure``.  ``reconfigure`` is the R1-c environment/contract failure exit:
on the legacy engine it existed in name but was a permanent 409 once a
development FAILED; here it is real, scoped by schema to the acceptance
context alone, and pairs with ``start`` launching a fresh generation.  The
remaining legacy tool names stay registered so every historical caller gets an
explicit, machine-readable ``NOT_SUPPORTED`` refusal instead of an unknown-tool
error, but they perform no work: ``steer`` was a permanent 409 on the legacy
engine and is not replicated; ``relock`` / ``control`` / ``deployment_*``
belong to the legacy engine's patch surface and are outside the equivalence
scope.

Two contracts the tools themselves enforce:

- **Admission is server-side derivation.** ``development_create`` takes a repo
  path, a target base, and the spec. There is no handoff parameter, no digest
  parameter, no receipt parameter -- the whole vocabulary a client used to
  have to guess is derived by the server and returned, not requested.
- **The gate carries no verdict.** ``development_gate`` reports the pending
  question note and offers a valueless ``resume``; on resume the graph
  re-reads the board itself. Decisions travel only as ``work.decision.v1`` on
  the bus, published by a human.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from fleet_graph.dd.control_plane import ControlPlaneError, DdControlPlane

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5610

# Legacy tool names that are registered but refuse with an explicit error
# structure instead of pretending the legacy semantics exist here.
# name -> reason, quoted in the refusal payload.
NOT_SUPPORTED_TOOLS: dict[str, str] = {
    "development_steer": ("steer was a permanent 409 on the legacy engine and is not replicated"),
    "development_relock": "relock belongs to the legacy engine's patch surface",
    "development_control": (
        "control is outside the consumed use-case family "
        "(create/start/get/list/events/evidence/gate)"
    ),
    "deployment_create": "deployment_* belongs to the legacy engine's patch surface",
    "deployment_status": "deployment_* belongs to the legacy engine's patch surface",
}

NOT_SUPPORTED_RULING = "wf-a08949 goal.md 2026-08-27 use-case-family ruling"

# The consumed use-case family: the only tools that do real work.
SUPPORTED_TOOLS: frozenset[str] = frozenset(
    {
        "development_list",
        "development_get",
        "development_events",
        "development_evidence",
        "development_create",
        "development_start",
        "development_gate",
        "development_reconfigure",
    }
)


def port_is_available(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Bind-test the selected loopback port before FastMCP tries to serve it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def build_mcp_server(plane: DdControlPlane | None = None) -> Any:
    """Build all active dev-dispatch tools over the in-process control plane."""
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    control = plane or DdControlPlane()
    mcp = FastMCP("fleet-graph-dev-dispatch")

    def call(method: str, /, **kwargs: Any) -> dict[str, Any]:
        try:
            return dict(getattr(control, method)(**kwargs))
        except ControlPlaneError as exc:
            raise ToolError(json.dumps(exc.to_dict(), sort_keys=True)) from exc

    def refuse(tool: str) -> dict[str, Any]:
        """Raise the explicit NOT_SUPPORTED structure for a legacy-only tool."""
        raise ToolError(
            json.dumps(
                {
                    "code": "NOT_SUPPORTED",
                    "tool": tool,
                    "reason": NOT_SUPPORTED_TOOLS[tool],
                    "ruling": NOT_SUPPORTED_RULING,
                    "supported_tools": sorted(SUPPORTED_TOOLS),
                },
                sort_keys=True,
            )
        )

    @mcp.tool()
    def deployment_create(request: dict[str, Any]) -> dict[str, Any]:
        """NOT_SUPPORTED: legacy patch-surface tool, refuses explicitly."""
        return refuse("deployment_create")

    @mcp.tool()
    def deployment_status(operation_id: str) -> dict[str, Any]:
        """NOT_SUPPORTED: legacy patch-surface tool, refuses explicitly."""
        return refuse("deployment_status")

    @mcp.tool()
    def development_list(
        state: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List development statuses. O(n) over the run artifacts, by ruling."""
        return call("list", state=state, limit=limit, cursor=cursor)

    @mcp.tool()
    def development_get(development_id: str) -> dict[str, Any]:
        """One development's admission record plus its recomputed live status."""
        return call("get", development_id=development_id)

    @mcp.tool()
    def development_events(
        development_id: str,
        after: str | None = None,
        limit: int = 100,
        generation: int | None = None,
    ) -> dict[str, Any]:
        """One generation's event log (events.jsonl), paged by event id.

        Defaults to the current generation; pass `generation` to read an
        earlier one's history.
        """
        return call(
            "events",
            development_id=development_id,
            after=after,
            limit=limit,
            generation=generation,
        )

    @mcp.tool()
    def development_evidence(development_id: str) -> dict[str, Any]:
        """The evidence entry, assembled live from git + checkpoint + receipts."""
        return call("evidence", development_id=development_id)

    @mcp.tool()
    def development_create(
        repo_path: str,
        target_base: str | None = None,
        spec_text: str | None = None,
        spec_path: str | None = None,
    ) -> dict[str, Any]:
        """Admit one development. Everything else is derived server-side.

        Takes a dedicated git worktree (or clone) path, an optional target
        base (defaults to the repo's HEAD), and the approved spec as text or
        as a path. The server derives the development id, freezes the spec
        and target base into the bootstrap commit, computes the H0 handoff
        and its chain-root digest, derives the durable ref and the acceptance
        argv (from the spec's ```dd-acceptance block), and publishes the work
        board card. Idempotent for the same (repo, spec, base).
        """
        return call(
            "create",
            repo_path=repo_path,
            target_base=target_base,
            spec_text=spec_text,
            spec_path=spec_path,
        )

    @mcp.tool()
    def development_start(development_id: str) -> dict[str, Any]:
        """Run the development detached in a transient systemd unit.

        The thread identity is `{development_id}:g{generation}`: starting
        again after a kill resumes the same generation's thread and re-adopts
        agent runs in flight instead of re-dispatching sealed stages, while
        starting after a retryable terminal (or after a reconfigure) launches
        the next generation fresh -- new thread id, new derived run ids, new
        gate idempotency key, so a rerun never collides with its own past. A
        fabrication terminal refuses (final), and so does `complete`.
        Starting a development that is already running is a no-op that says
        so.
        """
        return call("start", development_id=development_id)

    @mcp.tool()
    def development_gate(development_id: str, resume: bool = False) -> dict[str, Any]:
        """The human gate's state; optionally resume the suspended thread.

        This tool accepts **no decision**. It reports the question note the
        gate is waiting on, and `resume=True` re-enters the suspended thread
        with no input at all -- the gate re-reads the board itself, so the
        caller cannot cast a verdict by resuming. Decisions travel only as
        `work.decision.v1` messages on the bus, published by a human, with
        `refs=[{"target_entity": <question_note_id>}]`.
        """
        return call("gate", development_id=development_id, resume=resume)

    @mcp.tool()
    def development_steer(
        development_id: str,
        instruction: str,
        idempotency_key: str,
        expected_revision: int,
        reason: str = "",
        urgency: str = "next_safe_boundary",
    ) -> dict[str, Any]:
        """NOT_SUPPORTED: permanent 409 on the legacy engine, refuses explicitly."""
        return refuse("development_steer")

    @mcp.tool()
    def development_reconfigure(
        development_id: str,
        acceptance_env: dict[str, str] | None = None,
        acceptance_argv: list[str] | None = None,
        setup: list[str] | None = None,
    ) -> dict[str, Any]:
        """Change a development's acceptance context -- and nothing else.

        The environment/contract failure exit (R1-c): callable while the
        development is FAILED and in every non-terminal state, so an
        acceptance environment problem (missing piece, wrong acceptance argv,
        missing setup) no longer kills the development the way the legacy
        engine's permanent 409 did. After reconfiguring, `development_start`
        launches a fresh generation with the new context.

        The scope is the schema: `acceptance_env` (env overlay for setup and
        acceptance commands), `acceptance_argv` (acceptance command lines,
        shell quoting honoured), `setup` (setup command lines run first).
        There is no spec parameter and no implementation parameter -- the
        spec stays frozen under its bootstrap digest, and a changed spec is a
        new development. A fabrication terminal (UNVERIFIED_TEST_CLAIM
        family) refuses: that exit is final.
        """
        return call(
            "reconfigure",
            development_id=development_id,
            acceptance_env=acceptance_env,
            acceptance_argv=acceptance_argv,
            setup=setup,
        )

    @mcp.tool()
    def development_control(
        development_id: str,
        action: str,
        idempotency_key: str,
        expected_revision: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """NOT_SUPPORTED: outside the consumed use-case family, refuses explicitly."""
        return refuse("development_control")

    @mcp.tool()
    def development_relock(
        development_id: str,
        plugin_commit: str,
        idempotency_key: str,
        expected_revision: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """NOT_SUPPORTED: legacy patch-surface tool, refuses explicitly."""
        return refuse("development_relock")

    return mcp


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    root: str | None = None,
    plugin_binding: str | None = None,
    working_directory: str | None = None,
    executable: str | None = None,
    stage_models: dict[str, str] | None = None,
) -> None:
    if not port_is_available(host, port):
        raise RuntimeError(f"fleet-graph dev-dispatch port {host}:{port} is unavailable")
    overrides: dict[str, Any] = {}
    if root:
        overrides["root"] = Path(root)
    if plugin_binding:
        overrides["plugin_binding"] = Path(plugin_binding)
    if working_directory:
        overrides["working_directory"] = working_directory
    if executable:
        overrides["executable"] = executable
    if stage_models:
        overrides["stage_models"] = stage_models
    build_mcp_server(DdControlPlane(**overrides)).run(
        transport="streamable-http", host=host, port=port, path="/mcp"
    )


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "NOT_SUPPORTED_RULING",
    "NOT_SUPPORTED_TOOLS",
    "SUPPORTED_TOOLS",
    "build_mcp_server",
    "port_is_available",
    "serve",
]
