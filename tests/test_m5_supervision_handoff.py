"""M5 监督面冷启动接手：一个只读 MCP 工具，一次调用返回全部现状。

双向判据逐字对齐 goal.md M5：

- **阳性（冷启动演练，不是断言）**：一个零上下文 session 只调
  ``supervision_handoff`` 这一个工具，就拿到回答「现在哪几条线在跑 / 谁在等我
  拍板 / 有什么该收割 / 我的作业账在哪一卷」所需的全部权威值，且逐项与同刻
  ``:7494`` 读模型（:class:`FleetStateView`）一致。
- **阴性①**：该工具不得暴露任何写能力——``tools/list`` 与各工具 ``inputSchema``
  不出现任何写原语/写动作（零参数工具，schema 的 properties 为空）。
- **阴性②**：任一「读不到/缺失」的数据项返回体显式 ``unavailable`` / ``missing``
  标记，绝不返回空对象/空数组冒充「没有」（注入缺失场景断言标记存在）。

健康隔离纪律：测试只在 tmp 目录里造 scratch 工件，绝不触碰生产账本/生产文件。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.decision_mcp import RESERVED_PORTS_FILE
from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateView
from fleet_graph.state.run_artifacts import iso
from fleet_graph.supervision_mcp import (
    AUTH_MODE_FULL_AUTO,
    AUTH_MODE_SEMI_AUTO,
    DEFAULT_PORT,
    MCP_SERVER_NAME,
    SupervisionConfig,
    SupervisionHandoff,
    build_supervision_mcp_server,
    is_unavailable,
    load_reserved_ports,
    read_roster,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Write verbs the handoff surface must never expose (阴性①). The single tool is
#: zero-argument, so its inputSchema properties must be empty and no member of this
#: set may appear in the serialized schema or tool name.
WRITE_PRIMITIVES = {
    "write",
    "create",
    "set",
    "update",
    "delete",
    "edit",
    "put",
    "post",
    "approve",
    "reject",
    "admit",
    "resume",
    "deliver",
    "launch",
    "start",
    "stop",
    "harvest",
    "publish",
    "gate",
    "merge",
    "deploy",
    "revert",
    "reset",
    "remove",
    "mutate",
    "exec",
    "run",
}


class FakeClock:
    def __init__(self, now: float = 1_787_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def write_roster(path: Path, lines: list[dict[str, Any]], run_root: Path | None = None) -> None:
    payload: dict[str, Any] = {"lines": lines}
    if run_root is not None:
        payload["run_root"] = str(run_root)
    path.write_text(json.dumps(payload), encoding="utf-8")


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
                "pid": 1,
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
    run_id: str | None = None,
) -> None:
    (run_root / folder_id).mkdir(parents=True, exist_ok=True)
    (run_root / folder_id / "terminal.json").write_text(
        json.dumps(
            {
                "run_id": run_id or f"run-{folder_id}",
                "folder_id": folder_id,
                "terminal": terminal,
                "pump_fault": False,
                "rounds": 2,
                "reason": "need human",
                "at": iso(1_787_000_000.0),
                "pid": 1,
                "waiting_on": waiting_on,
                "waiting_on_declared": waiting_on,
                "log_path": f"/data/fleet-graph/logs/{folder_id}.log",
            }
        ),
        encoding="utf-8",
    )


def write_dev(dd_root: Path, dev_id: str, *, terminal: str, state: str = "awaiting_gate") -> None:
    d = dd_root / dev_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "record.json").write_text(
        json.dumps({"development_id": dev_id, "repo_path": "/tmp/x"}), encoding="utf-8"
    )
    (d / "status.json").write_text(
        json.dumps(
            {
                "development_id": dev_id,
                "state": state,
                "stage": "implement",
                "terminal": terminal,
                "head_commit": f"head-{dev_id}",
            }
        ),
        encoding="utf-8",
    )


class TestReadRoster:
    def test_missing_roster_is_unreadable_not_empty(self, tmp_path: Path) -> None:
        lines, error = read_roster(tmp_path / "nope.json")
        assert lines == []
        assert error is not None

    def test_empty_roster_is_genuinely_empty_not_unreadable(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        write_roster(roster, [])
        lines, error = read_roster(roster)
        assert lines == []
        assert error is None

    def test_alias_seat_enabled_are_read(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        write_roster(
            roster,
            [
                {"folder_id": "wf-1", "seat": "s1", "alias": "a1", "enabled": True},
                {"folder_id": "wf-2", "seat": "s2", "alias": "a2", "enabled": False},
            ],
        )
        lines, error = read_roster(roster)
        assert error is None
        assert lines == [
            {"folder_id": "wf-1", "alias": "a1", "seat": "s1", "enabled": True, "generation": 1},
            {"folder_id": "wf-2", "alias": "a2", "seat": "s2", "enabled": False, "generation": 1},
        ]


class TestNegative1NoWriteSurface:
    def test_the_only_tool_is_a_zero_argument_handoff(self) -> None:
        server = build_supervision_mcp_server()
        tools = asyncio.run(server.list_tools())
        assert MCP_SERVER_NAME == "fleet-graph-supervision"
        assert {tool.name for tool in tools} == {"supervision_handoff"}
        for tool in tools:
            assert tool.parameters.get("properties") == {}
            assert not tool.parameters.get("required")

    def test_tools_list_and_input_schema_carry_no_write_primitives(self) -> None:
        server = build_supervision_mcp_server()
        tools = asyncio.run(server.list_tools())
        for tool in tools:
            assert not (WRITE_PRIMITIVES & set(tool.name.split("_")))
            schema = json.dumps(tool.parameters, sort_keys=True).lower()
            for verb in WRITE_PRIMITIVES:
                assert verb not in schema, f"{verb!r} leaked into inputSchema of {tool.name}"


class TestNegative2MissingIsExplicitlyMarked:
    def _config(self, tmp_path: Path) -> SupervisionConfig:
        state = FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd-missing",
            lines_config=tmp_path / "missing-roster.json",
            bridge_state_dir=tmp_path / "bridge",
            clock=FakeClock(),
        )
        return SupervisionConfig(
            state=state,
            clock=FakeClock(),
            maintenance_stop_path=tmp_path / "no-maintenance-stop",
            release_current_path=tmp_path / "no-current",
        )

    def test_missing_roster_and_upstreams_are_marked_unavailable(self, tmp_path: Path) -> None:
        snapshot = SupervisionHandoff(self._config(tmp_path)).build()
        assert snapshot["degraded"] is True
        assert is_unavailable(snapshot["roster"])
        assert is_unavailable(snapshot["line_status"])
        assert is_unavailable(snapshot["awaiting_decision"]["parked_lines"])
        assert is_unavailable(snapshot["awaiting_decision"]["gate_developments"])
        assert is_unavailable(snapshot["harvestable"])
        assert is_unavailable(snapshot["releases"]["main"])
        assert is_unavailable(snapshot["releases"]["deployed"])
        assert is_unavailable(snapshot["releases"]["running"])
        assert "roster" in snapshot["unavailable_sources"]
        assert "harvestable" in snapshot["unavailable_sources"]

    def test_missing_supervision_volume_and_auth_mode_fail_safe(self, tmp_path: Path) -> None:
        snapshot = SupervisionHandoff(self._config(tmp_path)).build()
        assert snapshot["supervision_volume"] == {"folder_id": None, "missing": True}
        assert snapshot["authorization_mode"] == AUTH_MODE_SEMI_AUTO

    def test_malformed_auth_mode_fails_safe_to_semi_auto(self, tmp_path: Path) -> None:
        config = replace(self._config(tmp_path), authorization_mode="banana")
        assert SupervisionHandoff(config).build()["authorization_mode"] == AUTH_MODE_SEMI_AUTO

    def test_missing_dd_root_is_not_reported_as_no_developments(self, tmp_path: Path) -> None:
        config = replace(self._config(tmp_path), supervision_folder_id="wf-sup")
        snapshot = SupervisionHandoff(config).build()
        assert is_unavailable(snapshot["harvestable"])
        assert is_unavailable(snapshot["awaiting_decision"]["gate_developments"])


class TestPositiveColdStartDrill:
    def test_one_call_answers_the_four_questions(self, tmp_path: Path) -> None:
        clock = FakeClock()
        run_root = tmp_path / "runs"
        dd_root = tmp_path / "dd"
        lines_config = tmp_path / "ronin-lines.json"

        write_roster(
            lines_config,
            [
                {"folder_id": "wf-1", "seat": "s1", "alias": "a1", "enabled": True},
                {"folder_id": "wf-2", "seat": "s2", "alias": "a2", "enabled": True},
                {"folder_id": "wf-3", "seat": "s3", "alias": "a3", "enabled": False},
            ],
            run_root=run_root,
        )
        # wf-1: parked, waiting on a human decision.
        write_heartbeat(
            run_root,
            "wf-1",
            round_no=3,
            phase="coordinator",
            updated_at=iso(clock() - 100.0),
            release_id="rel-A",
        )
        write_terminal(run_root, "wf-1", terminal="blocked", waiting_on="decision")
        # wf-2: live, no terminal.
        write_heartbeat(
            run_root,
            "wf-2",
            round_no=1,
            phase="worker",
            updated_at=iso(clock() - 10.0),
            release_id="rel-B",
        )
        # wf-3: disabled, no artifacts -> still a roster line.

        # Pre-establish the E5 baseline so a fresh complete is listed, not cleared.
        (run_root / ".scheduler").mkdir(parents=True, exist_ok=True)
        (run_root / ".scheduler" / "e5-baseline.json").write_text(
            json.dumps({"development_ids": []}), encoding="utf-8"
        )
        write_dev(dd_root, "dev-new", terminal="complete")

        def gate_seam() -> list[dict[str, Any]]:
            return [
                {
                    "development_id": "dev-gate",
                    "state": "awaiting_gate",
                    "stage": "implement",
                    "head_commit": "head-gate",
                    "awaiting": {"question_note_id": "q1"},
                }
            ]

        def main_head() -> str:
            return "abc123def456"

        def release_id() -> str:
            return "rel-deployed"

        state = FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=run_root,
            dd_root=dd_root,
            lines_config=lines_config,
            bridge_state_dir=tmp_path / "bridge",
            clock=clock,
        )
        config = SupervisionConfig(
            state=state,
            supervision_folder_id="wf-sup",
            authorization_mode=AUTH_MODE_FULL_AUTO,
            clock=clock,
            awaiting_gate=gate_seam,
            main_head=main_head,
            release_id=release_id,
        )
        snapshot = SupervisionHandoff(config).build()

        assert snapshot["degraded"] is False
        assert snapshot["supervision_volume"] == {"folder_id": "wf-sup"}
        assert snapshot["authorization_mode"] == AUTH_MODE_FULL_AUTO

        # Question 1: which lines are running, and the roster.
        assert snapshot["roster"]["total"] == 3
        assert snapshot["roster"]["enabled"] == 2
        by_folder = {line["folder_id"]: line for line in snapshot["line_status"]["lines"]}
        assert by_folder["wf-1"]["parked"] is True
        assert by_folder["wf-1"]["terminal"] == "blocked"
        assert by_folder["wf-1"]["heartbeat_age_s"] == pytest.approx(100.0, abs=0.5)
        assert by_folder["wf-2"]["terminal"] is None
        assert by_folder["wf-2"]["parked"] is False
        assert by_folder["wf-3"]["enabled"] is False

        # Question 2: who awaits my decision (both required kinds).
        assert snapshot["awaiting_decision"]["parked_lines"] == [
            {"folder_id": "wf-1", "generation": 1, "waiting_on": "decision"}
        ]
        assert snapshot["awaiting_decision"]["gate_developments"][0]["development_id"] == "dev-gate"

        # Question 3: what to harvest.
        harvestable = snapshot["harvestable"]["developments"]
        assert [d["development_id"] for d in harvestable] == ["dev-new"]

        # Question 4: my homework volume, and the release facts.
        assert snapshot["releases"]["main"] == "abc123def456"
        assert snapshot["releases"]["deployed"] == "rel-deployed"
        running = {r["folder_id"]: r["release_id"] for r in snapshot["releases"]["running"]}
        assert running["wf-1"] == "rel-A"
        assert running["wf-2"] == "rel-B"

    def test_every_item_matches_the_read_model_authority(self, tmp_path: Path) -> None:
        clock = FakeClock()
        run_root = tmp_path / "runs"
        dd_root = tmp_path / "dd"
        lines_config = tmp_path / "ronin-lines.json"
        write_roster(
            lines_config,
            [{"folder_id": "wf-1", "seat": "s1", "alias": "a1", "enabled": True}],
            run_root=run_root,
        )
        write_heartbeat(
            run_root, "wf-1", round_no=5, phase="coordinator", updated_at=iso(clock() - 40.0)
        )
        write_terminal(run_root, "wf-1", terminal="blocked", waiting_on="decision")
        (run_root / ".scheduler").mkdir(parents=True, exist_ok=True)
        (run_root / ".scheduler" / "e5-baseline.json").write_text(
            json.dumps({"development_ids": []}), encoding="utf-8"
        )
        write_dev(dd_root, "dev-new", terminal="complete")

        state = FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=run_root,
            dd_root=dd_root,
            lines_config=lines_config,
            bridge_state_dir=tmp_path / "bridge",
            clock=clock,
        )
        config = SupervisionConfig(state=state, supervision_folder_id="wf-sup", clock=clock)

        snapshot = SupervisionHandoff(config).build()
        view = FleetStateView(state)

        authoritative_lines = {entry["folder_id"]: entry for entry in view.lines()["lines"]}
        for line in snapshot["line_status"]["lines"]:
            source = authoritative_lines[line["folder_id"]]
            assert line["terminal"] == source["terminal"]
            assert line["parked"] == source["parked"]
            assert line["wake_facts_stale"] == source["wake_facts_stale"]
            assert line["generation"] == source["generation"]
            assert line["round"] == source["round"]
            assert line["heartbeat_age_s"] == source["heartbeat_age_s"]
            assert line["release_id"] == source["release_id"]

        assert snapshot["harvestable"]["developments"] == view.harvestable()["developments"]


class TestMaintenanceWindow:
    def _state(self, tmp_path: Path) -> FleetStateConfig:
        return FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd",
            lines_config=tmp_path / "missing.json",
            bridge_state_dir=tmp_path / "bridge",
            clock=FakeClock(),
        )

    def test_absent_flag_is_inactive_not_unavailable(self, tmp_path: Path) -> None:
        config = SupervisionConfig(
            state=self._state(tmp_path),
            maintenance_stop_path=tmp_path / "no-flag",
            clock=FakeClock(),
        )
        window = SupervisionHandoff(config).build()["maintenance_window"]
        assert window == {"active": False, "status": "inactive"}

    def test_unexpired_flag_is_active(self, tmp_path: Path) -> None:
        flag = tmp_path / "maintenance-stop"
        flag.write_text(
            json.dumps(
                {
                    "reason": "drill",
                    "issued_by": "wig",
                    "expires_at": iso(1_787_003_600.0),
                }
            ),
            encoding="utf-8",
        )
        config = SupervisionConfig(
            state=self._state(tmp_path), maintenance_stop_path=flag, clock=FakeClock()
        )
        window = SupervisionHandoff(config).build()["maintenance_window"]
        assert window["active"] is True
        assert window["status"] == "active"

    def test_unparseable_flag_is_holding_not_absent(self, tmp_path: Path) -> None:
        flag = tmp_path / "maintenance-stop"
        flag.write_text("not-json", encoding="utf-8")
        config = SupervisionConfig(
            state=self._state(tmp_path), maintenance_stop_path=flag, clock=FakeClock()
        )
        window = SupervisionHandoff(config).build()["maintenance_window"]
        assert window["active"] is True
        assert window["status"] == "unparseable"


class TestPortR2:
    def test_default_port_is_unreserved(self) -> None:
        assert DEFAULT_PORT == 5615
        assert DEFAULT_PORT not in load_reserved_ports()

    def test_the_committed_reserved_list_is_the_single_source(self) -> None:
        assert RESERVED_PORTS_FILE.exists()
        ports = load_reserved_ports()
        assert 5613 in ports
        assert 7494 in ports
        assert DEFAULT_PORT not in ports
