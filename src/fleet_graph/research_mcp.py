"""The research MCP surface: ``fleet-graph research serve`` on loopback.

The research surface is its own service (not a guest of the dev-dispatch or
goal MCP): it serves exactly the deep-research entry family -- the
``research_run`` tool -- on its own port (:5612), registered as
``fleet-graph-research``. The tool is a *surface* over the shared unified
entry (``research_entry.run_research_ticket``), the same route the CLI
``research run`` and the deep-research skill use: three surfaces, one routing
determination (宪法条6「入口唯一」/ 条8「使用闭环」).

``research_run`` is an application entry, not an ignition: it runs one
research ticket to termination through the unified entry, resolving the
light/heavy tier deterministically and placing the final report into the wiki
domain ``DeepThought/<topic>/``. It offers no management tools -- tier
routing, run identity and report placement are all the unified entry's
business, not this surface's.

Fail-fast root binding: ``research serve`` refuses to start when the wiki
root is unbound (neither ``--wiki-root`` nor ``FLEET_GRAPH_WIKI_ROOT`` set) --
a research MCP without a wiki root would only misplace its reports on the
first real call. The default root (``/data/vault``) still requires the flag to
be explicit, so the service never silently writes to an assumed host path.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from fleet_graph.research_entry import WIKI_ROOT_ENV, run_research_ticket

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5612

#: The FastMCP registration name of this surface (what a client sees in
#: tools/list's server name, distinct from the dev-dispatch / goal servers).
MCP_SERVER_NAME = "fleet-graph-research"


def port_is_available(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Bind-test the selected loopback port before FastMCP tries to serve it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def build_research_mcp_server(wiki_root: str | None = None) -> Any:
    """Build the standalone research MCP surface.

    ``wiki_root`` optionally binds the wiki-domain report placement root; when
    ``None`` the tool still exists but falls back to the environment default
    (``FLEET_GRAPH_WIKI_ROOT`` or ``/data/vault``) -- and ``research serve``
    itself never starts that way, so production cannot reach the unbound route.
    """
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    mcp = FastMCP(MCP_SERVER_NAME)

    def refuse(message: str) -> None:
        raise ToolError(json.dumps({"code": "RESEARCH_RUN_REFUSED", "message": message}))

    @mcp.tool()
    def research_run(
        question: str,
        tier: str | None = None,
        scale: int | None = None,
        run_root: str | None = None,
    ) -> dict[str, Any]:
        """Run one deep-research ticket to termination through the unified entry.

        The light/heavy tier is resolved deterministically by the unified entry
        (``research_entry.resolve_tier``): pass an explicit ``tier`` (``light`` /
        ``heavy``) or let the scale routing decide. The final report is placed
        into the wiki domain ``DeepThought/<topic>/`` on finalise. Returns the
        run's terminal record plus ``tier`` and ``wiki`` placement info.
        """
        if not question or not str(question).strip():
            refuse("question is required")
        try:
            from fleet_graph.research_coldstart import LAUNCH_ENTRY_MCP, mcp_launch_argv

            return run_research_ticket(
                str(question),
                tier=tier,
                scale=scale,
                run_root=Path(run_root) if run_root else None,
                wiki_root=wiki_root,
                # R8 判据 ①：MCP 无 sys.argv 语义，落 tool 真实调用签名 + entry=mcp，
                # 不得用 CLI canonical 重建值冒充。
                launch_argv=mcp_launch_argv(
                    str(question),
                    tier=tier,
                    scale=scale,
                    run_root=Path(run_root) if run_root else None,
                ),
                launch_entry=LAUNCH_ENTRY_MCP,
            )
        except Exception as exc:
            raise ToolError(
                json.dumps(
                    {"code": "RESEARCH_RUN_FAILED", "message": f"{type(exc).__name__}: {exc}"}
                )
            ) from exc

    return mcp


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    wiki_root: str | None = None,
) -> None:
    """Run the standalone research MCP surface on loopback.

    Two startup refusals, both visible (never a silent half-broken service):

    - **Root unbound**: neither ``--wiki-root`` nor ``FLEET_GRAPH_WIKI_ROOT`` is
      set. The wiki-domain placement root is mandatory so reports land where the
      wiki discipline says they do.
    - **Port taken**: an occupied :5612 is a visible refusal, not a crash loop.
    """
    root = wiki_root if wiki_root is not None else os.environ.get(WIKI_ROOT_ENV)
    if root in (None, ""):
        raise RuntimeError(
            "no --wiki-root and no "
            f"{WIKI_ROOT_ENV} in the environment; a research MCP without a bound "
            "wiki root would misplace its reports on first use"
        )
    if not port_is_available(host, port):
        raise RuntimeError(f"fleet-graph research port {host}:{port} is unavailable")
    build_research_mcp_server(wiki_root=root).run(
        transport="streamable-http", host=host, port=port, path="/mcp"
    )


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MCP_SERVER_NAME",
    "build_research_mcp_server",
    "port_is_available",
    "serve",
]
