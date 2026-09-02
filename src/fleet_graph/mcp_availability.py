"""M4 —— the MCP-surface "functional availability" oracle: the layer above ``up == 0``.

Sixty-eight of the 118 alert rules key off ``up == 0``; none asks "can this
service still do its job". The ronin-mcp incident (38/59 tools broken while
everything stayed green) is the evidence for wf-525fd4 goal.md M4: reachability
is not functionality. This module is *only* the determination interface for
"how do we judge an MCP surface is functionally available"; writing the alert
rules belongs to wf-6475fd (no second copy of the rules here).

The criterion is not negotiable (verbatim from goal.md M4):

    functional availability == ``tools/list`` succeeds **and** at least one
    read-only tool's *real* call succeeds.

An explicitly ``NOT_SUPPORTED`` legacy tool's refusal is correct behaviour, not
a failure: it is neither a success nor an error, so it never moves the verdict
and never counts toward "broken".

Red lines the module honours:

- **Pure determination.** It returns a structured
  available/unavailable/indeterminate verdict plus evidence, never a bare
  ``bool``, and it writes no alert rules, no ledger, no files.
- **Unreachable is not silently asserted.** A ``tools/list`` that cannot be
  retrieved is marked explicitly; so is the case where the surface is reachable
  but no read-only tool can be proven to work (for example, every candidate is
  ``NOT_SUPPORTED``).
- **Transport-free tests.** The judgment runs over an injected surface seam
  (``McpSurface``); tests never touch a production ledger or production file.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

#: Verdict vocabulary of the oracle. ``available`` and ``unavailable`` are the
#: two states an alert can key off; ``indeterminate`` is the explicit "we could
#: not determine this" mark that must never be silently asserted either way.
STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
STATUS_INDETERMINATE = "indeterminate"

#: The exact refusal token the dd surface (and, by contract, any fleet MCP
#: surface that keeps legacy tools reachable) emits for a historical tool it
#: deliberately does not implement. A refusal carrying this token is correct
#: behaviour and must not be counted as a failure.
NOT_SUPPORTED_CODE = "NOT_SUPPORTED"

#: Per-probe outcome vocabulary.
PROBE_SUCCESS = "success"
PROBE_NOT_SUPPORTED = "not_supported"
PROBE_ERROR = "error"


class McpSurface(Protocol):
    """The seam a caller (or a test) implements for one MCP surface.

    ``list_tools`` returns the surface's registered tool names and raises on any
    transport/protocol failure (the unreachable case). ``call_tool`` runs one
    tool: it either returns on success or raises; a ``NOT_SUPPORTED`` refusal is
    surfaced as a raised error whose description carries the token
    ``NOT_SUPPORTED`` (exactly the shape ``fleet_graph.dd.service`` emits for its
    legacy-only tools). Nothing else is required of a surface -- in particular no
    framing of ``tools/list`` results, no alarm vocabulary.
    """

    def list_tools(self) -> list[str]: ...

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any: ...


@dataclass
class ProbeResult:
    """One read-only probe's outcome and evidence."""

    tool: str
    outcome: str  # success | not_supported | error
    detail: str = ""


@dataclass
class AvailabilityVerdict:
    """The structured conclusion: a status plus the evidence behind it."""

    status: str
    tools_listed: list[str] = field(default_factory=list)
    probes: list[ProbeResult] = field(default_factory=list)
    list_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The machine-readable form an alert layer (wf-6475fd) reads."""
        return {
            "status": self.status,
            "tools_listed": list(self.tools_listed),
            "probes": [
                {"tool": probe.tool, "outcome": probe.outcome, "detail": probe.detail}
                for probe in self.probes
            ],
            "list_error": self.list_error,
        }


def is_not_supported_refusal(exc: BaseException) -> bool:
    """Recognise an explicit ``NOT_SUPPORTED`` refusal from a raised error.

    The dd surface raises ``fastmcp.exceptions.ToolError`` whose payload is the
    JSON object ``{"code": "NOT_SUPPORTED", ...}``; matching on the token keeps
    this module transport-agnostic (it never imports fastmcp) while still
    recognising the real refusal on the wire.
    """
    return NOT_SUPPORTED_CODE in str(exc)


def _describe(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def judge_mcp_availability(
    surface: McpSurface,
    read_only_tools: Sequence[str],
    *,
    arguments: dict[str, Any] | None = None,
) -> AvailabilityVerdict:
    """Judge one MCP surface's functional availability.

    ``surface`` is the seam (``McpSurface``); ``read_only_tools`` is the explicit
    set of read-only tools to probe (>=1 name required for a determinate
    verdict). ``arguments`` (default empty) is the argument object passed to every
    read-only probe -- read-only tools are expected to be callable with no
    required arguments (for example ``development_list``).

    The verdict resolves as follows, in the spec's closed vocabulary:

    - ``available``      -- ``tools/list`` succeeded and at least one read-only
      probe returned (no exception).
    - ``unavailable``    -- ``tools/list`` raised (unreachable / broken), or it
      succeeded but at least one read-only probe raised a *real* error (the
      38/59-broken case).
    - ``indeterminate``  -- the surface was reached but no read-only probe
      succeeded and none errored: either none were configured, or every probe was
      an explicit ``NOT_SUPPORTED`` refusal. Reached but unprovable, so it is
      marked rather than silently asserted available or unavailable.
    """
    probe_names = list(read_only_tools)
    probe_args = {} if arguments is None else dict(arguments)

    try:
        tools_listed = list(surface.list_tools())
    except Exception as exc:  # transport / protocol failure: unreachable
        return AvailabilityVerdict(status=STATUS_UNAVAILABLE, list_error=_describe(exc))

    if not probe_names:
        return AvailabilityVerdict(
            status=STATUS_INDETERMINATE,
            tools_listed=tools_listed,
            list_error="no read-only tools configured: functional availability is unprovable",
        )

    probes: list[ProbeResult] = []
    for tool in probe_names:
        try:
            surface.call_tool(tool, dict(probe_args))
        except Exception as exc:  # classify the refusal below; that is the point
            if is_not_supported_refusal(exc):
                probes.append(ProbeResult(tool, PROBE_NOT_SUPPORTED, _describe(exc)))
            else:
                probes.append(ProbeResult(tool, PROBE_ERROR, _describe(exc)))
        else:
            probes.append(ProbeResult(tool, PROBE_SUCCESS, ""))

    success = any(probe.outcome == PROBE_SUCCESS for probe in probes)
    error = any(probe.outcome == PROBE_ERROR for probe in probes)

    if success:
        status = STATUS_AVAILABLE
    elif error:
        status = STATUS_UNAVAILABLE
    else:
        status = STATUS_INDETERMINATE

    return AvailabilityVerdict(status=status, tools_listed=tools_listed, probes=probes)


class FastMcpSurface:
    """A real MCP surface over a fastmcp streamable-http endpoint.

    Adapts a live `fastmcp.Client` to the ``McpSurface`` protocol so the oracle
    can be pointed at a running surface. Each call opens a short-lived client
    session -- the same pattern `state/work_folder.py` uses for the katana
    work-folder MCP -- so the oracle never holds a session open and never writes
    a file. ``fastmcp`` and ``httpx`` are imported lazily so importing this
    module stays free of transport dependencies.
    """

    def __init__(self, url: str, *, timeout: float | None = None) -> None:
        self.url = url
        self.timeout = timeout

    def list_tools(self) -> list[str]:
        return asyncio.run(self._list_tools())

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        return asyncio.run(self._call_tool(tool, arguments))

    async def _list_tools(self) -> list[str]:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        client = Client(
            StreamableHttpTransport(self.url, httpx_client_factory=self._client_factory)
        )
        async with client:
            tools = await client.list_tools()
        return [tool.name for tool in tools]

    async def _call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        client = Client(
            StreamableHttpTransport(self.url, httpx_client_factory=self._client_factory)
        )
        async with client:
            return await client.call_tool(tool, arguments)

    def _client_factory(self, **kwargs: Any) -> Any:
        import httpx

        kwargs["trust_env"] = False  # never proxy a loopback MCP surface
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        return httpx.AsyncClient(**kwargs)


__all__ = [
    "NOT_SUPPORTED_CODE",
    "PROBE_ERROR",
    "PROBE_NOT_SUPPORTED",
    "PROBE_SUCCESS",
    "STATUS_AVAILABLE",
    "STATUS_INDETERMINATE",
    "STATUS_UNAVAILABLE",
    "AvailabilityVerdict",
    "FastMcpSurface",
    "McpSurface",
    "ProbeResult",
    "is_not_supported_refusal",
    "judge_mcp_availability",
]
