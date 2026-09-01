"""The goal-driven MCP surface, served independently of dev-dispatch.

This package owns the standalone ``fleet-graph goal serve`` service (:5611,
registered as ``fleet-graph-goal``): the ``goal_enroll`` tool, the supervisor-
only ``goal_admit`` release tool, the ``goal-open`` briefing prompt, and the
``fleet-graph://goal-open/briefing`` resource live here -- not on the
dev-dispatch surface. dd (:5610) is pure dev-dispatch.
"""

from fleet_graph.goal.service import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    MCP_SERVER_NAME,
    WORK_FOLDER_ROOT_ENV,
    build_goal_mcp_server,
    serve,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MCP_SERVER_NAME",
    "WORK_FOLDER_ROOT_ENV",
    "build_goal_mcp_server",
    "serve",
]
