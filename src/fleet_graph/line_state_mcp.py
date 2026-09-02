"""The line-state MCP surface: read-only line runtime state (M1).

This is the ``fleet-graph line-state serve`` surface (:5615, registered as
``fleet-graph-line-state``) -- the M1 leg wf-525fd4's goal.md names as the
missing read-model face. It exposes the fleet's line runtime state as a set of
narrow, self-explanatory **read-only** MCP tools over the same view function /
same data source the ``:7494`` read model serves (``FleetStateView.lines()``) --
never a second reader (spec 红线 1).

Field surface (identical to ``:7494 /v1/lines``): ``folder_id / generation /
round / phase / heartbeat_age_s / terminal / parked / wake_facts / release_id /
run_id / wake_facts_stale``.

Tools (spec 交付 2): ``list_line_states`` (every line's state) and
``get_line_state(folder_id)`` (one line's state). Narrow and self-explanatory
-- deliberately **not** a "one call tool + one path param" wrapper of the
native face.

Read-only negative (spec 双向判据): the surface exposes **no write
capability**. The regression test asserts ``tools/list`` and every tool's
``inputSchema`` carry no write primitive (set/update/clear/patch/deliver/wake/
park); adding one turns the suite red.

Undecidable discipline (spec 红线 2): when the line-state source (the roster
that decides which lines exist) is missing / unreadable / malformed, the
surface reports an honest machine-readable ``LINE_STATE_UNDECIDABLE`` refusal
**with evidence** instead of a fabricated empty list, so a test can never
silently go green on an unreachable source.

Port (spec 交付 1, R1/R2): the surface serves loopback :5615. The committed
``config/line-state-mcp-reserved-ports.json`` is the single source of the
occupied/reserved loopback ports; :5615 must never appear in it. The red-able
port assertion in ``tests/test_m1_line_state_mcp.py`` makes a return to an
occupied port (e.g. 5614, now taken by the decision MCP) fail the suite. This
is a CI/acceptance-time assertion, deliberately not a runtime "probe the port
at startup" behavior (spec item 0 R2).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fleet_graph.state.fleet_state import (
    DEFAULT_LINES_CONFIG,
    DEFAULT_RUN_ROOT,
    FleetStateConfig,
    FleetStateView,
)

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5615

#: Path of the committed reserved/occupied loopback port list (R2 single source
#: for this surface, mirroring ``config/decision-mcp-reserved-ports.json``).
RESERVED_PORTS_FILE = (
    Path(__file__).resolve().parent.parent.parent / "config" / "line-state-mcp-reserved-ports.json"
)

#: The FastMCP registration name of this surface (what a client sees in
#: tools/list's server name, distinct from the dev-dispatch / goal / research /
#: decision servers).
MCP_SERVER_NAME = "fleet-graph-line-state"

#: The read-only field surface, identical to ``:7494 /v1/lines``.
LINE_STATE_FIELDS = (
    "folder_id",
    "generation",
    "round",
    "phase",
    "heartbeat_age_s",
    "terminal",
    "parked",
    "wake_facts",
    "release_id",
    "run_id",
    "wake_facts_stale",
)

#: Machine-readable refusal codes (closed).
CODE_UNDECIDABLE = "LINE_STATE_UNDECIDABLE"
CODE_LINE_NOT_FOUND = "LINE_NOT_FOUND"


def load_reserved_ports() -> frozenset[int]:
    """Read the committed reserved/occupied loopback port list.

    R2 single source: ``config/line-state-mcp-reserved-ports.json``. Used by
    the red-able assertion that ``DEFAULT_PORT`` never collides with an
    occupied port. A missing/malformed file is an empty set, so the assertion
    test degrades to a visible failure rather than a false green.
    """
    try:
        raw = json.loads(RESERVED_PORTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    ports = raw.get("reserved_ports") if isinstance(raw, dict) else None
    if not isinstance(ports, list):
        return frozenset()
    return frozenset(int(port) for port in ports if isinstance(port, int))


class LineStateUndecidableError(RuntimeError):
    """The line-state source is unreachable; the answer is undecidable.

    Raised by the core reader when the roster (the source that decides which
    lines exist) cannot be read. The tools translate it into a machine-readable
    ``LINE_STATE_UNDECIDABLE`` refusal with the message as evidence -- never a
    fabricated empty-green list (spec 红线 2).
    """


class LineStateNotFoundError(LookupError):
    """A ``get_line_state`` asked for a folder that is not a roster line."""


def _roster_reachable(config: FleetStateConfig) -> bool:
    """Whether the roster that decides which lines exist is readable.

    Not a second line-state reader: the actual per-line state still comes
    exclusively from ``FleetStateView(config).lines()`` (the same view the
    :7494 server serves). This probe only distinguishes "no lines because the
    source is gone" (undecidable) from "no lines because the roster says so".
    """
    path = Path(config.lines_config)
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and isinstance(raw.get("lines"), list)


def read_line_states(
    config: FleetStateConfig,
    *,
    view: FleetStateView | None = None,
) -> dict[str, Any]:
    """Read every line's state through the same :7494 view function.

    The returned payload is exactly what ``GET /v1/lines`` on :7494 answers:
    ``FleetStateView(config).lines()``, the identical view function and data
    source (spec 阳性判据: same source, field-for-field equal). Raises
    :class:`LineStateUndecidableError` with evidence when the source is
    unreachable.
    """
    if not _roster_reachable(config):
        raise LineStateUndecidableError(
            f"line-state source unreachable: roster {config.lines_config!r} is "
            "missing, unreadable or malformed; the line list is undecidable"
        )
    resolved = view if view is not None else FleetStateView(config)
    return resolved.lines()


def fetch_line_state(
    config: FleetStateConfig,
    folder_id: str,
    *,
    view: FleetStateView | None = None,
) -> dict[str, Any]:
    """One line's state from the same view, or :class:`LineStateNotFoundError`.

    Same read path as :func:`read_line_states` -- never a second reader. An
    unknown ``folder_id`` is an explicit ``LINE_NOT_FOUND`` refusal, not a
    fabricated empty state.
    """
    payload = read_line_states(config, view=view)
    for line in payload.get("lines") or []:
        if line.get("folder_id") == folder_id:
            return line
    raise LineStateNotFoundError(f"no such line: {folder_id!r} is not a roster line")


def build_line_state_mcp_server(
    config: FleetStateConfig,
    *,
    view: FleetStateView | None = None,
) -> Any:
    """Build the read-only line-state MCP surface.

    ``config`` binds the data-source roots (the same ones the :7494 read model
    reads); ``view`` is an injectable seam so tests can drive the surface
    against a scratch ``FleetStateView`` without a transport layer (spec 红线 3,
    same as ``decision_mcp.build_decision_mcp_server``).

    Two narrow, self-explanatory tools -- ``list_line_states`` and
    ``get_line_state(folder_id)`` -- each returning the field surface above.
    No write primitive is exposed.
    """
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    mcp = FastMCP(MCP_SERVER_NAME)

    def refuse(code: str, message: str) -> None:
        raise ToolError(json.dumps({"code": code, "message": message}, sort_keys=True))

    @mcp.tool()
    def list_line_states() -> dict[str, Any]:
        """List every line's current runtime state (read-only).

        Returns the same field surface as ``:7494 GET /v1/lines`` (folder_id /
        generation / round / phase / heartbeat_age_s / terminal / parked /
        wake_facts / release_id / run_id / wake_facts_stale), read through the
        exact same view function the :7494 read model serves -- never a second
        reader. An unreachable source is a machine-readable
        LINE_STATE_UNDECIDABLE refusal with evidence, never a fabricated empty
        list.
        """
        try:
            return read_line_states(config, view=view)
        except LineStateUndecidableError as exc:
            refuse(CODE_UNDECIDABLE, str(exc))
        return {}  # unreachable after refusal

    @mcp.tool()
    def get_line_state(folder_id: str) -> dict[str, Any]:
        """Fetch one line's current runtime state (read-only).

        Returns that line's field surface (same fields as list_line_states), or
        a machine-readable LINE_NOT_FOUND refusal when the folder is not a
        roster line. An unreachable source is a LINE_STATE_UNDECIDABLE refusal
        with evidence.
        """
        try:
            return fetch_line_state(config, folder_id, view=view)
        except LineStateUndecidableError as exc:
            refuse(CODE_UNDECIDABLE, str(exc))
        except LineStateNotFoundError as exc:
            refuse(CODE_LINE_NOT_FOUND, str(exc))
        return {}  # unreachable after refusal

    return mcp


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    run_root: str | None = None,
    lines_config: str | None = None,
) -> None:
    """Run the read-only line-state MCP surface on loopback.

    ``run_root`` / ``lines_config`` default to the same roots the :7494 read
    model serves (``/data/fleet-graph/runs`` + ``config/ronin-lines.json``), so
    production reads the identical data source. The R2 port discipline is a
    CI/acceptance-time assertion (the red-able ``tests/test_m1_line_state_mcp.py``
    check that :5615 never sits in ``config/line-state-mcp-reserved-ports.json``),
    deliberately not a runtime "probe the port at startup" behavior (spec item
    0 R2).
    """
    config = FleetStateConfig(
        host=host,
        port=port,
        run_root=Path(run_root) if run_root else DEFAULT_RUN_ROOT,
        lines_config=Path(lines_config) if lines_config else DEFAULT_LINES_CONFIG,
    )
    build_line_state_mcp_server(config).run(
        transport="streamable-http", host=host, port=port, path="/mcp"
    )


__all__ = [
    "CODE_LINE_NOT_FOUND",
    "CODE_UNDECIDABLE",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "LINE_STATE_FIELDS",
    "MCP_SERVER_NAME",
    "RESERVED_PORTS_FILE",
    "LineStateNotFoundError",
    "LineStateUndecidableError",
    "build_line_state_mcp_server",
    "fetch_line_state",
    "load_reserved_ports",
    "read_line_states",
    "serve",
]
