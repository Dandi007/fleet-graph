"""M1 fleet-state read-model: read-only, schema, field names, two views.

Synthetic artifacts (temp run_root heartbeat/terminal + temp dd status + a
bridge.sqlite3 fixture) drive the load-bearing cases:

- ``GET /v1/lines`` returns ``schema_version`` and ``lines`` as a list, and
  every ``line_obj`` carries exactly the A.2 field names;
- ``heartbeat_age_s`` is the now-minus-``updated_at`` mechanical difference,
  measured against the synthetic heartbeat's own ``updated_at``;
- ``parked`` follows ``waiting_on == "decision"`` (the ``normalize_waiting_on``
  semantics), surfaced through ``wake_facts``;
- ``GET /v1/decisions`` returns ``schema_version`` and ``decisions`` as a
  list, with ``swallowed`` entries carrying a ``reason``;
- missing/bad artifacts degrade per entry -- never a 5xx for the whole chain.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from fleet_graph.decision_bridge.store import (
    STATUS_NOOP,
    STATUS_REFUSED,
    STATUS_RESUMED,
    BridgeStore,
)
from fleet_graph.state.fleet_state import (
    FleetStateConfig,
    FleetStateHTTPServer,
    FleetStateView,
)
from fleet_graph.state.run_artifacts import iso

LINES_SCHEMA_VERSION = "1"
LINE_OBJ_FIELDS = {
    "folder_id",
    "generation",
    "round",
    "phase",
    "heartbeat_age_s",
    "terminal",
    "parked",
    "wake_facts",
}


class FakeClock:
    def __init__(self, start: float = 1_787_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def write_heartbeat(
    run_root: Path, folder_id: str, *, round_no: int, phase: str, updated_at: str
) -> None:
    (run_root / folder_id).mkdir(parents=True, exist_ok=True)
    (run_root / folder_id / "heartbeat.json").write_text(
        json.dumps(
            {
                "run_id": f"run-{folder_id}",
                "folder_id": folder_id,
                "round": round_no,
                "phase": phase,
                "pid": 1234,
                "started_at": updated_at,
                "phase_started_at": updated_at,
                "updated_at": updated_at,
                "log_path": f"/data/fleet-graph/logs/{folder_id}.log",
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
) -> None:
    (run_root / folder_id).mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "run_id": f"run-{folder_id}",
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


def seed_bridge(state_dir: Path) -> None:
    store = BridgeStore(state_dir).open()
    try:
        store.seal_terminal(
            {
                "source_message_id": "d-consumed",
                "action_key": "e1:d-consumed:dd:dev-1:1",
                "target_kind": "dd",
                "target_id": "dev-1",
                "generation": 1,
                "question_note_id": "q-1",
                "card_entity_id": "card-1",
                "status": STATUS_RESUMED,
                "reason": "",
                "source_event": {},
            },
            advance_seq=1,
        )
        store.seal_terminal(
            {
                "source_message_id": "d-swallowed",
                "action_key": "e1:d-swallowed:dd:dev-2:1",
                "target_kind": "dd",
                "target_id": "dev-2",
                "generation": 1,
                "question_note_id": "q-2",
                "card_entity_id": "card-2",
                "status": STATUS_NOOP,
                "reason": "no_waiting_owner",
                "source_event": {},
            },
            advance_seq=2,
        )
        store.seal_terminal(
            {
                "source_message_id": "d-refused",
                "action_key": "e1:d-refused:dd:dev-3:1",
                "target_kind": "dd",
                "target_id": "dev-3",
                "generation": 1,
                "question_note_id": "q-3",
                "card_entity_id": "card-3",
                "status": STATUS_REFUSED,
                "reason": "gate_refused",
                "source_event": {},
            },
            advance_seq=3,
        )
    finally:
        store.close()


@pytest.fixture
def synthetic(tmp_path: Path) -> dict[str, Any]:
    run_root = tmp_path / "runs"
    (run_root / "wf-000001").mkdir(parents=True)
    write_heartbeat(
        run_root,
        "wf-000001",
        round_no=3,
        phase="coordinator",
        updated_at=iso(1_787_000_000.0),
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
    bridge_dir = tmp_path / "bridge"
    seed_bridge(bridge_dir)
    return {
        "run_root": run_root,
        "lines_config": lines_config,
        "bridge_dir": bridge_dir,
        "tmp_path": tmp_path,
    }


def make_config(synthetic: dict[str, Any], clock: FakeClock | None = None) -> FleetStateConfig:
    return FleetStateConfig(
        host="127.0.0.1",
        port=0,
        run_root=synthetic["run_root"],
        dd_root=synthetic["tmp_path"] / "dd",
        lines_config=synthetic["lines_config"],
        bridge_state_dir=synthetic["bridge_dir"],
        clock=clock if clock is not None else time.time,
    )


# --- /v1/lines --------------------------------------------------------------


class TestLinesView:
    def test_returns_schema_version_and_lines_list(self, synthetic: dict[str, Any]) -> None:
        payload = FleetStateView(make_config(synthetic)).lines()
        assert payload["schema_version"] == LINES_SCHEMA_VERSION
        assert isinstance(payload["lines"], list)
        assert [line["folder_id"] for line in payload["lines"]] == ["wf-000001", "wf-000002"]

    def test_line_obj_field_names_match_spec_a2(self, synthetic: dict[str, Any]) -> None:
        payload = FleetStateView(make_config(synthetic)).lines()
        for line in payload["lines"]:
            assert set(line.keys()) == LINE_OBJ_FIELDS, line

    def test_heartbeat_age_s_is_now_minus_updated_at(self, synthetic: dict[str, Any]) -> None:
        clock = FakeClock(start=1_787_000_123.0)
        payload = FleetStateView(make_config(synthetic, clock=clock)).lines()
        by_id = {line["folder_id"]: line for line in payload["lines"]}
        # heartbeat updated_at = 1_787_000_000 -> age 123s
        assert by_id["wf-000001"]["heartbeat_age_s"] == pytest.approx(123.0, abs=0.5)
        assert by_id["wf-000001"]["round"] == 3
        assert by_id["wf-000001"]["phase"] == "coordinator"
        assert by_id["wf-000002"]["heartbeat_age_s"] == pytest.approx(23.0, abs=0.5)

    def test_parked_and_wake_facts_from_terminal(self, synthetic: dict[str, Any]) -> None:
        payload = FleetStateView(make_config(synthetic)).lines()
        by_id = {line["folder_id"]: line for line in payload["lines"]}
        assert by_id["wf-000001"]["parked"] is True
        assert by_id["wf-000001"]["wake_facts"]["waiting_on"] == "decision"
        assert by_id["wf-000001"]["wake_facts"]["reason"] == "need human"
        assert by_id["wf-000002"]["parked"] is False
        assert by_id["wf-000002"]["terminal"] == "done"
        assert by_id["wf-000002"]["wake_facts"]["waiting_on"] == "none"

    def test_generation_comes_from_roster_or_stall_state(self, synthetic: dict[str, Any]) -> None:
        payload = FleetStateView(make_config(synthetic)).lines()
        for line in payload["lines"]:
            assert line["generation"] == 2  # roster generation

    def test_missing_artifact_degrades_the_entry_not_the_table(
        self, synthetic: dict[str, Any]
    ) -> None:
        run_root = synthetic["run_root"]
        (run_root / "wf-000003").mkdir(parents=True)
        (run_root / "wf-000003" / "heartbeat.json").write_text("{not json", encoding="utf-8")
        write_roster(synthetic["lines_config"], run_root, ["wf-000001", "wf-000003"])
        payload = FleetStateView(make_config(synthetic)).lines()
        by_id = {line["folder_id"]: line for line in payload["lines"]}
        # The bad line is still listed (covered by the roster) but degraded:
        assert "wf-000003" in by_id
        assert by_id["wf-000003"]["heartbeat_age_s"] is None
        assert by_id["wf-000003"]["round"] is None
        assert by_id["wf-000003"]["phase"] is None
        assert by_id["wf-000003"]["terminal"] is None
        # And the good line is untouched.
        assert by_id["wf-000001"]["heartbeat_age_s"] is not None

    def test_unreadable_roster_degrades_to_empty_list(self, tmp_path: Path) -> None:
        lines_config = tmp_path / "missing.json"
        config = FleetStateConfig(
            run_root=tmp_path / "runs",
            lines_config=lines_config,
            bridge_state_dir=tmp_path / "bridge",
        )
        payload = FleetStateView(config).lines()
        assert payload["schema_version"] == LINES_SCHEMA_VERSION
        assert payload["lines"] == []


# --- /v1/decisions ----------------------------------------------------------


class TestDecisionsView:
    def test_returns_schema_version_and_decisions_list(self, synthetic: dict[str, Any]) -> None:
        payload = FleetStateView(make_config(synthetic)).decisions()
        assert payload["schema_version"] == LINES_SCHEMA_VERSION
        assert isinstance(payload["decisions"], list)
        ids = {d["source_message_id"] for d in payload["decisions"]}
        assert ids == {"d-consumed", "d-swallowed", "d-refused"}

    def test_swallowed_entries_carry_reason(self, synthetic: dict[str, Any]) -> None:
        payload = FleetStateView(make_config(synthetic)).decisions()
        by_id = {d["source_message_id"]: d for d in payload["decisions"]}
        assert by_id["d-swallowed"]["state"] == "swallowed"
        assert by_id["d-swallowed"]["reason"] == "no_waiting_owner"
        assert by_id["d-refused"]["state"] == "swallowed"
        assert by_id["d-refused"]["reason"] == "gate_refused"
        assert by_id["d-consumed"]["state"] == "consumed"
        assert by_id["d-consumed"].get("reason") is None

    def test_missing_bridge_db_degrades_to_empty_list(self, tmp_path: Path) -> None:
        config = FleetStateConfig(
            run_root=tmp_path / "runs",
            lines_config=tmp_path / "missing.json",
            bridge_state_dir=tmp_path / "no-bridge",
        )
        payload = FleetStateView(config).decisions()
        assert payload["schema_version"] == LINES_SCHEMA_VERSION
        assert payload["decisions"] == []


# --- /v1/harvestable --------------------------------------------------------


def write_dd_development(
    dd_root: Path,
    development_id: str,
    *,
    head_commit: str,
    terminal: str,
    stage: str = "implement",
    missing_status: bool = False,
    missing_record: bool = False,
) -> None:
    dev_dir = dd_root / development_id
    dev_dir.mkdir(parents=True, exist_ok=True)
    if not missing_record:
        (dev_dir / "record.json").write_text(
            json.dumps({"development_id": development_id, "repo_path": "/tmp/x"}),
            encoding="utf-8",
        )
    if not missing_status:
        (dev_dir / "status.json").write_text(
            json.dumps(
                {
                    "development_id": development_id,
                    "state": "awaiting_gate",
                    "stage": stage,
                    "terminal": terminal,
                    "head_commit": head_commit,
                }
            ),
            encoding="utf-8",
        )


class TestHarvestableView:
    def _config(
        self,
        tmp_path: Path,
        *,
        landed_in_default_branch: Callable[[str], bool] | None = None,
    ) -> FleetStateConfig:
        return FleetStateConfig(
            host="127.0.0.1",
            port=0,  # ephemeral: never collide with the live :7494 read-model
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd",
            lines_config=tmp_path / "missing.json",
            bridge_state_dir=tmp_path / "bridge",
            landed_in_default_branch=landed_in_default_branch,
        )

    def test_empty_dd_root_degrades_to_empty_list(self, tmp_path: Path) -> None:
        payload = FleetStateView(self._config(tmp_path)).harvestable()
        assert payload["schema_version"] == LINES_SCHEMA_VERSION
        assert payload["developments"] == []

    def test_refused_terminal_is_never_harvestable(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        write_dd_development(dd_root, "dev-refused", head_commit="abc123", terminal="refused")
        payload = FleetStateView(self._config(tmp_path)).harvestable()
        assert payload["developments"] == []

    def test_fault_terminal_is_never_harvestable(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        write_dd_development(dd_root, "dev-fault", head_commit="abc123", terminal="fault")
        payload = FleetStateView(self._config(tmp_path)).harvestable()
        assert payload["developments"] == []

    def test_empty_terminal_and_inflight_are_never_harvestable(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        # Empty terminal: never listed.
        write_dd_development(dd_root, "dev-empty", head_commit="abc123", terminal="")
        # In-flight (non-complete terminal): never listed.
        write_dd_development(dd_root, "dev-inflight", head_commit="abc123", terminal="implement")
        payload = FleetStateView(self._config(tmp_path)).harvestable()
        assert payload["developments"] == []

    def test_complete_unharvested_is_listed(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        write_dd_development(dd_root, "dev-unharvested", head_commit="abc123", terminal="complete")
        # Not landed on the default branch -> harvestable.
        view = FleetStateView(
            self._config(tmp_path, landed_in_default_branch=lambda _commit: False)
        )
        payload = view.harvestable()
        assert payload["developments"] == [
            {
                "development_id": "dev-unharvested",
                "head_commit": "abc123",
                "stage": "implement",
                "terminal": "complete",
            }
        ]

    def test_complete_harvested_is_excluded(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        write_dd_development(dd_root, "dev-harvested", head_commit="def456", terminal="complete")
        # Already landed on the default branch -> harvested, not listed.
        view = FleetStateView(self._config(tmp_path, landed_in_default_branch=lambda _commit: True))
        payload = view.harvestable()
        assert payload["developments"] == []

    def test_landing_check_failure_degrades_to_unharvested(self, tmp_path: Path) -> None:
        """A landing query failure must list the complete development (spec:
        读取/查询失败降级,绝不 5xx、绝不崩溃) -- never raise, never 5xx."""
        dd_root = tmp_path / "dd"
        write_dd_development(dd_root, "dev-unverified", head_commit="abc123", terminal="complete")

        def boom(_commit: str) -> bool:
            raise RuntimeError("git unreadable")

        view = FleetStateView(self._config(tmp_path, landed_in_default_branch=boom))
        payload = view.harvestable()
        assert [d["development_id"] for d in payload["developments"]] == ["dev-unverified"]

    def test_missing_status_or_record_degrades_the_entry(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        write_dd_development(dd_root, "dev-ok", head_commit="abc123", terminal="complete")
        write_dd_development(
            dd_root,
            "dev-no-status",
            head_commit="def456",
            terminal="complete",
            missing_status=True,
        )
        write_dd_development(
            dd_root,
            "dev-no-record",
            head_commit="123456",
            terminal="complete",
            missing_record=True,
        )
        view = FleetStateView(
            self._config(tmp_path, landed_in_default_branch=lambda _commit: False)
        )
        payload = view.harvestable()
        assert [d["development_id"] for d in payload["developments"]] == ["dev-ok"]

    def test_harvestable_is_served_over_the_wire(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        write_dd_development(dd_root, "dev-harvest", head_commit="abc123", terminal="complete")
        server = FleetStateHTTPServer(
            self._config(tmp_path, landed_in_default_branch=lambda _commit: False)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/v1/harvestable", timeout=5)
            assert resp.status_code == 200
            body = resp.json()
            assert body["schema_version"] == LINES_SCHEMA_VERSION
            assert body["developments"][0]["development_id"] == "dev-harvest"
        finally:
            server.shutdown()
            server.server_close()


# --- over the wire ----------------------------------------------------------


class TestOverTheWire:
    def _serve(self, config: FleetStateConfig) -> tuple[FleetStateHTTPServer, int]:
        server = FleetStateHTTPServer(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        return server, port

    def test_lines_and_decisions_endpoints_return_schema_version(
        self, synthetic: dict[str, Any]
    ) -> None:
        server, port = self._serve(make_config(synthetic))
        base = f"http://127.0.0.1:{port}"
        try:
            lines = httpx.get(f"{base}/v1/lines", timeout=5)
            assert lines.status_code == 200
            body = lines.json()
            assert "schema_version" in body
            assert isinstance(body["lines"], list)
            assert body["lines"][0]["folder_id"] == "wf-000001"

            decisions = httpx.get(f"{base}/v1/decisions", timeout=5)
            assert decisions.status_code == 200
            dbody = decisions.json()
            assert "schema_version" in dbody
            assert isinstance(dbody["decisions"], list)
        finally:
            server.shutdown()
            server.server_close()

    def test_unknown_path_is_404(self, synthetic: dict[str, Any]) -> None:
        server, port = self._serve(make_config(synthetic))
        base = f"http://127.0.0.1:{port}"
        try:
            resp = httpx.get(f"{base}/v1/nope", timeout=5)
            assert resp.status_code == 404
        finally:
            server.shutdown()
            server.server_close()
