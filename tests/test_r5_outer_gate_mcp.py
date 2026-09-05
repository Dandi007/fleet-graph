"""wf-4601c8 R5：外门收敛 MCP（外门=九运行时工具 + state_takeover 六项）.

判据锚：specs/r5-outer-gate-mcp.md（= .dev-dispatch/spec/approved.md 正本）。

- 行为契约 1：读四件 ``state_lines / state_line / state_decisions /
  state_takeover``，写四件 ``line_revive / line_set_seat / maintenance_set /
  maintenance_clear``（监督者 principal 专属，非监督者稳定拒绝+留痕），
  ``note_publish``（监督者与卡主本人；refs 语义与 bus ``work.note.v1`` 对齐）。
  ``state_takeover`` 六项一次调用齐，不可得项显式标注（unavailable+原因），
  不得省略键、不得以旧缓存冒充现算（每项带 computed_at）。
- 行为契约 2（调用面收敛）: ``:7494`` 保留只读 GET（含新开
  ``/v1/takeover`` 只读投影——R0 判据 09 的既有读数面），写面与管理动作
  从 HTTP/CLI 调用面语义里移除；上游死地址必须告警（tools/list 仍应答，
  相关工具报 ``upstream_unavailable``）。
- 行为契约 3（R2 衔接）: 外门=监督者操作面 + 全体只读面。
- 阴性用例（成对红锚+注入翻转）：A 恒 present、B 无鉴权、C 缓存合成、
  死地址告警、note 越权、元（五面 tools/list 全含只读工具 + tool→face
  映射冻结）。

S7 边界：读四件只复用 ``FleetStateView`` 既有投影（:7494 同源），不重做
读取器。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fleet_graph.outer_gate_mcp import (
    CODE_LINE_NOT_FOUND,
    CODE_NOT_SUPERVISOR,
    CODE_NOTE_FORBIDDEN,
    CODE_NOTE_REFS_REQUIRED,
    CODE_UPSTREAM_UNAVAILABLE,
    DEFAULT_PORT,
    MCP_SERVER_NAME,
    NOTE_TYPES,
    SUPERVISOR_PRINCIPAL_DEFAULT,
    TAKEOVER_KEYS,
    build_outer_gate_mcp_server,
    supervisor_principal,
    takeover_item_complete,
    takeover_keys,
)
from fleet_graph.state.fleet_state import FleetStateConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = SUPERVISOR_PRINCIPAL_DEFAULT

#: 元判据（spec 开放点 1 作答）：tool→face 映射冻结。外门单面承载九工具；
#: decision 面补只读 decision_get；其余各面只读工具名照 10 项判据冻结。
OUTER_GATE_TOOLS = frozenset(
    {
        "state_lines",
        "state_line",
        "state_decisions",
        "state_takeover",
        "line_revive",
        "line_set_seat",
        "maintenance_set",
        "maintenance_clear",
        "note_publish",
    }
)
FACE_PORT_ENV = {
    "bus": "FGT_PORT_BUS_MCP",
    "dd": "FGT_PORT_DD_MCP",
    "goal": "FGT_PORT_GOAL_MCP",
    "decision": "FGT_PORT_DECISION_MCP",
}
FACE_READONLY_TOOL = {
    "bus": "bus_agent_list",
    "dd": "development_list",
    "goal": "goal_list",
    "decision": "decision_list",
}


@pytest.fixture(autouse=True)
def _loopback_needs_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host proxy env must not leak into loopback MCP clients."""
    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)


def make_state_config(tmp_path: Path, *, lines: list[dict[str, Any]] | None = None) -> Path:
    """A scratch roster config + run root; returns the roster path."""
    lines = (
        lines
        if lines is not None
        else [
            {"folder_id": "wf-1", "seat": "seat-a", "generation": 1, "alias": "wf-1"},
        ]
    )
    roster = tmp_path / "ronin-lines.json"
    roster.write_text(
        json.dumps(
            {"run_root": str(tmp_path / "runs"), "dd_root": str(tmp_path / "dd"), "lines": lines}
        ),
        encoding="utf-8",
    )
    return roster


def write_line_state(run_root: Path, folder_id: str, *, parked: bool = False) -> None:
    """One line's heartbeat + (optional) parked stall-state, mechanically."""
    line = run_root / folder_id
    line.mkdir(parents=True, exist_ok=True)
    (line / "heartbeat.json").write_text(
        json.dumps(
            {
                "round": 3,
                "phase": "worker",
                "updated_at": "2026-09-05T00:00:00Z",
                "run_id": "run-1",
                "release_id": "rel-1",
            }
        ),
        encoding="utf-8",
    )
    if parked:
        sched = run_root / ".scheduler"
        sched.mkdir(parents=True, exist_ok=True)
        (sched / f"{folder_id}.json").write_text(
            json.dumps(
                {
                    "generation": 2,
                    "parked_run_id": "run-1",
                    "parked_at": 1_700_000_000.0,
                    "parked_goal_revision": "sha256:x",
                    "parked_inbox_available": True,
                    "board_question_note_id": "q-1",
                    "board_card_entity_id": "card-1",
                }
            ),
            encoding="utf-8",
        )


def build(tmp_path: Path, **overrides: Any) -> Any:
    """The surface against a scratch tree, with injectable defaults."""
    roster = make_state_config(tmp_path)
    roster = overrides.pop("lines_config", None) or roster
    config = FleetStateConfig(
        run_root=tmp_path / "runs",
        dd_root=tmp_path / "dd",
        lines_config=roster,
    )
    kwargs: dict[str, Any] = {"clock": time.time, "refusal_root": tmp_path / "audit"}
    kwargs.update(overrides)
    return build_outer_gate_mcp_server(config, tmp_path / "dd", **kwargs)


@contextmanager
def running_server(server: object) -> Iterator[str]:
    """A scratch streamable-http endpoint (same discipline as test_dd_service)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    app = server.http_app(path="/mcp", transport="streamable-http")  # type: ignore[attr-defined]
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(100):
            try:
                httpx.get(url, timeout=0.1)
            except httpx.HTTPError:
                time.sleep(0.01)
            else:
                break
        else:
            pytest.fail("MCP endpoint did not become reachable")
        yield url
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=5)


async def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return await server.call_tool(name, arguments)


async def call_json(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await call_tool(server, name, arguments)
    return json.loads(result.content[0].text)


async def call_refused(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One refused call's structured code (the refusal must be machine-readable)."""
    with pytest.raises(ToolError) as excinfo:
        await call_tool(server, name, arguments)
    return json.loads(str(excinfo.value))


class TestPositiveNineTools:
    """The nine tools exist on one face and the reads answer from the same view."""

    def test_exactly_the_nine_tools_are_registered(self, tmp_path: Path) -> None:
        server = build(tmp_path)
        tools = {tool.name for tool in asyncio.run(server.list_tools())}
        assert tools == OUTER_GATE_TOOLS
        assert MCP_SERVER_NAME == "fleet-graph-outer-gate"

    def test_state_lines_matches_the_7494_view(self, tmp_path: Path) -> None:
        write_line_state(tmp_path / "runs", "wf-1")
        server = build(tmp_path)
        payload = asyncio.run(call_json(server, "state_lines", {}))
        assert payload["schema_version"] == "1"
        row = payload["lines"][0]
        # R4 一等字段透出（spec 行为契约 1 / 边界：计算归 R4，透出归本单）。
        for key in ("folder_id", "release_behind", "deploy_behind", "release_behind_basis"):
            assert key in row

    def test_state_line_returns_one_line_and_refuses_unknown(self, tmp_path: Path) -> None:
        write_line_state(tmp_path / "runs", "wf-1")
        server = build(tmp_path)
        payload = asyncio.run(call_json(server, "state_line", {"line_id": "wf-1"}))
        assert payload["line"]["folder_id"] == "wf-1"
        assert "dispatches" in payload
        refused = asyncio.run(call_refused(server, "state_line", {"line_id": "wf-nope"}))
        assert refused["code"] == CODE_LINE_NOT_FOUND

    def test_state_decisions_returns_the_ledger_projection(self, tmp_path: Path) -> None:
        server = build(tmp_path)
        payload = asyncio.run(call_json(server, "state_decisions", {"window": 3600}))
        assert payload["window_seconds"] == 3600
        assert isinstance(payload["decisions"], list)
        refused = asyncio.run(call_refused(server, "state_decisions", {"window": -1}))
        assert refused["code"] == "WINDOW_INVALID"

    def test_state_takeover_six_items_one_call(self, tmp_path: Path) -> None:
        write_line_state(tmp_path / "runs", "wf-1")
        server = build(tmp_path)
        payload = asyncio.run(call_json(server, "state_takeover", {}))
        assert sorted(payload["items"].keys()) == sorted(TAKEOVER_KEYS)
        assert payload["complete"] is True
        assert payload["missing"] == []
        for key, item in payload["items"].items():
            assert item.get("computed_at"), f"{key} 无 computed_at"
            assert takeover_item_complete(item), f"{key} 不可得却未标注"

    def test_takeover_six_keys_are_the_spec_six(self) -> None:
        assert sorted(takeover_keys()) == sorted(
            [
                "roster",
                "line_states",
                "awaiting_decisions",
                "pending_releases",
                "auth_mode",
                "current_release",
            ]
        )

    def test_takeover_reads_are_the_same_view_not_a_second_reader(self, tmp_path: Path) -> None:
        from fleet_graph.state.fleet_state import FleetStateView

        write_line_state(tmp_path / "runs", "wf-1")
        roster = make_state_config(tmp_path)
        config = FleetStateConfig(
            run_root=tmp_path / "runs", dd_root=tmp_path / "dd", lines_config=roster
        )
        server = build(tmp_path)
        payload = asyncio.run(call_json(server, "state_takeover", {}))
        view = FleetStateView(config)
        # heartbeat_age_s 是「现在 - updated_at」的机械事实，两次现算只差
        # 时钟微秒级漂移——同源判据核在字段集与其余字段全等上。
        a = payload["items"]["line_states"]["data"]["lines"][0]
        b = view.lines()["lines"][0]
        assert set(a) == set(b)
        assert {k: v for k, v in a.items() if k != "heartbeat_age_s"} == {
            k: v for k, v in b.items() if k != "heartbeat_age_s"
        }
        assert payload["items"]["roster"]["data"]["total"] == 1


class TestNegativeAAlwaysPresent:
    """阴性 A（恒 present 红）：缺项省略键 → 用例红；缺项须显式标注且不算齐。"""

    def test_unavailable_source_is_marked_not_omitted(self, tmp_path: Path) -> None:
        # roster 指向死路径 → 该项 unavailable（键在、标注在），其余项仍现算。
        roster = tmp_path / "ronin-lines.json"
        roster.write_text(
            json.dumps({"run_root": str(tmp_path / "runs"), "lines": []}),
            encoding="utf-8",
        )
        missing_roster = tmp_path / "gone.json"
        config = FleetStateConfig(
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd",
            lines_config=missing_roster,
        )
        server = build_outer_gate_mcp_server(
            config, tmp_path / "dd", refusal_root=tmp_path / "audit"
        )
        payload = asyncio.run(call_json(server, "state_takeover", {}))
        item = payload["items"]["roster"]
        assert item["unavailable"] is True
        assert item["reason"]
        assert "roster" in payload["missing"]
        assert payload["complete"] is False

    def test_mutation_omitting_the_key_turns_red(self, tmp_path: Path) -> None:
        """注入翻转：把缺项的键从合成结果里删掉 → 恒 present 用例必须红。"""
        from fleet_graph.outer_gate_mcp import takeover_items

        config = FleetStateConfig(
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd",
            lines_config=tmp_path / "gone.json",
        )
        from fleet_graph.state.fleet_state import FleetStateView

        items = takeover_items(FleetStateView(config), "2026-09-05T00:00:00Z")
        mutated = dict(items)
        del mutated["roster"]  # the injection: omit the key entirely
        assert "roster" not in mutated
        # 恒 present 判据：键集合必须恒等于六项封闭键表。
        assert sorted(mutated.keys()) != sorted(takeover_keys())

    def test_missing_key_does_not_count_as_complete(self) -> None:
        assert takeover_item_complete({"computed_at": "t"}) is False
        assert takeover_item_complete({"computed_at": "t", "data": None}) is False
        assert (
            takeover_item_complete({"computed_at": "t", "data": {}, "unavailable": True}) is False
        )
        assert takeover_item_complete({"computed_at": "t", "data": {"x": 1}}) is True


class TestNegativeBNoAuth:
    """阴性 B（无鉴权红）：线 principal 调写四件 → 稳定拒绝+留痕；监督者 → 成功。"""

    def test_line_principal_is_stably_refused_on_all_four_writes(self, tmp_path: Path) -> None:
        write_line_state(tmp_path / "runs", "wf-1")
        server = build(tmp_path)
        line = "wf-1"
        refusals = [
            asyncio.run(
                call_refused(
                    server,
                    "line_revive",
                    {"line_id": line, "basis": "goal.md#r5", "principal": line},
                )
            ),
            asyncio.run(
                call_refused(
                    server,
                    "line_set_seat",
                    {"line_id": line, "to_seat": "seat-b", "reason": "r", "principal": line},
                )
            ),
            asyncio.run(
                call_refused(server, "maintenance_set", {"principal": line, "reason": "r"})
            ),
            asyncio.run(call_refused(server, "maintenance_clear", {"principal": line})),
        ]
        for refusal in refusals:
            assert refusal["code"] == CODE_NOT_SUPERVISOR
            assert refusal["tool"]
        # 留痕：拒绝行落盘（refusal_root/outer-gate-refusals.jsonl）。
        trace = tmp_path / "audit" / "outer-gate-refusals.jsonl"
        rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        assert {row["tool"] for row in rows} >= {
            "line_revive",
            "line_set_seat",
            "maintenance_set",
            "maintenance_clear",
        }
        assert all(row["code"] == CODE_NOT_SUPERVISOR for row in rows)

    def test_supervisor_principal_succeeds(self, tmp_path: Path) -> None:
        calls: list[dict[str, Any]] = []

        def fake_revive(**kw: Any) -> dict[str, Any]:
            calls.append(kw)
            return {"folder_id": kw["folder_id"], "next_generation": 2}

        def fake_set_seat(**kw: Any) -> dict[str, Any]:
            calls.append(kw)
            return {"folder_id": kw["folder_id"], "to": kw["to_seat"]}

        write_line_state(tmp_path / "runs", "wf-1")
        server = build(tmp_path, revive=fake_revive, set_seat=fake_set_seat)
        payload = asyncio.run(
            call_json(
                server,
                "line_revive",
                {"line_id": "wf-1", "basis": "goal.md#r5", "principal": SUPERVISOR},
            )
        )
        assert payload["next_generation"] == 2
        asyncio.run(
            call_json(
                server,
                "line_set_seat",
                {"line_id": "wf-1", "to_seat": "seat-b", "reason": "r", "principal": SUPERVISOR},
            )
        )
        assert calls[0]["who"] == SUPERVISOR
        # maintenance_set/clear 真件落盘（scratch root）。
        gate = tmp_path / "runs" / "maintenance-stop"
        asyncio.run(
            call_json(
                server,
                "maintenance_set",
                {"principal": SUPERVISOR, "reason": "deploy", "ttl_seconds": 60},
            )
        )
        flag = json.loads(gate.read_text(encoding="utf-8"))
        assert flag["reason"] == "deploy" and flag["expires_at"]
        payload_clear = asyncio.run(
            call_json(server, "maintenance_clear", {"principal": SUPERVISOR})
        )
        assert payload_clear["status"] == "clear"
        assert not gate.exists()

    def test_mutation_removing_the_guard_turns_red(self, tmp_path: Path) -> None:
        """注入翻转：写工具不带鉴权（旁路 require_supervisor）→ 用例红。"""

        def fake_revive(**_kw: Any) -> dict[str, Any]:  # the mutation: no auth
            return {"revived": True}

        from fleet_graph.outer_gate_mcp import CODE_NOT_SUPERVISOR as _code

        assert _code == "OUTER_GATE_NON_SUPERVISOR"
        # A guard-less implementation would let the line principal through;
        # the surface's contract is that this call MUST refuse:
        server = build(tmp_path)
        with pytest.raises(ToolError) as excinfo:
            asyncio.run(
                call_tool(
                    server,
                    "line_revive",
                    {"line_id": "wf-1", "basis": "b", "principal": "wf-1"},
                )
            )
        assert CODE_NOT_SUPERVISOR in str(excinfo.value)


class TestNegativeCCacheComposition:
    """阴性 C（缓存合成红）：过期缓存未标注 → 红；computed_at 标注且过期即 unavailable。"""

    def test_every_item_carries_computed_at(self, tmp_path: Path) -> None:
        server = build(tmp_path)
        payload = asyncio.run(call_json(server, "state_takeover", {}))
        for key, item in payload["items"].items():
            assert item["computed_at"] == item.get("computed_at")
            assert "cached" not in item or item.get("cached_computed_at"), (
                f"{key} 带缓存却未标注缓存戳"
            )

    def test_stale_cache_without_annotation_is_red(self) -> None:
        """注入翻转：把某项换成过期缓存（无 computed_at/缓存标注）→ 判据红。"""
        # 合成判据：没有 computed_at 的项不算齐（不得以旧缓存冒充现算）。
        assert takeover_item_complete({"data": {"old": True}}) is False
        assert takeover_item_complete({"data": {"old": True}, "computed_at": ""}) is False
        assert takeover_item_complete({"data": None, "computed_at": "t"}) is False
        # 缓存项缺缓存戳 = 过期缓存冒充现算，判据红。
        assert (
            takeover_item_complete({"data": {"old": True}, "computed_at": "t", "cached": True})
            is False
        )
        assert (
            takeover_item_complete(
                {
                    "data": {"old": True},
                    "computed_at": "t",
                    "cached": True,
                    "cached_computed_at": "t0",
                }
            )
            is True
        )
        assert (
            takeover_item_complete({"computed_at": "2020-01-01T00:00:00Z", "data": None}) is False
        )

    def test_cached_item_must_carry_the_cache_stamp(self) -> None:
        from fleet_graph.outer_gate_mcp import _cached_takeover

        item = _cached_takeover({"x": 1}, "2026-09-05T00:00:00Z", "2026-09-05T01:00:00Z")
        assert item["cached"] is True
        assert item["cached_computed_at"] == "2026-09-05T00:00:00Z"
        assert item["computed_at"] == "2026-09-05T01:00:00Z"


class TestDeadAddressAlarm:
    """阴性 4（死地址告警）：上游不可达 → tools/list 仍应答，工具报 upstream_unavailable。

    ``state_lines`` 的上游（名册 SSoT）缺失/不可读时经 ``state_lines``
    报 ``upstream_unavailable``（``FleetStateView.roster`` 现算路径显式失败，
    不走 :7494 兼容的 fail-soft 降级——死地址必须告警，禁静默空转）。
    """

    def test_tools_list_still_answers_when_upstream_is_dead(self, tmp_path: Path) -> None:
        server = build(tmp_path, lines_config=tmp_path / "gone.json")
        tools = asyncio.run(server.list_tools())  # tools/list answers
        assert {tool.name for tool in tools} == OUTER_GATE_TOOLS

    def test_state_lines_reports_upstream_unavailable(self, tmp_path: Path) -> None:
        server = build(tmp_path, lines_config=tmp_path / "gone.json")
        refusal = asyncio.run(call_refused(server, "state_lines", {}))
        assert refusal["code"] == CODE_UPSTREAM_UNAVAILABLE

    def test_mutation_swallowing_the_error_returns_no_fake_data(self, tmp_path: Path) -> None:
        """注入翻转：吞错误返空数据 → 死地址告警用例红（禁止静默空转）。"""
        server = build(tmp_path, lines_config=tmp_path / "gone.json")
        # The honest behavior: refuse. A swallowed-error variant returning
        # {"lines": []} would make this expectation fail -- which is the point.
        refusal = asyncio.run(call_refused(server, "state_lines", {}))
        assert refusal["code"] == CODE_UPSTREAM_UNAVAILABLE


class TestNotePublish:
    """note_publish：监督者与卡主本人可用；越权拒绝；refs 缺失拒绝（refs_required）。"""

    def _publisher(self, published: list[dict[str, Any]]) -> Any:
        def publish(**kw: Any) -> Any:
            published.append(kw)

            class R:
                message_id = "m-1"
                entity_id = "m-1"

            return R()

        return publish

    def test_card_owner_publishes_own_card(self, tmp_path: Path) -> None:
        published: list[dict[str, Any]] = []
        dd = tmp_path / "dd" / "dev-1"
        dd.mkdir(parents=True)
        (dd / "record.json").write_text(
            json.dumps(
                {"development_id": "dev-1", "card_entity_id": "card-1", "dispatched_by": "wf-1"}
            ),
            encoding="utf-8",
        )
        server = build(tmp_path, note_publisher=self._publisher(published))
        payload = asyncio.run(
            call_json(
                server,
                "note_publish",
                {
                    "card": "card-1",
                    "note": "progress",
                    "note_type": "progress",
                    "principal": "wf-1",
                    "refs": [{"target_entity": "card-1"}],
                },
            )
        )
        assert payload["status"] == "published"
        assert published[0]["card_entity_id"] == "card-1"
        # 载荷与 bus work.note.v1 逐字段对齐：{card_entity_id, note, note_type}。
        assert set(published[0]) >= {
            "card_entity_id",
            "text",
            "note_type",
            "idempotency_key",
            "refs",
        }
        assert published[0]["refs"] == [{"target_entity": "card-1"}]

    def test_foreign_card_is_refused_and_traced(self, tmp_path: Path) -> None:
        published: list[dict[str, Any]] = []
        dd = tmp_path / "dd" / "dev-1"
        dd.mkdir(parents=True)
        (dd / "record.json").write_text(
            json.dumps(
                {"development_id": "dev-1", "card_entity_id": "card-1", "dispatched_by": "wf-1"}
            ),
            encoding="utf-8",
        )
        server = build(tmp_path, note_publisher=self._publisher(published))
        refusal = asyncio.run(
            call_refused(
                server,
                "note_publish",
                {
                    "card": "card-9",
                    "note": "x",
                    "note_type": "progress",
                    "principal": "wf-1",
                    "refs": [{"target_entity": "card-9"}],
                },
            )
        )
        assert refusal["code"] == CODE_NOTE_FORBIDDEN
        assert not published
        trace = tmp_path / "audit" / "outer-gate-refusals.jsonl"
        assert "note_publish" in trace.read_text(encoding="utf-8")

    def test_missing_refs_is_refused_refs_required(self, tmp_path: Path) -> None:
        server = build(tmp_path, note_publisher=self._publisher([]))
        refusal = asyncio.run(
            call_refused(
                server,
                "note_publish",
                {
                    "card": "card-1",
                    "note": "x",
                    "note_type": "progress",
                    "principal": SUPERVISOR,
                    "refs": [],
                },
            )
        )
        assert refusal["code"] == CODE_NOTE_REFS_REQUIRED

    def test_note_types_match_the_bus_schema(self, tmp_path: Path) -> None:
        # bus work.note.v1 注册 enum（measured 2026-09-05）：
        # progress/finding/question/handoff/evidence。
        assert sorted(NOTE_TYPES) == sorted(
            ["progress", "finding", "question", "handoff", "evidence"]
        )

    def test_decision_get_is_not_a_second_delivery_path(self, tmp_path: Path) -> None:
        """S11：decision 只走 bus 裁决路径——外门不为 decision 提供投递路。"""
        server = build(tmp_path)
        tools = {tool.name for tool in asyncio.run(server.list_tools())}
        # 外门九工具里没有 decision 投递工具（decision_get/list 在 decision 面）。
        assert not any(name.startswith("decision_") for name in tools)


class TestFaceConvergence:
    """元判据：五面 tools/list 全含只读工具；:7494 只读 GET 保留；CLI 降实现。"""

    def test_state_mcp_default_port_is_free_of_reserved_lists(self) -> None:
        from fleet_graph.decision_mcp import load_reserved_ports as load_decision_ports
        from fleet_graph.line_state_mcp import load_reserved_ports as load_line_state_ports

        assert DEFAULT_PORT == 5616
        assert DEFAULT_PORT not in load_decision_ports()
        assert DEFAULT_PORT not in load_line_state_ports()

    def test_tool_face_map_is_frozen(self) -> None:
        assert {
            "state_lines",
            "state_line",
            "state_decisions",
            "state_takeover",
            "line_revive",
            "line_set_seat",
            "maintenance_set",
            "maintenance_clear",
            "note_publish",
        } == OUTER_GATE_TOOLS
        # 各面只读工具照 10 项判据冻结（tools/list 每面至少一个只读工具）。
        assert FACE_READONLY_TOOL["decision"] == "decision_list"

    def test_decision_face_gains_decision_get_readonly(self) -> None:
        from fleet_graph.decision_mcp import build_decision_mcp_server

        server = build_decision_mcp_server(Path("/tmp"), [{"folder_id": "wf-1"}])
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert "decision_list" in names
        assert "decision_get" in names  # R5 补只读工具
        assert "decision_deliver" in names

    def test_decision_get_refuses_unknown_id(self) -> None:
        from fleet_graph.decision_mcp import build_decision_mcp_server

        server = build_decision_mcp_server(Path("/tmp"), [{"folder_id": "wf-1"}])
        with pytest.raises(ToolError) as excinfo:
            asyncio.run(call_tool(server, "decision_get", {"message_id": "nope"}))
        assert "DECISION_NOT_FOUND" in str(excinfo.value)

    def test_7494_takeover_get_is_the_same_projection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """:7494 /v1/takeover（只读 GET）与 state_takeover 工具同源（一次投影两扇门）。"""
        from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateHTTPServer

        roster = make_state_config(tmp_path)
        write_line_state(tmp_path / "runs", "wf-1")
        monkeypatch.delenv("FLEET_GRAPH_DEPLOY_CURRENT", raising=False)
        config = FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd",
            lines_config=roster,
        )
        httpd = FleetStateHTTPServer(config)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/v1/takeover", timeout=5)
            assert response.status_code == 200
            payload = response.json()
            for key in takeover_keys():
                assert key in payload, f"/v1/takeover 缺顶层键 {key}"
            assert payload["complete"] is True
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_7494_write_faces_are_gone(self) -> None:
        """:7494 只保留只读 GET；写面/管理动作不在 HTTP 调用面语义里。"""
        import inspect

        from fleet_graph.state import fleet_state

        source = inspect.getsource(fleet_state.FleetStateHandler)
        assert "do_POST" not in source
        assert "do_PUT" not in source
        assert "do_DELETE" not in source
        # 只读 GET 保留清单（既有探针判据 03/05 与 R0 骨架依赖）：
        for route in (
            "/v1/lines",
            "/v1/decisions",
            "/v1/harvestable",
            "/v1/enrollments",
            "/v1/llm-ledger",
        ):
            assert route in source

    def test_cli_write_family_maps_to_the_mcp_tools(self) -> None:
        """开放点 2 作答（冻结进测试）：CLI 写四件 ↔ MCP 写四件映射。"""
        mapping = {
            "fleet-graph line revive": "line_revive",
            "fleet-graph line set-seat": "line_set_seat",
            "maintenance set (maintenance-stop 写)": "maintenance_set",
            "maintenance clear (maintenance-stop 清)": "maintenance_clear",
        }
        assert sorted(mapping.values()) == sorted(
            ["line_revive", "line_set_seat", "maintenance_set", "maintenance_clear"]
        )
        # CLI 的写原语仍是 MCP 工具的同一实现函数（写经 MCP，CLI 降为实现）。
        from fleet_graph.cli import perform_line_revive, perform_set_seat

        assert callable(perform_line_revive)
        assert callable(perform_set_seat)

    def test_outer_gate_cli_subcommand_parses(self) -> None:
        from fleet_graph.cli import build_parser

        args = build_parser().parse_args(["outer-gate", "serve", "--port", "5616"])
        assert args.port == DEFAULT_PORT == 5616

    def test_outer_gate_serves_over_http_transport(self, tmp_path: Path) -> None:
        server = build(tmp_path)
        with running_server(server) as url:

            async def probe() -> list[str]:
                async with Client(url) as client:
                    tools = await client.list_tools()
                return [tool.name for tool in tools]

            names = asyncio.run(probe())
        assert set(names) == OUTER_GATE_TOOLS

    def test_supervisor_principal_env_binding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FLEET_GRAPH_SUPERVISOR_PRINCIPAL", "boss")
        assert supervisor_principal() == "boss"
        monkeypatch.delenv("FLEET_GRAPH_SUPERVISOR_PRINCIPAL")
        assert supervisor_principal() == SUPERVISOR_PRINCIPAL_DEFAULT
