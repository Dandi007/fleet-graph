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

from fleet_graph.decision_bridge.owners import (
    OWNER_KIND_DD,
    OWNER_KIND_LINE,
    RESUME_REFUSED,
    RESUME_RESUMED,
    OwnerResult,
    OwnerTarget,
)
from fleet_graph.decision_mcp import (
    ALLOWED_DECISIONS,
    CODE_DD_NOT_AWAITING_GATE,
    CODE_DD_NOT_FOUND,
    CODE_LINE_NOT_PARKED,
    CODE_NO_WAITING_PARTY,
    CODE_OWNER_REFUSED,
    CODE_QUESTION_CARD_UNRESOLVED,
    DECISION_APPROVE,
    DECISION_REJECT,
    DEFAULT_PORT,
    DEFAULT_STATE_DIR,
    MCP_SERVER_NAME,
    OUTCOME_DELIVERED,
    OUTCOME_REFUSED,
    TARGET_KIND_DD,
    TARGET_KIND_LINE,
    DecisionPayloadError,
    DeliveryLedger,
    DeliveryResult,
    build_decision_mcp_server,
    deliver_decision,
    deliver_decision_dd,
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


class FakePlane:
    """A dd control-plane stub: ``get`` answers known/awaiting vs unknown."""

    def __init__(self, source: FakeDdSource) -> None:
        self._source = source

    def get(self, development_id: str) -> dict[str, Any]:
        from fleet_graph.dd.control_plane import ControlPlaneError

        if development_id in self._source._awaiting:
            return {"development_id": development_id, "state": "awaiting_gate"}
        if development_id in self._source._known:
            return {"development_id": development_id, "state": "running"}
        raise ControlPlaneError(
            "DEVELOPMENT_NOT_FOUND", f"no admission record for {development_id}"
        )


class FakeDdSource:
    """An in-memory ``DdOwnerSource`` stand-in for the decision MCP dd path.

    ``awaiting`` maps development id -> OwnerTarget (a waiting dd gate);
    ``known`` names developments the control plane has on record but whose
    state is *not* awaiting_gate; anything else is unknown. ``resume`` records
    its calls and answers ``resumed``.
    """

    def __init__(
        self,
        awaiting: dict[str, OwnerTarget] | None = None,
        known: set[str] | None = None,
    ) -> None:
        self._awaiting = dict(awaiting or {})
        self._known = set(known or {})
        self.resumes: list[tuple[OwnerTarget, str]] = []

    def _control_plane(self) -> FakePlane:
        return FakePlane(self)

    def discover_all(self) -> list[OwnerTarget]:
        return list(self._awaiting.values())

    def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
        self.resumes.append((target, action_key))
        return OwnerResult(RESUME_RESUMED, "resumed")


def _dd_target(dev: str = "dev-abc", *, generation: int = 1) -> OwnerTarget:
    return OwnerTarget(
        kind=OWNER_KIND_DD,
        id=dev,
        generation=generation,
        question_note_id="q-1",
        card_entity_id="card-1",
        state="awaiting_gate",
    )


def _call(
    run_root: Path,
    line: str = "wf-1",
    decision: str = DECISION_APPROVE,
    reason: str = "live drill",
    roster: list[Any] | None = None,
    target_kind: str = TARGET_KIND_LINE,
    target_id: str = "",
    dd_source: FakeDdSource | None = None,
) -> DeliveryResult:
    return deliver_decision(
        line=line,
        decision=decision,
        reason=reason,
        run_root=run_root,
        lines=roster if roster is not None else ROSTER,
        target_kind=target_kind,
        target_id=target_id,
        dd_source=dd_source,
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
        schema = tools["decision_deliver"].parameters
        params = set(schema["properties"])
        # The target is explicit in the schema: a distinct target_kind plus a
        # distinct target_id, so a line never shares one string with a dd id.
        assert {"line", "decision", "reason", "target_kind", "target_id"} <= params
        required = set(schema.get("required") or params)
        assert {"decision", "reason"} <= required
        # The three-arg line path stays backward-compatible: line/target_kind/
        # target_id are optional, and target_kind defaults to the line path.
        assert "line" not in required
        assert "target_kind" not in required
        assert "target_id" not in required
        assert schema["properties"]["target_kind"]["default"] == TARGET_KIND_LINE

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

    def test_built_without_a_ledger_never_touches_the_production_state_dir(
        self, tmp_path: Path
    ) -> None:
        """Health-isolation guard (2026-09-02 spec): a server built without an
        explicit ledger must never write to ``DEFAULT_STATE_DIR``.

        ``build_decision_mcp_server`` used to fall back to
        ``DeliveryLedger()`` (whose default is the production
        ``DEFAULT_STATE_DIR``), so a plain test server appending to the real
        ledger was the production-pollution bug this spec fixes. Only
        ``serve()``, which always injects an explicit ledger, may write
        production files.
        """
        from fastmcp import Client

        from test_dd_service import running_server

        production_ledger = DEFAULT_STATE_DIR / "deliveries.jsonl"
        before = production_ledger.read_bytes() if production_ledger.exists() else None
        _stall(tmp_path, "wf-1")
        server = build_decision_mcp_server(tmp_path, ROSTER)

        async def call(url: str) -> dict[str, Any]:
            async with Client(url) as client:
                result = await client.call_tool(
                    "decision_deliver",
                    {"line": "wf-1", "decision": "APPROVE", "reason": "guard"},
                )
                return json.loads(result.content[0].text)

        with running_server(server) as url:
            payload = asyncio.run(call(url))
        assert payload["status"] == OUTCOME_DELIVERED
        after = production_ledger.read_bytes() if production_ledger.exists() else None
        assert after == before, (
            f"server built without an explicit ledger wrote to the production "
            f"state dir {DEFAULT_STATE_DIR}"
        )

    def test_an_injected_ledger_records_calls_from_the_tool(self, tmp_path: Path) -> None:
        """A server with an explicit scratch ledger records every call into
        that ledger (never anywhere else)."""
        from fastmcp import Client

        from test_dd_service import running_server

        _stall(tmp_path, "wf-1")
        ledger = DeliveryLedger(state_dir=tmp_path / "state")
        server = build_decision_mcp_server(tmp_path, ROSTER, ledger=ledger)

        async def call(url: str) -> dict[str, Any]:
            async with Client(url) as client:
                result = await client.call_tool(
                    "decision_deliver",
                    {"line": "wf-1", "decision": "APPROVE", "reason": "recorded"},
                )
                return json.loads(result.content[0].text)

        with running_server(server) as url:
            payload = asyncio.run(call(url))
        assert payload["status"] == OUTCOME_DELIVERED
        assert payload["action_key"] == "mcp:wf-1:g2:APPROVE"
        entries = ledger.entries()
        assert len(entries) == 1
        assert entries[0]["status"] == OUTCOME_DELIVERED
        assert entries[0]["line"] == "wf-1"
        assert (tmp_path / "state" / "deliveries.jsonl").exists()


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


class TestDdGateDelivery:
    """M2(a): the decision surface delivers to a dd gate, not just a line.

    A dd target is resolved server-side from the dd control plane's
    ``awaiting_gate`` record and resumed through ``DdControlPlane.gate(resume=True)``,
    exercised here over an isolated dd owner. An unknown dd, a non-awaiting dd,
    and a dd id mis-delivered down the line path are each an explicit refusal --
    never a silent swallow and never the "read the line stall state" bypass that
    would surface a dd id as ``LINE_NOT_PARKED``.
    """

    def test_awaiting_dd_gate_is_delivered_and_consumed(self, tmp_path: Path) -> None:
        dd_source = FakeDdSource(awaiting={"dev-abc": _dd_target()})
        result = _call(
            tmp_path,
            target_kind=TARGET_KIND_DD,
            target_id="dev-abc",
            dd_source=dd_source,
        )
        assert result.status == OUTCOME_DELIVERED
        assert result.as_dict()["outcome"] == "consumed"
        assert result.target is not None
        assert result.target["kind"] == OWNER_KIND_DD
        assert result.target["id"] == "dev-abc"
        assert result.target["question_note_id"] == "q-1"
        assert result.target["card_entity_id"] == "card-1"
        assert result.target["resume_status"] == RESUME_RESUMED
        assert dd_source.resumes == [(_dd_target(), "mcp:dd:dev-abc:g1:APPROVE")]

    def test_reject_is_also_a_valid_dd_verdict(self, tmp_path: Path) -> None:
        dd_source = FakeDdSource(awaiting={"dev-abc": _dd_target()})
        result = _call(
            tmp_path,
            decision=DECISION_REJECT,
            reason="do not merge",
            target_kind=TARGET_KIND_DD,
            target_id="dev-abc",
            dd_source=dd_source,
        )
        assert result.status == OUTCOME_DELIVERED
        assert result.decision == DECISION_REJECT
        assert result.action_key == "mcp:dd:dev-abc:g1:REJECT"

    def test_unknown_dd_is_an_explicit_refusal(self, tmp_path: Path) -> None:
        result = _call(
            tmp_path,
            target_kind=TARGET_KIND_DD,
            target_id="dev-nope",
            dd_source=FakeDdSource(),
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_NOT_FOUND

    def test_a_dd_not_awaiting_the_gate_is_an_explicit_refusal(self, tmp_path: Path) -> None:
        result = _call(
            tmp_path,
            target_kind=TARGET_KIND_DD,
            target_id="dev-busy",
            dd_source=FakeDdSource(known={"dev-busy"}),
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_DD_NOT_AWAITING_GATE

    def test_a_dd_id_delivered_down_the_line_path_is_refused_not_treated_as_line(
        self, tmp_path: Path
    ) -> None:
        """Regression guard for the swallow: a dd development id routed down the
        *line* path is an explicit refusal (the line roster does not know it),
        never a read of the line stall-state that would answer LINE_NOT_PARKED."""
        result = _call(tmp_path, line="dev-abc")
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NO_WAITING_PARTY
        assert result.code != CODE_LINE_NOT_PARKED

    def test_missing_target_id_for_a_dd_target_is_a_call_point_error(self) -> None:
        with pytest.raises(DecisionPayloadError, match="target_id is required"):
            deliver_decision_dd(
                target_id="  ",
                decision=DECISION_APPROVE,
                reason="x",
                dd_source=FakeDdSource(),
            )

    def test_an_unknown_target_kind_is_a_call_point_error(self, tmp_path: Path) -> None:
        with pytest.raises(DecisionPayloadError, match="target_kind must be"):
            _call(tmp_path, target_kind="folder", target_id="dev-abc")

    def test_owner_side_refusal_surfaces_as_owner_refused(self, tmp_path: Path) -> None:
        class RefusingDdSource(FakeDdSource):
            def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
                return OwnerResult(RESUME_REFUSED, "gate refused")

        result = _call(
            tmp_path,
            target_kind=TARGET_KIND_DD,
            target_id="dev-abc",
            dd_source=RefusingDdSource(awaiting={"dev-abc": _dd_target()}),
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_OWNER_REFUSED

    def test_dd_delivery_reaches_the_client(self, tmp_path: Path) -> None:
        """The tool wiring carries target_kind/target_id to the dd path."""
        from fastmcp import Client

        from test_dd_service import running_server

        dd_source = FakeDdSource(awaiting={"dev-abc": _dd_target()})
        server = build_decision_mcp_server(tmp_path, ROSTER, dd_source=dd_source)

        async def call(url: str) -> dict[str, Any]:
            async with Client(url) as client:
                result = await client.call_tool(
                    "decision_deliver",
                    {
                        "decision": "APPROVE",
                        "reason": "live",
                        "target_kind": "dd",
                        "target_id": "dev-abc",
                    },
                )
                return json.loads(result.content[0].text)

        with running_server(server) as url:
            payload = asyncio.run(call(url))
        assert payload["status"] == OUTCOME_DELIVERED
        assert payload["outcome"] == "consumed"
        assert payload["target"]["kind"] == OWNER_KIND_DD
        assert payload["target"]["id"] == "dev-abc"
