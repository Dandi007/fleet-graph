"""M1 line-state MCP: read-only line runtime state via the same :7494 view.

The spec's two-way criteria:

- **阳性**: a running line, the MCP tool's `generation / round / phase` equal
  the same-moment `:7494` answer field-for-field. Because the surface reads
  through the *same* view function / data source the `:7494` HTTP server serves
  (`FleetStateView(config).lines()`), the tests build one `FleetStateConfig`
  over a scratch run_root + roster and assert the MCP tool answer equals the
  view answer -- no second reader (spec 红线 1).
- **阴性**: the MCP tools expose **no write capability**. The tests scan
  `tools/list` and every tool's `inputSchema` for write primitives
  (set/update/clear/patch/deliver/wake/park); adding one turns the suite red.

Undecidable discipline (spec 红线 2/3): an unreachable source (missing /
unreadable roster) is an honest machine-readable `LINE_STATE_UNDECIDABLE`
refusal with evidence, never a fabricated empty-green; the test that drives an
unreachable source asserts the refusal, so it cannot silently go green.

Health-isolation (spec 红线 3): `build_line_state_mcp_server` is testable
without a transport layer; all reads run against a scratch run_root, never
production files. R2 port discipline: :5615 stays outside the committed
`config/line-state-mcp-reserved-ports.json` occupied list.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.line_state_mcp import (
    CODE_LINE_NOT_FOUND,
    CODE_UNDECIDABLE,
    DEFAULT_PORT,
    MCP_SERVER_NAME,
    RESERVED_PORTS_FILE,
    build_line_state_mcp_server,
    load_reserved_ports,
)
from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateView
from fleet_graph.state.run_artifacts import iso

REPO_ROOT = Path(__file__).resolve().parent.parent
LINE_STATE_UNIT = REPO_ROOT / "deploy" / "systemd" / "fleet-graph-line-state-mcp.service"

WRITE_TOKENS = ("set", "update", "clear", "patch", "deliver", "wake", "park")


class FakeClock:
    def __init__(self, start: float = 1_787_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def write_heartbeat(
    run_root: Path,
    folder_id: str,
    *,
    round_no: int,
    phase: str,
    updated_at: str,
    release_id: str | None = None,
    run_id: str | None = None,
) -> None:
    (run_root / folder_id).mkdir(parents=True, exist_ok=True)
    (run_root / folder_id / "heartbeat.json").write_text(
        json.dumps(
            {
                "run_id": run_id or f"run-{folder_id}",
                "folder_id": folder_id,
                "round": round_no,
                "phase": phase,
                "pid": 1234,
                "started_at": updated_at,
                "phase_started_at": updated_at,
                "updated_at": updated_at,
                "log_path": f"/data/fleet-graph/logs/{folder_id}.log",
                "release_id": release_id,
            }
        ),
        encoding="utf-8",
    )


def write_terminal(
    run_root: Path,
    folder_id: str,
    *,
    terminal: str,
    waiting_on: str | None = None,
    reason: str | None = None,
    run_id: str | None = None,
) -> None:
    (run_root / folder_id).mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "run_id": run_id or f"run-{folder_id}",
        "folder_id": folder_id,
        "terminal": terminal,
        "pump_fault": False,
        "rounds": 2,
        "reason": reason,
        "at": iso(1_787_000_000.0),
        "pid": 1234,
        "waiting_on": waiting_on,
        "waiting_on_declared": waiting_on,
        "log_path": f"/data/fleet-graph/logs/{folder_id}.log",
    }
    (run_root / folder_id / "terminal.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def write_roster(lines_config: Path, run_root: Path, folder_ids: list[str]) -> None:
    lines_config.write_text(
        json.dumps(
            {
                "run_root": str(run_root),
                "lines": [
                    {"folder_id": fid, "seat": "opencode-test", "generation": 2}
                    for fid in folder_ids
                ],
            }
        ),
        encoding="utf-8",
    )


def make_config(tmp_path: Path, clock: FakeClock | None = None) -> FleetStateConfig:
    return FleetStateConfig(
        host="127.0.0.1",
        port=0,
        run_root=tmp_path / "runs",
        dd_root=tmp_path / "dd",
        lines_config=tmp_path / "ronin-lines.json",
        bridge_state_dir=tmp_path / "bridge",
        clock=clock if clock is not None else FakeClock(),
    )


def _tool_text(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)


def _running_line_config(tmp_path: Path) -> FleetStateConfig:
    run_root = tmp_path / "runs"
    write_heartbeat(
        run_root,
        "wf-000001",
        round_no=3,
        phase="coordinator",
        updated_at=iso(1_787_000_000.0),
        release_id="20260902-030934-05dec3709ba0",
    )
    write_terminal(
        run_root,
        "wf-000001",
        terminal="blocked",
        waiting_on="decision",
        reason="need human",
    )
    write_heartbeat(
        run_root,
        "wf-000002",
        round_no=1,
        phase="worker",
        updated_at=iso(1_787_000_100.0),
    )
    write_terminal(run_root, "wf-000002", terminal="done", waiting_on="none")
    lines_config = tmp_path / "ronin-lines.json"
    write_roster(lines_config, run_root, ["wf-000001", "wf-000002"])
    return make_config(tmp_path)


class TestPositiveSameSource:
    """The MCP answer equals the same-moment :7494 view, field-for-field."""

    def test_list_line_states_matches_the_7494_view_field_by_field(self, tmp_path: Path) -> None:
        config = _running_line_config(tmp_path)
        reference = FleetStateView(config).lines()  # the :7494 view's answer
        server = build_line_state_mcp_server(config)
        result = asyncio.run(server.call_tool("list_line_states", {}))
        payload = _tool_text(result)
        assert payload["schema_version"] == reference["schema_version"]
        assert len(payload["lines"]) == len(reference["lines"])
        for line, ref in zip(payload["lines"], reference["lines"], strict=True):
            for field in (
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
            ):
                assert line[field] == ref[field], (field, line.get("folder_id"))

    def test_generation_round_phase_are_equal_field_by_field(self, tmp_path: Path) -> None:
        config = _running_line_config(tmp_path)
        reference = FleetStateView(config).lines()
        server = build_line_state_mcp_server(config)
        result = asyncio.run(server.call_tool("list_line_states", {}))
        lines = {line["folder_id"]: line for line in _tool_text(result)["lines"]}
        refs = {line["folder_id"]: line for line in reference["lines"]}
        # The running line's generation/round/phase are the literal positive
        # criterion from goal.md M1.
        assert lines["wf-000001"]["generation"] == refs["wf-000001"]["generation"] == 2
        assert lines["wf-000001"]["round"] == refs["wf-000001"]["round"] == 3
        assert lines["wf-000001"]["phase"] == refs["wf-000001"]["phase"] == "coordinator"

    def test_get_line_state_returns_the_same_line_as_the_view(self, tmp_path: Path) -> None:
        config = _running_line_config(tmp_path)
        reference = FleetStateView(config).lines()
        ref_by_id = {line["folder_id"]: line for line in reference["lines"]}
        server = build_line_state_mcp_server(config)
        result = asyncio.run(server.call_tool("get_line_state", {"folder_id": "wf-000001"}))
        assert _tool_text(result) == ref_by_id["wf-000001"]

    def test_get_line_state_unknown_line_is_a_machine_readable_refusal(
        self, tmp_path: Path
    ) -> None:
        from fastmcp.exceptions import ToolError

        config = _running_line_config(tmp_path)
        server = build_line_state_mcp_server(config)
        with pytest.raises(ToolError) as excinfo:
            asyncio.run(server.call_tool("get_line_state", {"folder_id": "wf-nope"}))
        payload = json.loads(str(excinfo.value))
        assert payload["code"] == CODE_LINE_NOT_FOUND
        assert "wf-nope" in payload["message"]


class TestNarrowSelfExplanatoryTools:
    """Tools are narrow and self-explanatory (golden-order 2): two tools, each
    one clear question -- never a "one call + one path param" wrapper."""

    def test_exactly_the_two_line_state_tools_are_registered(self) -> None:
        config = make_config(Path("unused"))
        server = build_line_state_mcp_server(config)
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        assert names == {"list_line_states", "get_line_state"}
        assert MCP_SERVER_NAME == "fleet-graph-line-state"

    def test_get_line_state_takes_only_folder_id(self) -> None:
        config = make_config(Path("unused"))
        server = build_line_state_mcp_server(config)
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        params = set(tools["get_line_state"].parameters["properties"])
        assert params == {"folder_id"}
        required = set(tools["get_line_state"].parameters.get("required") or params)
        assert required == {"folder_id"}

    def test_list_line_states_takes_no_arguments(self) -> None:
        config = make_config(Path("unused"))
        server = build_line_state_mcp_server(config)
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        assert not tools["list_line_states"].parameters.get("properties")


def assert_no_write_primitive(tools: list[Any]) -> None:
    """The regression core: no write token may appear in a tool name or in any
    tool's inputSchema property names."""
    offenders: list[str] = []
    for tool in tools:
        name = str(tool.name).lower()
        for token in WRITE_TOKENS:
            if token in name:
                offenders.append(f"tool name {tool.name!r} contains {token!r}")
        props = (tool.parameters.get("properties") or {}).keys()
        for prop in props:
            prop_lower = str(prop).lower()
            for token in WRITE_TOKENS:
                if token in prop_lower:
                    offenders.append(
                        f"tool {tool.name!r} inputSchema property {prop!r} contains {token!r}"
                    )
    assert not offenders, "write primitive exposed on a read-only surface: " + "; ".join(offenders)


class TestReadOnlyNegative:
    """Negative criterion: no write capability anywhere on the surface."""

    def test_tools_list_and_input_schemas_carry_no_write_primitive(self) -> None:
        config = make_config(Path("unused"))
        server = build_line_state_mcp_server(config)
        assert_no_write_primitive(asyncio.run(server.list_tools()))

    def test_mutation_adding_a_write_tool_turns_the_suite_red(self, tmp_path: Path) -> None:
        """Spec 变异判据: give the surface a write primitive -> the assertion
        above must fail red. Proof by grafting a write tool onto a copy."""
        from fastmcp import FastMCP

        server = build_line_state_mcp_server(make_config(tmp_path))
        base = list(asyncio.run(server.list_tools()))

        poison = FastMCP("poisoned")

        @poison.tool()
        def set_line_state(folder_id: str, phase: str) -> dict[str, Any]:
            return {"folder_id": folder_id, "phase": phase}

        poisoned = base + list(asyncio.run(poison.list_tools()))
        with pytest.raises(AssertionError, match="set"):
            assert_no_write_primitive(poisoned)

    def test_mutation_adding_a_write_input_schema_property_turns_the_suite_red(
        self, tmp_path: Path
    ) -> None:
        server = build_line_state_mcp_server(make_config(tmp_path))
        base = list(asyncio.run(server.list_tools()))

        class _WriteTool:
            name = "list_line_states"

            def __init__(self) -> None:
                self.parameters = {"properties": {"deliver_decision": {"type": "string"}}}

        with pytest.raises(AssertionError, match="deliver"):
            assert_no_write_primitive([_WriteTool(), *base])


class TestUndecidableIsRed:
    """Unreachable source: honest LINE_STATE_UNDECIDABLE refusal, never a
    fabricated empty-green; the test asserting the refusal cannot silently go
    green (spec 红线 2/3)."""

    def test_missing_roster_is_an_undecidable_refusal(self, tmp_path: Path) -> None:
        from fastmcp.exceptions import ToolError

        config = make_config(tmp_path)  # roster file never written
        server = build_line_state_mcp_server(config)
        with pytest.raises(ToolError) as excinfo:
            asyncio.run(server.call_tool("list_line_states", {}))
        payload = json.loads(str(excinfo.value))
        assert payload["code"] == CODE_UNDECIDABLE
        assert "unreachable" in payload["message"]
        assert "undecidable" in payload["message"]

    def test_missing_roster_refusal_carries_the_offending_path(self, tmp_path: Path) -> None:
        from fastmcp.exceptions import ToolError

        config = make_config(tmp_path)
        server = build_line_state_mcp_server(config)
        with pytest.raises(ToolError) as excinfo:
            asyncio.run(server.call_tool("get_line_state", {"folder_id": "wf-1"}))
        payload = json.loads(str(excinfo.value))
        assert payload["code"] == CODE_UNDECIDABLE
        assert str(config.lines_config) in payload["message"]

    def test_unreadable_roster_is_also_undecidable(self, tmp_path: Path) -> None:
        from fastmcp.exceptions import ToolError

        config = make_config(tmp_path)
        config.lines_config.write_text("{not json", encoding="utf-8")
        server = build_line_state_mcp_server(config)
        with pytest.raises(ToolError) as excinfo:
            asyncio.run(server.call_tool("list_line_states", {}))
        payload = json.loads(str(excinfo.value))
        assert payload["code"] == CODE_UNDECIDABLE


class TestHealthIsolation:
    """build_* is testable without a transport layer and never touches
    production files (spec 红线 3)."""

    def test_build_and_call_against_a_scratch_root_writes_nothing_outside_it(
        self, tmp_path: Path
    ) -> None:
        config = _running_line_config(tmp_path)
        reference = FleetStateView(config).lines()
        before = {p for p in tmp_path.rglob("*")}
        server = build_line_state_mcp_server(config)
        listed = _tool_text(asyncio.run(server.call_tool("list_line_states", {})))
        asyncio.run(server.call_tool("get_line_state", {"folder_id": "wf-000001"}))
        after = {p for p in tmp_path.rglob("*")}
        # The surface only ever reads the scratch view; the answer is exactly
        # the scratch data and the call created no new files anywhere.
        assert listed == reference
        assert after == before


class TestPortR2:
    """R2: the committed reserved-ports list is the single source, and the
    surface's default port must never sit in it (mechanical red/green)."""

    def test_the_reserved_ports_file_is_committed_and_readable(self) -> None:
        assert RESERVED_PORTS_FILE.exists(), RESERVED_PORTS_FILE
        raw = json.loads(RESERVED_PORTS_FILE.read_text(encoding="utf-8"))
        assert isinstance(raw.get("reserved_ports"), list)
        assert len(raw["reserved_ports"]) > 0

    def test_the_module_loads_the_same_committed_list(self) -> None:
        raw = json.loads(RESERVED_PORTS_FILE.read_text(encoding="utf-8"))
        assert load_reserved_ports() == frozenset(int(p) for p in raw["reserved_ports"])

    def test_the_default_port_is_not_in_the_reserved_list(self) -> None:
        assert DEFAULT_PORT == 5615
        assert DEFAULT_PORT not in load_reserved_ports()

    def test_the_rejected_5614_is_in_the_reserved_list(self) -> None:
        """5614 is now occupied (decision MCP); the red-able assertion above
        must turn red if the default ever drifts back to it."""
        assert 5614 in load_reserved_ports()

    def test_the_systemd_unit_serves_the_unreserved_default_port(self) -> None:
        text = LINE_STATE_UNIT.read_text(encoding="utf-8")
        assert "--port" in text
        port_line = next(
            line for line in text.replace("\\\n", " ").splitlines() if "--port" in line
        )
        assert f"--port {DEFAULT_PORT}" in port_line, port_line
        assert DEFAULT_PORT not in load_reserved_ports()

    def test_cli_default_matches_the_module_port(self) -> None:
        from fleet_graph.cli import build_parser

        args = build_parser().parse_args(["line-state", "serve", "--lines-config", "x.json"])
        assert args.port == DEFAULT_PORT == 5615
        assert args.port not in load_reserved_ports()


class TestCli:
    def test_line_state_serve_parses(self) -> None:
        from fleet_graph.cli import build_parser

        args = build_parser().parse_args(["line-state", "serve", "--lines-config", "x.json"])
        assert args.port == 5615

    def test_line_state_serve_routes_to_the_mcp_serve(self) -> None:
        import inspect

        from fleet_graph.cli import _line_state_serve

        assert "from fleet_graph.line_state_mcp import serve" in inspect.getsource(
            _line_state_serve
        )
