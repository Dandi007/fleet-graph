"""读模型 /v1/decisions 的终结对账回归（spec: 从「快照」改「对账」）。

consumed / swallowed 不再单凭 bridge receipt 瞬时 status：有 dd 目标时，拿单据侧
（events.jsonl ``human_gate success`` / status.json 离开 ``awaiting_gate``）再对一声。

- 阳性：栽「发布后单据立刻推进」→ 单据侧证明消费 → 该裁决必须记 consumed。
- 阴性：真丢（refs 空 / 卡片错配）→ 单据侧无消费证据 → 仍必须 swallowed。
- 对不上（有 dd 目标但单据侧缺失/不可读）→ 显式标注 unreconciled，不静默归类。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import httpx

from fleet_graph.decision_bridge.store import (
    STATUS_NOOP,
    STATUS_REFUSED,
    STATUS_RESUMED,
    BridgeStore,
)
from fleet_graph.state.fleet_state import (
    BASIS_DOCUMENT_AWAITING,
    BASIS_HUMAN_GATE_SUCCESS,
    BASIS_LEFT_AWAITING_GATE,
    BASIS_RECEIPT,
    BASIS_UNRECONCILED,
    FleetStateConfig,
    FleetStateHTTPServer,
    FleetStateView,
)


def _seal(
    bridge_dir: Path,
    *,
    source_message_id: str,
    status: str,
    reason: str,
    target_kind: str = "dd",
    target_id: str = "",
    generation: int = 1,
    seq: int,
) -> None:
    store = BridgeStore(bridge_dir).open()
    try:
        store.seal_terminal(
            {
                "source_message_id": source_message_id,
                "action_key": f"e1:{source_message_id}:{target_kind}:{target_id}:{generation}",
                "target_kind": target_kind,
                "target_id": target_id,
                "generation": generation,
                "question_note_id": "q-1",
                "card_entity_id": "card-1",
                "status": status,
                "reason": reason,
                "source_event": {},
            },
            advance_seq=seq,
        )
    finally:
        store.close()


def _write_status(dd_root: Path, development_id: str, *, state: str) -> None:
    dev = dd_root / development_id
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "status.json").write_text(
        json.dumps({"development_id": development_id, "state": state}), encoding="utf-8"
    )


def _write_gate_event(
    dd_root: Path, development_id: str, *, stage: str = "human_gate", event: str = "success"
) -> None:
    dev = dd_root / development_id
    dev.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"at": "2026-09-02T00:00:00Z", "stage": stage, "event": event, "output_commit": "aaa"},
        ensure_ascii=False,
    )
    with (dev / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _config(tmp_path: Path, dd_root: Path, bridge_dir: Path) -> FleetStateConfig:
    return FleetStateConfig(
        host="127.0.0.1",
        port=0,
        run_root=tmp_path / "runs",
        dd_root=dd_root,
        lines_config=tmp_path / "missing.json",
        bridge_state_dir=bridge_dir,
    )


def _decisions(tmp_path: Path, dd_root: Path, bridge_dir: Path) -> dict[str, Any]:
    return FleetStateView(_config(tmp_path, dd_root, bridge_dir)).decisions()


def _by_id(decisions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["source_message_id"]: d for d in decisions["decisions"]}


class TestPositiveReconciliation:
    """阳性：单据侧证明消费 → 该裁决必须记 consumed（即使 receipt 是 swallowed）。"""

    def test_human_gate_success_in_events_promotes_to_consumed(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        bridge_dir = tmp_path / "bridge"
        _seal(
            bridge_dir,
            source_message_id="d-1",
            status=STATUS_REFUSED,
            reason="owner refused delivery: already advancing",
            target_id="dev-ok",
            seq=1,
        )
        _write_gate_event(dd_root, "dev-ok", stage="human_gate", event="success")

        decision = _by_id(_decisions(tmp_path, dd_root, bridge_dir))["d-1"]

        assert decision["state"] == "consumed"
        assert decision["basis"] == BASIS_HUMAN_GATE_SUCCESS
        assert decision.get("reason") is None

    def test_status_leaving_awaiting_gate_promotes_to_consumed(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        bridge_dir = tmp_path / "bridge"
        _seal(
            bridge_dir,
            source_message_id="d-2",
            status=STATUS_REFUSED,
            reason="owner refused delivery: already advancing",
            target_id="dev-ok",
            seq=1,
        )
        _write_status(dd_root, "dev-ok", state="complete")

        decision = _by_id(_decisions(tmp_path, dd_root, bridge_dir))["d-2"]

        assert decision["state"] == "consumed"
        assert decision["basis"] == BASIS_LEFT_AWAITING_GATE
        assert decision.get("reason") is None

    def test_a_resumed_receipt_stays_consumed_without_a_document(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        bridge_dir = tmp_path / "bridge"
        _seal(
            bridge_dir,
            source_message_id="d-3",
            status=STATUS_RESUMED,
            reason="",
            target_id="dev-3",
            seq=1,
        )

        decision = _by_id(_decisions(tmp_path, dd_root, bridge_dir))["d-3"]

        assert decision["state"] == "consumed"
        assert decision.get("reason") is None


class TestNegativeReconciliation:
    """阴性：真丢（refs 空 / 卡片错配 / 单据侧仍 waiting）→ 仍必须 swallowed。"""

    def test_refs_empty_noop_stays_swallowed(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        bridge_dir = tmp_path / "bridge"
        _seal(
            bridge_dir,
            source_message_id="d-refs",
            status=STATUS_NOOP,
            reason="no waiting owner references this question",
            target_kind="",
            target_id="",
            seq=1,
        )

        decision = _by_id(_decisions(tmp_path, dd_root, bridge_dir))["d-refs"]

        assert decision["state"] == "swallowed"
        assert decision["basis"] == BASIS_RECEIPT
        assert decision["reason"] == "no waiting owner references this question"

    def test_card_mismatch_noop_stays_swallowed(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        bridge_dir = tmp_path / "bridge"
        _seal(
            bridge_dir,
            source_message_id="d-card",
            status=STATUS_NOOP,
            reason="decision card 'card-x' does not match owner card 'card-y'",
            target_kind="",
            target_id="",
            seq=1,
        )

        decision = _by_id(_decisions(tmp_path, dd_root, bridge_dir))["d-card"]

        assert decision["state"] == "swallowed"
        assert decision["basis"] == BASIS_RECEIPT
        assert "does not match owner card" in decision["reason"]

    def test_document_still_awaiting_keeps_the_swallow(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        bridge_dir = tmp_path / "bridge"
        _seal(
            bridge_dir,
            source_message_id="d-wait",
            status=STATUS_REFUSED,
            reason="owner refused delivery: stale",
            target_id="dev-wait",
            seq=1,
        )
        _write_status(dd_root, "dev-wait", state="awaiting_gate")

        decision = _by_id(_decisions(tmp_path, dd_root, bridge_dir))["d-wait"]

        assert decision["state"] == "swallowed"
        assert decision["basis"] == BASIS_DOCUMENT_AWAITING
        assert decision["reason"] == "owner refused delivery: stale"


class TestUnreconciledAnnotation:
    """对不上/不可判定：有 dd 目标但单据侧缺失 → 显式标注，不静默归类。"""

    def test_missing_document_side_is_explicitly_unreconciled(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        bridge_dir = tmp_path / "bridge"
        _seal(
            bridge_dir,
            source_message_id="d-ghost",
            status=STATUS_REFUSED,
            reason="owner refused delivery: gone",
            target_id="dev-ghost",
            seq=1,
        )

        decision = _by_id(_decisions(tmp_path, dd_root, bridge_dir))["d-ghost"]

        assert decision["state"] == "swallowed"
        assert decision["basis"] == BASIS_UNRECONCILED
        assert decision["reason"] == "owner refused delivery: gone"


class TestOverTheWire:
    def test_decisions_endpoint_serves_the_reconciled_state(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        bridge_dir = tmp_path / "bridge"
        _seal(
            bridge_dir,
            source_message_id="d-wire",
            status=STATUS_REFUSED,
            reason="owner refused delivery: already advancing",
            target_id="dev-wire",
            seq=1,
        )
        _write_gate_event(dd_root, "dev-wire", stage="human_gate", event="success")

        server = FleetStateHTTPServer(_config(tmp_path, dd_root, bridge_dir))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/v1/decisions", timeout=5)
            assert resp.status_code == 200
            body = resp.json()
            assert body["schema_version"] == "1"
            [decision] = [d for d in body["decisions"] if d["source_message_id"] == "d-wire"]
            assert decision["state"] == "consumed"
            assert decision["basis"] == BASIS_HUMAN_GATE_SUCCESS
        finally:
            server.shutdown()
            server.server_close()
