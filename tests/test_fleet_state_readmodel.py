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
    "run_id",
    "wake_facts_stale",
    "release_id",
}


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

    def test_matching_run_id_wake_facts_belong_to_the_live_run(
        self, synthetic: dict[str, Any]
    ) -> None:
        """阳性判据（spec §4.2）：terminal.json.run_id == heartbeat.run_id 时，
        该行照常 parked=true 且 wake_facts 属活 run，wake_facts_stale=false。"""
        payload = FleetStateView(make_config(synthetic)).lines()
        by_id = {line["folder_id"]: line for line in payload["lines"]}
        line = by_id["wf-000001"]
        assert line["run_id"] == "run-wf-000001"
        assert line["wake_facts_stale"] is False
        assert line["parked"] is True
        assert line["wake_facts"]["run_id"] == "run-wf-000001"

    def test_stale_run_declaration_is_flagged_not_presented_as_current(
        self, synthetic: dict[str, Any]
    ) -> None:
        """阴性判据（spec §4.1）：线在 run A 声明驻停、run B（新 run_id）起来推进后，
        /v1/lines 不得再把该行呈现为「当前驻停中且不可分辨」——wake_facts_stale=true，
        顶层 run_id 暴露活 run B，声明 run（wake_facts.run_id）与活 run 机械可判。"""
        run_root = synthetic["run_root"]
        write_heartbeat(
            run_root,
            "wf-000001",
            round_no=8,
            phase="coordinator",
            updated_at=iso(1_787_000_200.0),
            run_id="run-B",
        )
        write_terminal(
            run_root,
            "wf-000001",
            terminal="blocked",
            waiting_on="decision",
            reason="stale reason from run A",
            run_id="run-A",
        )
        payload = FleetStateView(make_config(synthetic)).lines()
        by_id = {line["folder_id"]: line for line in payload["lines"]}
        line = by_id["wf-000001"]
        assert line["wake_facts_stale"] is True
        assert line["run_id"] == "run-B"
        assert line["wake_facts"]["run_id"] == "run-A"

    def test_stale_terminal_without_live_run_is_flagged(self, synthetic: dict[str, Any]) -> None:
        """terminal 声明存在但活 heartbeat 缺失/不可比 → 声明 run 无法归于任何活 run，
        同样按过期声明标记（方向 b：保留历史声明，wake_facts_stale=true）。"""
        run_root = synthetic["run_root"]
        (run_root / "wf-000001" / "heartbeat.json").unlink()
        payload = FleetStateView(make_config(synthetic)).lines()
        by_id = {line["folder_id"]: line for line in payload["lines"]}
        line = by_id["wf-000001"]
        assert line["wake_facts_stale"] is True
        assert line["run_id"] is None

    def test_no_terminal_has_no_stale_flag(self, synthetic: dict[str, Any]) -> None:
        """没有 terminal 声明就没有「过期声明」可言：wf-000002 heartbeat 在但未声明
        驻停，wake_facts_stale=false（parked=false 时消费者无歧义）。"""
        run_root = synthetic["run_root"]
        (run_root / "wf-000002" / "terminal.json").unlink()
        payload = FleetStateView(make_config(synthetic)).lines()
        by_id = {line["folder_id"]: line for line in payload["lines"]}
        line = by_id["wf-000002"]
        assert line["wake_facts_stale"] is False
        assert line["run_id"] == "run-wf-000002"

    def test_release_id_is_read_from_the_persisted_heartbeat(
        self, synthetic: dict[str, Any]
    ) -> None:
        """A 类缺口: /v1/lines exposes the release the generation runs, taken
        from the persisted heartbeat value the line process froze at startup."""
        run_root = synthetic["run_root"]
        write_heartbeat(
            run_root,
            "wf-000001",
            round_no=3,
            phase="coordinator",
            updated_at=iso(1_787_000_000.0),
            release_id="20260902-030934-05dec3709ba0",
        )
        payload = FleetStateView(make_config(synthetic)).lines()
        by_id = {line["folder_id"]: line for line in payload["lines"]}
        assert by_id["wf-000001"]["release_id"] == "20260902-030934-05dec3709ba0"

    def test_release_id_degrades_to_null_when_absent_or_bad(
        self, synthetic: dict[str, Any]
    ) -> None:
        """A heartbeat that predates the field, or that never had one, exposes
        null -- never a guess from the deploy `current` symlink."""
        payload = FleetStateView(make_config(synthetic)).lines()
        by_id = {line["folder_id"]: line for line in payload["lines"]}
        assert by_id["wf-000001"]["release_id"] is None
        assert by_id["wf-000002"]["release_id"] is None

    def test_repointing_the_symlink_does_not_change_the_exposed_value(self, tmp_path: Path) -> None:
        """Negative: the read model consumes only the persisted value, so a
        re-pointed deploy `current` symlink mid-generation cannot change what
        the view reports -- the frozen field is the mechanical fact."""
        run_root = tmp_path / "runs"
        write_heartbeat(
            run_root,
            "wf-000001",
            round_no=3,
            phase="coordinator",
            updated_at=iso(1_787_000_000.0),
            release_id="20260902-030934-05dec3709ba0",
        )
        lines_config = tmp_path / "ronin-lines.json"
        write_roster(lines_config, run_root, ["wf-000001"])
        view = FleetStateView(
            FleetStateConfig(
                host="127.0.0.1",
                port=0,
                run_root=run_root,
                dd_root=tmp_path / "dd",
                lines_config=lines_config,
                bridge_state_dir=tmp_path / "bridge",
            )
        )

        assert view.lines()["lines"][0]["release_id"] == "20260902-030934-05dec3709ba0"

        current = tmp_path / "current"
        releases = tmp_path / "releases"
        (releases / "rel-new").mkdir(parents=True)
        current.symlink_to(releases / "rel-new")

        assert view.lines()["lines"][0]["release_id"] == "20260902-030934-05dec3709ba0"

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
    card_entity_id: str = "",
) -> None:
    """Fixture write. ``missing_status`` now means "no authority result.json"
    (M3.1 defect 6: the harvestable view derives from the generation's
    result.json, never the rebuildable status.json cache)."""
    dev_dir = dd_root / development_id
    dev_dir.mkdir(parents=True, exist_ok=True)
    if not missing_record:
        record: dict[str, Any] = {"development_id": development_id, "repo_path": "/tmp/x"}
        if card_entity_id:
            record["card_entity_id"] = card_entity_id
        (dev_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    if not missing_status:
        (dev_dir / "result.json").write_text(
            json.dumps(
                {
                    "development_id": development_id,
                    "stage": stage,
                    "terminal": terminal,
                    "head_commit": head_commit,
                }
            ),
            encoding="utf-8",
        )


class TestHarvestableView:
    def _config(self, tmp_path: Path) -> FleetStateConfig:
        return FleetStateConfig(
            host="127.0.0.1",
            port=0,  # ephemeral: never collide with the live :7494 read-model
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd",
            lines_config=tmp_path / "missing.json",
            bridge_state_dir=tmp_path / "bridge",
        )

    def _baselined(self, config: FleetStateConfig, dd_root: Path) -> FleetStateView:
        """One complete historical dev + one observation, so the first-run
        baseline is adopted (direction B). Returns the view with the baseline
        established; later-added complete devs are then *new* and listed."""
        write_dd_development(dd_root, "dev-hist", head_commit="h0", terminal="complete")
        view = FleetStateView(config)
        assert view.harvestable()["developments"] == []
        return view

    def test_empty_dd_root_degrades_to_empty_list(self, tmp_path: Path) -> None:
        payload = FleetStateView(self._config(tmp_path)).harvestable()
        assert payload["schema_version"] == LINES_SCHEMA_VERSION
        assert payload["developments"] == []

    def test_refused_terminal_is_never_listed(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        view = self._baselined(self._config(tmp_path), dd_root)
        # refused: never listed.
        write_dd_development(dd_root, "dev-refused", head_commit="r1", terminal="refused")
        # a fresh complete is the positive control: only it is listed.
        write_dd_development(dd_root, "dev-new", head_commit="n1", terminal="complete")
        payload = view.harvestable()
        assert [d["development_id"] for d in payload["developments"]] == ["dev-new"]

    def test_fault_terminal_is_never_listed(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        view = self._baselined(self._config(tmp_path), dd_root)
        write_dd_development(dd_root, "dev-fault", head_commit="f1", terminal="fault")
        write_dd_development(dd_root, "dev-new", head_commit="n1", terminal="complete")
        payload = view.harvestable()
        assert [d["development_id"] for d in payload["developments"]] == ["dev-new"]

    def test_complete_with_harvest_receipt_is_excluded(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        config = self._config(tmp_path)
        config.has_harvest_receipt = lambda card: card == "card-reaped"
        write_dd_development(
            dd_root, "dev-hist", head_commit="h0", terminal="complete", card_entity_id="card-hist"
        )
        view = FleetStateView(config)
        assert view.harvestable()["developments"] == []
        # complete + harvest receipt (evidence note / evidence- prefix) -> not listed.
        write_dd_development(
            dd_root,
            "dev-reaped",
            head_commit="r1",
            terminal="complete",
            card_entity_id="card-reaped",
        )
        # complete without receipt is still listed (positive control).
        write_dd_development(
            dd_root,
            "dev-open",
            head_commit="o1",
            terminal="complete",
            card_entity_id="card-open",
        )
        payload = view.harvestable()
        assert [d["development_id"] for d in payload["developments"]] == ["dev-open"]

    def test_complete_without_receipt_is_listed(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        view = self._baselined(self._config(tmp_path), dd_root)
        write_dd_development(dd_root, "dev-new", head_commit="n1", terminal="complete")
        payload = view.harvestable()
        assert [d["development_id"] for d in payload["developments"]] == ["dev-new"]

    def test_first_run_baseline_clears_historical_complete(self, tmp_path: Path) -> None:
        """交付 D.5 首跑基线豁免：147 条历史 complete（无回执）首跑全部出清不在
        列；此后新增一条 complete 无回执 → 该条入列。"""
        dd_root = tmp_path / "dd"
        for i in range(147):
            dev_id = f"dev-hist-{i:03d}"
            write_dd_development(dd_root, dev_id, head_commit=f"h{i}", terminal="complete")
        view = FleetStateView(self._config(tmp_path))
        payload = view.harvestable()
        assert payload["developments"] == []
        write_dd_development(dd_root, "dev-new", head_commit="n1", terminal="complete")
        payload = view.harvestable()
        assert [d["development_id"] for d in payload["developments"]] == ["dev-new"]

    def test_missing_status_or_record_degrades_the_entry(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        view = self._baselined(self._config(tmp_path), dd_root)
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
        payload = view.harvestable()
        assert [d["development_id"] for d in payload["developments"]] == ["dev-ok"]

    def test_harvestable_is_served_over_the_wire(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        config = self._config(tmp_path)
        server = FleetStateHTTPServer(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            # First observation adopts the baseline (historical complete cleared).
            write_dd_development(dd_root, "dev-hist", head_commit="h0", terminal="complete")
            resp = httpx.get(f"http://127.0.0.1:{port}/v1/harvestable", timeout=5)
            assert resp.status_code == 200
            body = resp.json()
            assert body["schema_version"] == LINES_SCHEMA_VERSION
            assert body["developments"] == []
            # A new complete without a receipt is served over the wire.
            write_dd_development(dd_root, "dev-new", head_commit="n1", terminal="complete")
            resp = httpx.get(f"http://127.0.0.1:{port}/v1/harvestable", timeout=5)
            body = resp.json()
            assert [d["development_id"] for d in body["developments"]] == ["dev-new"]
        finally:
            server.shutdown()
            server.server_close()


# --- /v1/enrollments --------------------------------------------------------


class TestEnrollmentsView:
    """goal-driven 入册申请 (spec 交付 B.1): GET /v1/enrollments re-reads the
    goal service's enroll-queue.jsonl on every request, same discipline as
    _read_roster -- bad rows degrade per entry, never a 5xx for the chain."""

    def _config(self, tmp_path: Path, queue_path: Path | None) -> FleetStateConfig:
        return FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd",
            lines_config=tmp_path / "missing.json",
            bridge_state_dir=tmp_path / "bridge",
            enroll_queue_path=queue_path,
        )

    def _queue_file(self, tmp_path: Path, lines: list[dict[str, Any]]) -> Path:
        path = tmp_path / "goal" / "enroll-queue.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n" for line in lines),
            encoding="utf-8",
        )
        return path

    def test_the_default_queue_path_is_the_goal_queue_home(self) -> None:
        """U4 defect 2: the read model defaults to the goal service's own queue
        home (the same /data/fleet-graph/goal/ home goal serve writes), so it
        observes the actual enrollment queue rather than going blind."""
        from fleet_graph.state.fleet_state import DEFAULT_ENROLL_QUEUE

        config = FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=Path("/tmp/fg-runs"),
            dd_root=Path("/tmp/fg-dd"),
            lines_config=Path("/tmp/missing.json"),
            bridge_state_dir=Path("/tmp/bridge"),
        )
        assert config.enroll_queue_path == DEFAULT_ENROLL_QUEUE
        assert str(DEFAULT_ENROLL_QUEUE) == "/data/fleet-graph/goal/enroll-queue.jsonl"

    def test_returns_schema_version_and_enrollments_list(self, tmp_path: Path) -> None:
        queue = self._queue_file(
            tmp_path,
            [
                {
                    "folder_id": "wf-1",
                    "alias": "ronin-fresh",
                    "seat_hint": "opencode-gpt-sol",
                    "max_rounds": 9999,
                    "briefing_version": "v1",
                    "submitted_by": "drill",
                    "submitted_at": "2026-08-31T00:00:00Z",
                    "status": "pending",
                }
            ],
        )
        payload = FleetStateView(self._config(tmp_path, queue)).enrollments()
        assert payload["schema_version"] == LINES_SCHEMA_VERSION
        assert [e["folder_id"] for e in payload["enrollments"]] == ["wf-1"]
        assert payload["enrollments"][0]["status"] == "pending"

    def test_a_missing_queue_degrades_to_empty_list(self, tmp_path: Path) -> None:
        payload = FleetStateView(self._config(tmp_path, None)).enrollments()
        assert payload["schema_version"] == LINES_SCHEMA_VERSION
        assert payload["enrollments"] == []

    def test_bad_rows_degrade_the_entry_not_the_table(self, tmp_path: Path) -> None:
        queue = self._queue_file(
            tmp_path,
            [
                "{not json",
                {"alias": "no-folder-id", "status": "pending"},
                {
                    "folder_id": "wf-good",
                    "alias": "ronin-good",
                    "status": "pending",
                },
            ],
        )
        payload = FleetStateView(self._config(tmp_path, queue)).enrollments()
        assert [e["folder_id"] for e in payload["enrollments"]] == ["wf-good"]

    def test_the_view_is_served_over_the_wire(self, tmp_path: Path) -> None:
        queue = self._queue_file(
            tmp_path,
            [{"folder_id": "wf-1", "alias": "ronin-fresh", "status": "pending"}],
        )
        config = self._config(tmp_path, queue)
        server = FleetStateHTTPServer(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/v1/enrollments", timeout=5)
            assert resp.status_code == 200
            body = resp.json()
            assert body["schema_version"] == LINES_SCHEMA_VERSION
            assert [e["folder_id"] for e in body["enrollments"]] == ["wf-1"]
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
