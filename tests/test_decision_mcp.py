"""The decision MCP surface: synchronous, conclusive decision delivery.

The spec (2026-09-02 用户拍板) moves decision hand-off to a synchronous MCP
interface: one call either proves the verdict was delivered *and consumed* by
the parked owner, or returns an explicit, machine-readable refusal -- never a
silent HTTP-200 swallow. Four failure modes each get one negative test, and the
positive case is the parked + waiting_on=decision line that APPROVE actually
wakes.

These drive the *real* ``LineOwnerSource`` against a scratch run root (the same
stall-state file the scheduler writes and the decision bridge reads), so the
resume side is not a mock: a positive delivery clears the parked snapshot just
as a live wake would.

R2 (spec item 0, 2026-09-02 successor): the surface serves :5614, and the
committed ``config/decision-mcp-reserved-ports.json`` is the single source of
the occupied/reserved loopback ports. The port-assertion tests here are the
mechanical red/green criterion: with 5614 the suite is green, and reverting the
default to 5613 (which *is* in the reserved list) fails the suite. This is a
CI/acceptance-time assertion, deliberately not a runtime port probe.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.decision_bridge.owners import OWNER_KIND_LINE
from fleet_graph.decision_mcp import (
    ALLOWED_DECISIONS,
    CODE_LINE_NOT_PARKED,
    CODE_NO_WAITING_PARTY,
    CODE_QUESTION_CARD_UNRESOLVED,
    DECISION_APPROVE,
    DECISION_REJECT,
    DEFAULT_PORT,
    MCP_SERVER_NAME,
    OUTCOME_DELIVERED,
    OUTCOME_REFUSED,
    DecisionPayloadError,
    DeliveryLedger,
    DeliveryResult,
    build_decision_mcp_server,
    deliver_decision,
    load_reserved_ports,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESERVED_PORTS_FILE = REPO_ROOT / "config" / "decision-mcp-reserved-ports.json"
DECISION_MCP_UNIT = REPO_ROOT / "deploy" / "systemd" / "fleet-graph-decision-mcp.service"


def _stall(run_root: Path, folder_id: str, *, parked: bool = True) -> Path:
    path = run_root / ".scheduler" / f"{folder_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {
        "generation": 2,
        "board_question_note_id": "q-1",
        "board_card_entity_id": "card-1",
    }
    if parked:
        state["parked_run_id"] = "run-1"
        state["parked_at"] = 1_700_000_000.0
        state["parked_goal_revision"] = "sha256:consumed"
        state["parked_inbox_available"] = True
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def _parked_but_unresolved(run_root: Path, folder_id: str) -> Path:
    path = run_root / ".scheduler" / f"{folder_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generation": 2,
                "board_question_note_id": "",
                "board_card_entity_id": "",
                "parked_run_id": "run-1",
                "parked_at": 1_700_000_000.0,
            }
        ),
        encoding="utf-8",
    )
    return path


ROSTER = [{"folder_id": "wf-1", "seat": "s", "generation": 2}]


def _call(
    run_root: Path,
    line: str = "wf-1",
    decision: str = DECISION_APPROVE,
    reason: str = "live drill",
    roster: list[Any] | None = None,
) -> DeliveryResult:
    return deliver_decision(
        line=line,
        decision=decision,
        reason=reason,
        run_root=run_root,
        lines=roster if roster is not None else ROSTER,
    )


class TestPositiveDelivery:
    def test_parked_decision_line_approve_is_delivered_and_consumed(self, tmp_path: Path) -> None:
        stall = _stall(tmp_path, "wf-1")
        result = _call(tmp_path, decision=DECISION_APPROVE)
        assert result.status == OUTCOME_DELIVERED
        assert result.as_dict()["outcome"] == "consumed"
        # the line was woken through the registered control entry
        assert result.target is not None
        assert result.target["kind"] == OWNER_KIND_LINE
        assert result.target["id"] == "wf-1"
        assert result.target["question_note_id"] == "q-1"
        assert result.target["card_entity_id"] == "card-1"
        # parking is lifted: the stall-state snapshot is cleared
        after = json.loads(stall.read_text(encoding="utf-8"))
        assert after["parked_run_id"] is None
        assert after["parked_at"] is None
        assert after["parked_goal_revision"] is None

    def test_reject_is_also_a_valid_delivered_verdict(self, tmp_path: Path) -> None:
        _stall(tmp_path, "wf-1")
        result = _call(tmp_path, decision=DECISION_REJECT, reason="do not merge")
        assert result.status == OUTCOME_DELIVERED
        assert result.decision == DECISION_REJECT
        assert result.action_key == "mcp:wf-1:g2:REJECT"


class TestFailureMode1LineNotParked:
    def test_line_not_parked_is_an_explicit_refusal(self, tmp_path: Path) -> None:
        _stall(tmp_path, "wf-1", parked=False)
        result = _call(tmp_path)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_LINE_NOT_PARKED
        assert result.retryable is True
        assert "not parked" in result.message

    def test_no_stall_file_is_the_same_explicit_refusal(self, tmp_path: Path) -> None:
        result = _call(tmp_path)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_LINE_NOT_PARKED
        assert result.retryable is True


class TestFailureMode2QuestionCardResolution:
    def test_unresolvable_question_card_is_a_sync_refusal(self, tmp_path: Path) -> None:
        _parked_but_unresolved(tmp_path, "wf-1")
        result = _call(tmp_path)
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_QUESTION_CARD_UNRESOLVED
        assert "server-side resolution failed" in result.message


class TestFailureMode3InvalidPayload:
    def test_decision_outside_the_closed_set_is_a_call_point_error(self, tmp_path: Path) -> None:
        with pytest.raises(DecisionPayloadError, match="APPROVE or REJECT"):
            _call(tmp_path, decision="MAYBE")

    def test_missing_decision_is_a_call_point_error(self, tmp_path: Path) -> None:
        with pytest.raises(DecisionPayloadError, match="decision is required"):
            _call(tmp_path, decision="")

    def test_missing_line_is_a_call_point_error(self, tmp_path: Path) -> None:
        with pytest.raises(DecisionPayloadError, match="line is required"):
            _call(tmp_path, line="  ")

    def test_missing_reason_is_a_call_point_error(self, tmp_path: Path) -> None:
        with pytest.raises(DecisionPayloadError, match="reason is required"):
            _call(tmp_path, reason="  ")

    def test_the_allowed_set_is_exactly_the_two_verdicts(self) -> None:
        assert {DECISION_APPROVE, DECISION_REJECT} == ALLOWED_DECISIONS


class TestFailureMode4NoWaitingParty:
    def test_an_unregistered_line_is_no_such_waiting_party(self, tmp_path: Path) -> None:
        result = _call(tmp_path, line="wf-nope")
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NO_WAITING_PARTY
        assert "no such waiting party" in result.message

    def test_an_empty_roster_refuses_everything_as_no_waiting_party(self, tmp_path: Path) -> None:
        result = _call(tmp_path, roster=[])
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NO_WAITING_PARTY


class TestLedgerObservability:
    def test_every_call_is_recorded_in_the_ledger(self, tmp_path: Path) -> None:
        _stall(tmp_path, "wf-1")
        ledger = DeliveryLedger(state_dir=tmp_path / "state")
        result = _call(tmp_path)
        ledger.record(result)
        refused = _call(tmp_path, line="wf-nope")
        ledger.record(refused)
        entries = ledger.entries()
        assert len(entries) == 2
        assert entries[0]["status"] == OUTCOME_DELIVERED
        assert entries[1]["status"] == OUTCOME_REFUSED
        assert entries[1]["code"] == CODE_NO_WAITING_PARTY

    def test_metrics_textfile_counts_delivered_and_refused(self, tmp_path: Path) -> None:
        from fleet_graph.cost_obs.exposition import parse

        _stall(tmp_path, "wf-1")
        ledger = DeliveryLedger(state_dir=tmp_path / "state")
        ledger.record(_call(tmp_path))
        _stall(tmp_path, "wf-1")
        ledger.record(_call(tmp_path, decision=DECISION_REJECT))
        refused = _call(tmp_path, line="wf-nope")
        ledger.record(refused)
        samples = parse(ledger.metrics_path.read_text(encoding="utf-8"))
        by_name = {}
        for sample in samples:
            by_name.setdefault(sample.name, []).append(sample)
        assert [s.value for s in by_name["fleet_graph_decision_delivered_total"]] == [2]
        refused_samples = by_name["fleet_graph_decision_refused_total"]
        assert any(
            s.label_map().get("code") == CODE_NO_WAITING_PARTY and s.value == 1
            for s in refused_samples
        )


class TestMcpSurface:
    def test_the_tool_is_registered_on_the_decision_surface(self) -> None:
        server = build_decision_mcp_server(Path("/tmp"), ROSTER)
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        assert MCP_SERVER_NAME == "fleet-graph-decision"
        assert "decision_deliver" in names

    def test_decision_deliver_is_not_on_dd_or_goal_faces(self) -> None:
        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import FakeControlPlane

        dd_server = build_mcp_server(FakeControlPlane())
        dd_tools = {tool.name for tool in asyncio.run(dd_server.list_tools())}
        assert "decision_deliver" not in dd_tools

    def test_the_tool_lists_its_required_arguments(self) -> None:
        server = build_decision_mcp_server(Path("/tmp"), ROSTER)
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        params = set(tools["decision_deliver"].parameters["properties"])
        assert {"line", "decision", "reason"} <= params
        required = set(tools["decision_deliver"].parameters.get("required") or params)
        assert {"line", "decision", "reason"} <= required

    def test_invalid_payload_reaches_the_client_machine_readably(self, tmp_path: Path) -> None:
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from test_dd_service import running_server

        server = build_decision_mcp_server(tmp_path, ROSTER)

        async def call(url: str) -> str:
            async with Client(url) as client:
                with pytest.raises(ToolError) as excinfo:
                    await client.call_tool(
                        "decision_deliver",
                        {"line": "wf-1", "decision": "MAYBE", "reason": "x"},
                    )
                return str(excinfo.value)

        with running_server(server) as url:
            message = asyncio.run(call(url))
        payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
        assert payload["code"] == "DECISION_DELIVER_REFUSED"
        assert "invalid payload" in payload["message"]

    def test_delivered_result_reaches_the_client(self, tmp_path: Path) -> None:
        from fastmcp import Client

        from test_dd_service import running_server

        _stall(tmp_path, "wf-1")
        server = build_decision_mcp_server(tmp_path, ROSTER)

        async def call(url: str) -> dict[str, Any]:
            async with Client(url) as client:
                result = await client.call_tool(
                    "decision_deliver",
                    {"line": "wf-1", "decision": "APPROVE", "reason": "live"},
                )
                return json.loads(result.content[0].text)

        with running_server(server) as url:
            payload = asyncio.run(call(url))
        assert payload["status"] == OUTCOME_DELIVERED
        assert payload["outcome"] == "consumed"
        assert payload["line"] == "wf-1"
        assert payload["decision"] == "APPROVE"


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
        assert DEFAULT_PORT == 5614
        assert DEFAULT_PORT not in load_reserved_ports()

    def test_the_rejected_5613_is_in_the_reserved_list(self) -> None:
        """The previously chosen 5613 is now occupied (5602-5613 continuous);
        the red-able assertion above must turn red if the default ever drifts
        back to 5613."""
        assert 5613 in load_reserved_ports()

    def test_the_systemd_unit_serves_the_unreserved_default_port(self) -> None:
        text = DECISION_MCP_UNIT.read_text(encoding="utf-8")
        assert "--port" in text
        port_line = next(
            line for line in text.replace("\\\n", " ").splitlines() if "--port" in line
        )
        assert f"--port {DEFAULT_PORT}" in port_line, port_line
        assert DEFAULT_PORT not in load_reserved_ports()

    def test_cli_default_matches_the_module_port(self) -> None:
        from fleet_graph.cli import build_parser

        args = build_parser().parse_args(["decision", "serve", "--lines-config", "x.json"])
        assert args.port == DEFAULT_PORT == 5614
        assert args.port not in load_reserved_ports()


class TestCli:
    def test_decision_serve_parses(self) -> None:
        from fleet_graph.cli import build_parser

        args = build_parser().parse_args(["decision", "serve", "--lines-config", "x.json"])
        assert args.port == 5614

    def test_decision_serve_routes_to_the_mcp_serve(self) -> None:
        import inspect

        from fleet_graph.cli import _decision_serve

        assert "from fleet_graph.decision_mcp import serve" in inspect.getsource(_decision_serve)
