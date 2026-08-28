"""The E1 decision bridge: durability, strictness, replay, and isolation.

The bridge must be able to answer four questions without a live bus:

- Is the source cursor only advanced with a terminal disposition, and is the
  store fail-closed when SQLite cannot be read or written?
- Does the resolver map a decision strictly -- one owner, or a structured no-op
  for zero/ambiguous/stale/invalid -- and never guess?
- Does a SIGKILL after ``intent_recorded`` replay to exactly one logical
  recovery (owner-side action-key dedup)?
- When the bus is down or SQLite is unwritable, does the bridge recover zero
  owners instead of falling back to a memory cursor?

The isolated *process* drills (resume <5s, kill/restart exactly-once) live in
``scripts/e1_decision_bridge_acceptance.py``; these tests exercise the same
code paths in-process with fakes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.bus.board import DECISION_KIND
from fleet_graph.decision_bridge.bridge import DecisionBridge, DecisionBridgeConfig
from fleet_graph.decision_bridge.owners import OwnerResult, OwnerSource, OwnerTarget
from fleet_graph.decision_bridge.resolver import (
    CATEGORY_AMBIGUOUS,
    CATEGORY_INVALID,
    CATEGORY_NO_WAITING_OWNER,
    CATEGORY_STALE,
    action_key_for,
    resolve_decision,
)
from fleet_graph.decision_bridge.store import (
    STATUS_NOOP,
    STATUS_RESUMED,
    BridgeStore,
    BridgeStoreError,
)

# --- fixtures ---------------------------------------------------------------


class FakeOwner(OwnerSource):
    """In-memory owner: dedups on the action key, records its calls."""

    def __init__(self) -> None:
        self.targets: dict[str, list[OwnerTarget]] = {}
        self.seen: set[str] = set()
        self.calls: list[tuple[OwnerTarget, str]] = []
        self.logical_resumes = 0

    def add(self, target: OwnerTarget) -> None:
        self.targets.setdefault(target.question_note_id, []).append(target)

    def discover(self, question_note_id: str) -> list[OwnerTarget]:
        return list(self.targets.get(question_note_id, []))

    def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
        self.calls.append((target, action_key))
        if action_key in self.seen:
            return OwnerResult("already_resumed", "dedup")
        self.seen.add(action_key)
        self.logical_resumes += 1
        return OwnerResult("resumed", "ok")

    def pretend_resumed(self, action_key: str) -> None:
        """Mark a resume as already performed (a crash landed after the owner
        answered but before the bridge sealed)."""
        self.seen.add(action_key)
        self.logical_resumes += 1


class FakeBus:
    """messages() as the bridge reads it; decisions carry inline refs."""

    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.notes: list[dict[str, Any]] = messages or []

    def messages(self, channel: str, *, limit: int = 100, after_seq: int = 0):
        selected = [m for m in self.notes if int(m["channel_seq"]) > after_seq][:limit]
        head = max((int(m["channel_seq"]) for m in self.notes), default=0)
        return selected, head


def decision(message_id: str, seq: int, *, question: str, card: str, kind: str = DECISION_KIND):
    return {
        "message_id": message_id,
        "channel_seq": seq,
        "kind": kind,
        "payload": {"decision": "APPROVE", "card_entity_id": card},
        "refs": [{"target_entity": question}],
    }


def owner(question: str = "q-1", *, state: str = "awaiting_gate") -> OwnerTarget:
    return OwnerTarget(
        kind="dd",
        id="dev-abc",
        generation=1,
        question_note_id=question,
        card_entity_id="card-1",
        state=state,
    )


def bridge(tmp_path: Path, bus: Any, owner_source: OwnerSource, store: BridgeStore | None = None):
    return DecisionBridge(
        DecisionBridgeConfig(state_dir=tmp_path / "bridge", poll_interval_seconds=0.0),
        bus=bus,
        owner_source=owner_source,
        store=store,
    )


# --- durability -------------------------------------------------------------


class TestStore:
    def test_wal_and_full_synchronous_are_set(self, tmp_path: Path) -> None:
        store = BridgeStore(tmp_path / "db").open()
        conn = store._require_conn()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        store.close()

    def test_intent_does_not_advance_the_cursor(self, tmp_path: Path) -> None:
        store = BridgeStore(tmp_path / "db").open()
        store.record_intent(TOMBSTONE_INTENT)
        assert store.cursor() == 0  # untouched until terminal disposition
        store.close()

    def test_seal_advances_cursor_and_status_atomically(self, tmp_path: Path) -> None:
        store = BridgeStore(tmp_path / "db").open()
        store.seal_terminal(
            {
                "source_message_id": "d-1",
                "action_key": "e1:d-1:dd:dev-abc:1",
                "target_kind": "dd",
                "target_id": "dev-abc",
                "generation": 1,
                "question_note_id": "q-1",
                "card_entity_id": "card-1",
                "status": STATUS_RESUMED,
                "reason": "",
                "source_event": {},
            },
            advance_seq=7,
        )
        assert store.cursor() == 7
        assert store.receipt("d-1")["status"] == STATUS_RESUMED
        store.close()

    def test_action_key_is_a_unique_constraint(self, tmp_path: Path) -> None:
        store = BridgeStore(tmp_path / "db").open()
        receipt = {
            "source_message_id": "d-1",
            "action_key": "e1:d-1:dd:dev-abc:1",
            "target_kind": "dd",
            "target_id": "dev-abc",
            "generation": 1,
            "question_note_id": "q-1",
            "card_entity_id": "card-1",
            "status": STATUS_RESUMED,
            "reason": "",
            "source_event": {},
        }
        store.seal_terminal(receipt, advance_seq=1)
        with pytest.raises(BridgeStoreError):
            # same action key, different source message: the durable unique
            # constraint refuses the second resume.
            store.seal_terminal({**receipt, "source_message_id": "d-2"}, advance_seq=2)
        store.close()

    def test_corrupt_database_fails_closed_on_open(self, tmp_path: Path) -> None:
        db = tmp_path / "db" / "bridge.sqlite3"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"this is not a sqlite database at all")
        with pytest.raises(BridgeStoreError):
            BridgeStore(tmp_path / "db").open()

    def test_cursor_read_fails_closed(self, tmp_path: Path) -> None:
        db = tmp_path / "db" / "bridge.sqlite3"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"garbage bytes, not a database")
        store = BridgeStore(tmp_path / "db")
        store._conn = sqlite3.connect(str(db))
        with pytest.raises(BridgeStoreError):
            store.cursor()


TOMBSTONE_INTENT = {
    "source_message_id": "d-1",
    "action_key": "e1:d-1:dd:dev-abc:1",
    "target_kind": "dd",
    "target_id": "dev-abc",
    "generation": 1,
    "question_note_id": "q-1",
    "card_entity_id": "card-1",
    "reason": "",
    "source_event": {},
}


# --- resolver ---------------------------------------------------------------


class TestResolver:
    def test_action_key_is_exact(self) -> None:
        assert action_key_for("m-1", "dd", "dev-x", 2) == "e1:m-1:dd:dev-x:2"

    def test_a_single_waiting_owner_resolves(self) -> None:
        o = FakeOwner()
        o.add(owner())
        resolution = resolve_decision(decision("d-1", 1, question="q-1", card="card-1"), o)
        assert resolution.ok
        assert resolution.target == owner()

    def test_channel_outside_allowlist_is_invalid(self) -> None:
        o = FakeOwner()
        o.add(owner())
        resolution = resolve_decision(
            decision("d-1", 1, question="q-1", card="card-1"), o, channel_id="board:other"
        )
        assert resolution.category == CATEGORY_INVALID and not resolution.ok

    def test_wrong_kind_is_invalid(self) -> None:
        o = FakeOwner()
        o.add(owner())
        resolution = resolve_decision(
            decision("d-1", 1, question="q-1", card="card-1", kind="work.note.v1"), o
        )
        assert resolution.category == CATEGORY_INVALID

    def test_empty_decision_payload_is_invalid(self) -> None:
        o = FakeOwner()
        msg = decision("d-1", 1, question="q-1", card="card-1")
        msg["payload"]["decision"] = ""
        assert resolve_decision(msg, o).category == CATEGORY_INVALID

    def test_no_refs_is_invalid(self) -> None:
        o = FakeOwner()
        msg = decision("d-1", 1, question="q-1", card="card-1")
        msg["refs"] = []
        assert resolve_decision(msg, o).category == CATEGORY_INVALID

    def test_zero_owners_is_a_noop(self) -> None:
        o = FakeOwner()  # no targets registered
        resolution = resolve_decision(decision("d-1", 1, question="q-1", card="card-1"), o)
        assert resolution.category == CATEGORY_NO_WAITING_OWNER and not resolution.ok

    def test_multiple_owners_is_ambiguous(self) -> None:
        o = FakeOwner()
        o.add(owner(question="q-1"))
        o.add(OwnerTarget("dd", "dev-xyz", 1, "q-1", "card-2", "awaiting_gate"))
        resolution = resolve_decision(decision("d-1", 1, question="q-1", card="card-1"), o)
        assert resolution.category == CATEGORY_AMBIGUOUS

    def test_a_stale_owner_is_a_noop(self) -> None:
        o = FakeOwner()
        o.add(owner(state="complete"))
        resolution = resolve_decision(decision("d-1", 1, question="q-1", card="card-1"), o)
        assert resolution.category == CATEGORY_STALE

    def test_a_mismatched_card_is_stale(self) -> None:
        o = FakeOwner()
        o.add(owner())
        resolution = resolve_decision(decision("d-1", 1, question="q-1", card="card-999"), o)
        assert resolution.category == CATEGORY_STALE


# --- bridge loop ------------------------------------------------------------


class TestBridgeLoop:
    def test_a_fresh_decision_resumes_and_advances(self, tmp_path: Path) -> None:
        o = FakeOwner()
        o.add(owner())
        b = bridge(tmp_path, FakeBus([decision("d-1", 1, question="q-1", card="card-1")]), o)
        record = b.run_once()
        assert record["resumed"] == 1
        assert record["cursor_after"] == 1
        assert o.logical_resumes == 1
        assert o.calls[0][1] == "e1:d-1:dd:dev-abc:1"

    def test_a_duplicate_owner_call_returns_same_logical_success(self, tmp_path: Path) -> None:
        o = FakeOwner()
        o.add(owner())
        b = bridge(tmp_path, FakeBus([decision("d-1", 1, question="q-1", card="card-1")]), o)
        b.run_once()
        first = o.logical_resumes
        # The owner sees the same action key again (a duplicate transport call):
        result = o.resume(owner(), "e1:d-1:dd:dev-abc:1")
        assert result.status == "already_resumed"
        assert o.logical_resumes == first  # no second logical resume

    def test_a_noop_decision_still_advances_the_cursor(self, tmp_path: Path) -> None:
        o = FakeOwner()  # nothing waiting
        b = bridge(tmp_path, FakeBus([decision("d-1", 1, question="q-1", card="card-1")]), o)
        record = b.run_once()
        assert record["resumed"] == 0
        assert o.logical_resumes == 0
        assert record["cursor_after"] == 1
        assert b.store.receipt("d-1")["status"] == STATUS_NOOP

    def test_a_replayed_terminal_receipt_is_skipped(self, tmp_path: Path) -> None:
        o = FakeOwner()
        o.add(owner())
        store = BridgeStore(tmp_path / "bridge").open()
        store.seal_terminal(
            {
                "source_message_id": "d-1",
                "action_key": "e1:d-1:dd:dev-abc:1",
                "target_kind": "dd",
                "target_id": "dev-abc",
                "generation": 1,
                "question_note_id": "q-1",
                "card_entity_id": "card-1",
                "status": STATUS_RESUMED,
                "reason": "",
                "source_event": {},
            },
            advance_seq=1,
        )
        # Simulate the same decision being re-presented (a cursor rewind).
        store._require_conn().execute("UPDATE cursor SET board_seq = 0 WHERE id = 1")
        b = bridge(
            tmp_path, FakeBus([decision("d-1", 1, question="q-1", card="card-1")]), o, store=store
        )
        record = b.run_once()
        assert record["resumed"] == 0
        assert o.calls == []  # the terminal receipt suppresses any re-call
        assert record["cursor_after"] == 1

    def test_crash_after_intent_replays_to_exactly_one_resume(self, tmp_path: Path) -> None:
        o = FakeOwner()
        o.add(owner())
        o.pretend_resumed("e1:d-1:dd:dev-abc:1")  # owner already answered, bridge died pre-seal
        store = BridgeStore(tmp_path / "bridge").open()
        store.record_intent(TOMBSTONE_INTENT)  # intent persisted, cursor untouched
        store.close()

        b = bridge(
            tmp_path,
            FakeBus([decision("d-1", 1, question="q-1", card="card-1")]),
            o,
            store=BridgeStore(tmp_path / "bridge").open(),
        )
        record = b.run_once()
        assert record["resumed"] == 0  # replay is not a *new* logical resume
        assert o.logical_resumes == 1  # still exactly one
        assert record["cursor_after"] == 1
        assert b.store.receipt("d-1")["status"] == STATUS_RESUMED
        # exactly one receipt for the single decision, and it is terminal
        assert len(b.store.receipts()) == 1

    def test_bus_unavailable_recovers_nothing(self, tmp_path: Path) -> None:
        o = FakeOwner()
        o.add(owner())

        class DownBus:
            def messages(self, *a: Any, **kw: Any):
                raise RuntimeError("bus down")

        b = bridge(tmp_path, DownBus(), o)
        record = b.run_once()
        assert record["resumed"] == 0
        assert o.logical_resumes == 0
        assert record["bus"] == "error"

    def test_no_bus_object_recovers_nothing(self, tmp_path: Path) -> None:
        o = FakeOwner()
        b = bridge(tmp_path, None, o)
        record = b.run_once()
        assert record["resumed"] == 0
        assert record["bus"] == "unavailable"

    def test_unwritable_sqlite_recovers_nothing(self, tmp_path: Path) -> None:
        o = FakeOwner()
        o.add(owner())
        db = tmp_path / "bridge" / "bridge.sqlite3"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"corrupt, not a database")
        b = bridge(tmp_path, FakeBus([decision("d-1", 1, question="q-1", card="card-1")]), o)
        record = b.run_once()
        assert record["resumed"] == 0
        assert o.logical_resumes == 0
        assert record.get("error")  # fail closed, recorded


# --- the old polling fallback survives --------------------------------------


class TestOldPollingFixtureSurvives:
    def test_supervisor_observer_e1_still_emits(self) -> None:
        """The decision bridge is a new, independent service; it must not have
        disabled the observation-only E1 board scan the supervisor already
        shipped. Pinned here so a regression is loud, not a silent fixture
        removal."""
        from fleet_graph.supervise.events import board_question_event, validate_event

        event = validate_event(board_question_event("q-1", "card-1").as_dict())
        assert event.type == "board_question"
        assert event.key == "e1-q-1"


# --- the shipped unit -------------------------------------------------------


class TestDeployUnit:
    UNIT = (
        Path(__file__).resolve().parent.parent
        / "deploy"
        / "systemd"
        / "fleet-graph-decision-bridge.service"
    )

    def _text(self) -> str:
        return self.UNIT.read_text(encoding="utf-8")

    def _directives(self) -> list[str]:
        """Non-comment, non-empty lines -- the effective unit, not the prose."""
        return [
            line
            for line in self._text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_exec_start_parses_as_the_cli_subcommand(self) -> None:
        from fleet_graph.cli import build_parser

        joined = self._text().replace("\\\n", " ")
        argv = []
        for line in joined.splitlines():
            if line.startswith("ExecStart="):
                argv = line[len("ExecStart=") :].split()
        assert argv[0].endswith("fleet-graph"), argv
        parsed = build_parser().parse_args(argv[1:])
        assert parsed.func is not None

    def test_restart_policy_is_on_failure_not_always(self) -> None:
        directives = self._directives()
        assert "Restart=on-failure" in directives
        assert "Restart=always" not in directives

    def test_no_process_linkage_to_the_scheduler(self) -> None:
        directives = self._directives()
        assert not any(line.startswith("Requires=") for line in directives)
        assert not any(line.startswith("PartOf=") for line in directives)
        assert "fleet-graphd.service" not in directives

    def test_no_decision_publish_credential(self) -> None:
        directives = self._directives()
        assert not any("FLEET_GRAPH_DECISION_TOKEN_FILE" in line for line in directives)
        for line in directives:
            if line.startswith("Environment=") or line.startswith("EnvironmentFile="):
                assert "TOKEN" not in line.upper(), line
                assert "KEY" not in line.upper(), line

    def test_uses_its_own_env_file_not_the_shared_one(self) -> None:
        directives = self._directives()
        assert any("decision-bridge.env" in line for line in directives)
        assert "EnvironmentFile=-%h/.config/fleet-graph/env" not in directives
