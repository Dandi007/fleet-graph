"""The goal-driven MCP surface: ``fleet-graph goal serve`` on loopback.

The goal-driven MCP surface is its own service, not a guest of the
dev-dispatch MCP. It serves exactly the goal-driven family -- the ``goal_enroll``
tool, the versioned ``goal-open`` briefing prompt, and the
``fleet-graph://goal-open/briefing`` resource -- on its own port (:5611),
registered as ``fleet-graph-goal``. dd (:5610) carries no goal-driven
registrations any more.

The ``goal_enroll`` machinery itself is untouched: the fail-closed validator,
the refusal codes, the roster registry, and the briefing versioning all come
from :mod:`fleet_graph.goal_enroll` unchanged. This module is only the
surface that binds them to the standalone service.

Fail-fast root binding: ``goal serve`` refuses to start when neither
``--work-folder-root`` nor ``FLEET_GRAPH_WORK_FOLDER_ROOT`` is set. A goal MCP
without a bound goal-folder root would only discover ``GOAL_ENROLL_SOURCE_UNBOUND``
on the first real call -- the registered-but-unbound family bug this service
refuses to ship. Startup-time visible failure replaces the runtime half-broken
service.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

from fleet_graph.goal_enroll.briefing import BRIEFING_TEXT, goal_open_prompt_text
from fleet_graph.goal_enroll.contract import (
    BRIEFING_RESOURCE_URI,
    BRIEFING_VERSION,
    GOAL_OPEN_PROMPT_NAME,
    GoalEnrollError,
)
from fleet_graph.goal_enroll.service import GoalEnrollService
from fleet_graph.goal_enroll.source import governed_goal_folder_store
from fleet_graph.goal_enroll.store import GoalEnrollRoster
from fleet_graph.goal_enroll.validator import GoalEnrollValidator

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5611

#: The FastMCP registration name of this surface (what a client sees in
#: tools/list's server name, distinct from the dev-dispatch server).
MCP_SERVER_NAME = "fleet-graph-goal"

#: The directory that owns one governed goal-folder repository per folder id.
#: ``goal serve`` binds the concrete ``goal_enroll`` source from this root (or
#: the ``--work-folder-root`` flag); a service without it refuses to start.
WORK_FOLDER_ROOT_ENV = "FLEET_GRAPH_WORK_FOLDER_ROOT"


def port_is_available(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Bind-test the selected loopback port before FastMCP tries to serve it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def build_goal_mcp_server(
    goal_folders: Any | None = None,
    goal_roster: GoalEnrollRoster | None = None,
) -> Any:
    """Build the standalone goal-driven MCP surface.

    ``goal_folders`` optionally binds the goal-line enroll exit to a governed
    goal-folder source seam; ``goal_roster`` optionally binds the persistent
    roster registry. When ``goal_folders`` is ``None`` the tool still exists
    but refuses with ``GOAL_ENROLL_SOURCE_UNBOUND`` (the validator's own
    fail-closed answer) -- and ``goal serve`` itself never starts that way, so
    production cannot reach the unbound route.
    """
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    enroll = GoalEnrollService(
        GoalEnrollValidator(goal_folders),
        roster=goal_roster if goal_roster is not None else GoalEnrollRoster(),
    )
    mcp = FastMCP(MCP_SERVER_NAME)

    def refuse_enroll(tool: str, exc: GoalEnrollError) -> dict[str, Any]:
        """Raise the machine-readable goal_enroll refusal structure."""
        raise ToolError(
            json.dumps(
                {
                    "code": exc.code,
                    "message": exc.detail,
                    "tool": tool,
                    "briefing_version": BRIEFING_VERSION,
                },
                sort_keys=True,
            )
        )

    # The versioned opening briefing (交底), registered as both a prompt and a
    # versioned resource so the handoff is pinned to this engine release. The
    # roster entries `goal_enroll` admits record the same BRIEFING_VERSION.
    @mcp.prompt(name=GOAL_OPEN_PROMPT_NAME)
    def goal_open() -> str:
        """The Phase-0 goal-line opening briefing (交底), engine-versioned."""
        return goal_open_prompt_text()

    @mcp.resource(BRIEFING_RESOURCE_URI)
    def goal_open_briefing() -> str:
        """The versioned briefing text behind the goal-open prompt."""
        return BRIEFING_TEXT

    @mcp.tool()
    def goal_enroll(folder_id: str) -> dict[str, Any]:
        """Admit one goal line to the roster, fail-closed, versioned.

        Validates the candidate goal folder against every gate -- folder is a
        goal line, goal.md declares executable acceptance argv, golden-order.md
        is present and non-empty, spec-lint bans are clean, and every declared
        acceptance command starts in a throwaway liveness probe -- and only then
        records the engine-versioned roster entry (briefing version id
        included). A refusal is an explicit, machine-readable error with a
        stable code and the failing clause; there is never a partial entry.
        """
        try:
            return enroll.enroll(folder_id)
        except GoalEnrollError as exc:
            return refuse_enroll("goal_enroll", exc)

    return mcp


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    work_folder_root: str | None = None,
) -> None:
    """Run the standalone goal-driven MCP surface on loopback.

    Two startup refusals, both visible (never a silent half-broken service):

    - **Root unbound**: neither ``--work-folder-root`` nor
      ``FLEET_GRAPH_WORK_FOLDER_ROOT`` is set. This is the structural fix for
      the ``GOAL_ENROLL_SOURCE_UNBOUND`` family: the service refuses to start
      instead of serving a route that fails on first use.
    - **Port taken**: the same ``port_is_available`` discipline the dd surface
      uses -- an occupied :5611 is a visible refusal, not a crash loop.
    """
    root = (
        work_folder_root if work_folder_root is not None else os.environ.get(WORK_FOLDER_ROOT_ENV)
    )
    if root in (None, ""):
        raise RuntimeError(
            "no --work-folder-root and no "
            f"{WORK_FOLDER_ROOT_ENV} in the environment; a goal MCP without a bound "
            "goal-folder root would report GOAL_ENROLL_SOURCE_UNBOUND on first use"
        )
    if not port_is_available(host, port):
        raise RuntimeError(f"fleet-graph goal port {host}:{port} is unavailable")
    goal_folders = governed_goal_folder_store(root)
    goal_roster = GoalEnrollRoster(root)
    build_goal_mcp_server(goal_folders=goal_folders, goal_roster=goal_roster).run(
        transport="streamable-http", host=host, port=port, path="/mcp"
    )


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MCP_SERVER_NAME",
    "WORK_FOLDER_ROOT_ENV",
    "build_goal_mcp_server",
    "port_is_available",
    "serve",
]
