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

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.bus.board import DECISION_KIND
from fleet_graph.decision_bridge.bridge import DecisionBridge, DecisionBridgeConfig
from fleet_graph.decision_bridge.owners import (
    OWNER_KIND_LINE,
    RESUME_ALREADY_RESUMED,
    RESUME_REFUSED,
    RESUME_RESUMED,
    CompositeOwnerSource,
    DdOwnerSource,
    LineOwnerSource,
    OwnerResult,
    OwnerSource,
    OwnerTarget,
)
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

    def discover_all(self) -> list[OwnerTarget]:
        return [target for targets in self.targets.values() for target in targets]

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
    """messages() + refs_to() as the real bus serves them: no inline refs.

    The real bus indexes a forward reference on its target entity, so a decision
    references a question through the reverse ``refs_to`` surface (``GET
    /v1/entities/<question>/refs``), never through an inline ``refs`` field on
    the served message.
    """

    def __init__(
        self,
        messages: list[dict[str, Any]] | None = None,
        *,
        refs: dict[str, list[str]] | None = None,
    ) -> None:
        self.notes: list[dict[str, Any]] = messages or []
        self.refs: dict[str, list[str]] = dict(refs or {})

    def messages(self, channel: str, *, limit: int = 100, after_seq: int = 0):
        selected = [m for m in self.notes if int(m["channel_seq"]) > after_seq][:limit]
        head = max((int(m["channel_seq"]) for m in self.notes), default=0)
        return selected, head

    def refs_to(self, entity_id: str) -> list[dict[str, Any]]:
        return [
            {"message_id": mid, "target_entity": entity_id} for mid in self.refs.get(entity_id, [])
        ]


def decision(message_id: str, seq: int, *, card: str, kind: str = DECISION_KIND):
    return {
        "message_id": message_id,
        "channel_seq": seq,
        "kind": kind,
        "payload": {"decision": "APPROVE", "card_entity_id": card},
    }


def refs_to(refs: dict[str, list[str]]):
    """A ``refs_to`` seam whose response shape matches the real endpoint."""

    def _refs_to(entity_id: str) -> list[dict[str, Any]]:
        return [{"message_id": mid, "target_entity": entity_id} for mid in refs.get(entity_id, [])]

    return _refs_to


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

    def test_seal_cursor_is_monotonic_never_backwards(self, tmp_path: Path) -> None:
        """A later seal must not move the cursor back past a position an earlier
        seal already advanced it over -- otherwise a skipped/mis-sealed decision
        would be silently re-skipped. ``seal_terminal`` and ``advance_cursor``
        both use ``MAX``, so the cursor is monotonic."""
        store = BridgeStore(tmp_path / "db").open()
        store.seal_terminal(_receipt("d-1", 1), advance_seq=5)
        assert store.cursor() == 5
        # A decision with a *lower* seq sealing after a higher one must not
        # rewind the cursor.
        store.seal_terminal(_receipt("d-0", 0), advance_seq=3)
        assert store.cursor() == 5
        store.close()


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


def _receipt(message_id: str, seq: int) -> dict[str, Any]:
    return {
        "source_message_id": message_id,
        "action_key": f"e1:{message_id}:dd:dev-abc:1",
        "target_kind": "dd",
        "target_id": "dev-abc",
        "generation": 1,
        "question_note_id": "q-1",
        "card_entity_id": "card-1",
        "status": STATUS_RESUMED,
        "reason": "",
        "source_event": {"message_id": message_id, "channel_seq": seq},
    }


# --- resolver ---------------------------------------------------------------


class TestResolver:
    def test_action_key_is_exact(self) -> None:
        assert action_key_for("m-1", "dd", "dev-x", 2) == "e1:m-1:dd:dev-x:2"

    def test_a_single_waiting_owner_resolves(self) -> None:
        o = FakeOwner()
        o.add(owner())
        resolution = resolve_decision(
            decision("d-1", 1, card="card-1"), o, refs_to=refs_to({"q-1": ["d-1"]})
        )
        assert resolution.ok
        assert resolution.target == owner()

    def test_refs_are_resolved_through_the_real_endpoint(self) -> None:
        """The question comes from the reverse-refs endpoint, not an inline
        ``refs`` field: a decision referencing a different question than the
        one an owner is waiting on resolves nobody."""
        o = FakeOwner()
        o.add(owner())  # waiting on q-1
        resolution = resolve_decision(
            decision("d-1", 1, card="card-1"), o, refs_to=refs_to({"q-2": ["d-1"]})
        )
        assert resolution.category == CATEGORY_NO_WAITING_OWNER and not resolution.ok

    def test_inline_question_note_id_is_not_trusted(self) -> None:
        """A decision carrying ``payload.question_note_id`` inline does not
        resolve an owner on its own: the question is established only through
        the reverse-refs endpoint. An inline field, even matching a waiting
        owner's question, resolves nothing when the refs endpoint does not name
        this decision for that question."""
        o = FakeOwner()
        o.add(owner())  # waiting on q-1
        msg = decision("d-1", 1, card="card-1")
        msg["payload"]["question_note_id"] = "q-1"
        resolution = resolve_decision(msg, o, refs_to=refs_to({}))
        assert resolution.category == CATEGORY_NO_WAITING_OWNER and not resolution.ok

    def test_channel_outside_allowlist_is_invalid(self) -> None:
        o = FakeOwner()
        o.add(owner())
        resolution = resolve_decision(
            decision("d-1", 1, card="card-1"), o, channel_id="board:other"
        )
        assert resolution.category == CATEGORY_INVALID and not resolution.ok

    def test_wrong_kind_is_invalid(self) -> None:
        o = FakeOwner()
        o.add(owner())
        resolution = resolve_decision(decision("d-1", 1, card="card-1", kind="work.note.v1"), o)
        assert resolution.category == CATEGORY_INVALID

    def test_empty_decision_payload_is_invalid(self) -> None:
        o = FakeOwner()
        msg = decision("d-1", 1, card="card-1")
        msg["payload"]["decision"] = ""
        assert resolve_decision(msg, o).category == CATEGORY_INVALID

    def test_a_decision_referencing_no_question_is_a_noop(self) -> None:
        o = FakeOwner()
        o.add(owner())
        resolution = resolve_decision(decision("d-1", 1, card="card-1"), o, refs_to=refs_to({}))
        assert resolution.category == CATEGORY_NO_WAITING_OWNER and not resolution.ok

    def test_zero_owners_is_a_noop(self) -> None:
        o = FakeOwner()  # no targets registered
        resolution = resolve_decision(
            decision("d-1", 1, card="card-1"), o, refs_to=refs_to({"q-1": ["d-1"]})
        )
        assert resolution.category == CATEGORY_NO_WAITING_OWNER and not resolution.ok

    def test_multiple_owners_is_ambiguous(self) -> None:
        o = FakeOwner()
        o.add(owner(question="q-1"))
        o.add(OwnerTarget("dd", "dev-xyz", 1, "q-1", "card-2", "awaiting_gate"))
        resolution = resolve_decision(
            decision("d-1", 1, card="card-1"), o, refs_to=refs_to({"q-1": ["d-1"]})
        )
        assert resolution.category == CATEGORY_AMBIGUOUS

    def test_a_stale_owner_is_a_noop(self) -> None:
        o = FakeOwner()
        o.add(owner(state="complete"))
        resolution = resolve_decision(
            decision("d-1", 1, card="card-1"), o, refs_to=refs_to({"q-1": ["d-1"]})
        )
        assert resolution.category == CATEGORY_STALE

    def test_a_mismatched_card_is_stale(self) -> None:
        o = FakeOwner()
        o.add(owner())
        resolution = resolve_decision(
            decision("d-1", 1, card="card-999"), o, refs_to=refs_to({"q-1": ["d-1"]})
        )
        assert resolution.category == CATEGORY_STALE

    def test_a_discovery_failure_is_a_structured_noop(self) -> None:
        """A control-plane read failure during discovery must be a *structured*
        no-op (discovery failures named in the reason), not silently read as
        "no waiting owner" and not a crash."""

        class ExplodingOwner(OwnerSource):
            def discover(self, question_note_id: str) -> list[OwnerTarget]:
                raise RuntimeError("control plane down")

            def discover_all(self) -> list[OwnerTarget]:
                raise RuntimeError("control plane down")

            def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
                return OwnerResult("refused", "unreachable")

        resolution = resolve_decision(decision("d-1", 1, card="card-1"), ExplodingOwner())
        assert resolution.category == CATEGORY_NO_WAITING_OWNER
        assert "discovery failures" in resolution.reason
        assert "control plane down" in resolution.reason

    def test_dd_owner_discover_re_raises_control_plane_failure(self, tmp_path: Path) -> None:
        """DdOwnerSource must not swallow a control-plane read failure: it is a
        discovered fact for the resolver to record, not "zero owners"."""
        source = DdOwnerSource(tmp_path / "dd")

        def failing() -> Any:
            raise RuntimeError("control plane down")

        source._control_plane = failing  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            source.discover("q-1")


# --- bridge loop ------------------------------------------------------------


class TestBridgeLoop:
    def test_a_fresh_decision_resumes_and_advances(self, tmp_path: Path) -> None:
        o = FakeOwner()
        o.add(owner())
        b = bridge(tmp_path, FakeBus([decision("d-1", 1, card="card-1")], refs={"q-1": ["d-1"]}), o)
        record = b.run_once()
        assert record["resumed"] == 1
        assert record["cursor_after"] == 1
        assert o.logical_resumes == 1
        assert o.calls[0][1] == "e1:d-1:dd:dev-abc:1"

    def test_a_duplicate_owner_call_returns_same_logical_success(self, tmp_path: Path) -> None:
        o = FakeOwner()
        o.add(owner())
        b = bridge(tmp_path, FakeBus([decision("d-1", 1, card="card-1")], refs={"q-1": ["d-1"]}), o)
        b.run_once()
        first = o.logical_resumes
        # The owner sees the same action key again (a duplicate transport call):
        result = o.resume(owner(), "e1:d-1:dd:dev-abc:1")
        assert result.status == "already_resumed"
        assert o.logical_resumes == first  # no second logical resume

    def test_a_noop_decision_still_advances_the_cursor(self, tmp_path: Path) -> None:
        o = FakeOwner()  # nothing waiting
        b = bridge(tmp_path, FakeBus([decision("d-1", 1, card="card-1")], refs={"q-1": ["d-1"]}), o)
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
            tmp_path,
            FakeBus([decision("d-1", 1, card="card-1")], refs={"q-1": ["d-1"]}),
            o,
            store=store,
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
            FakeBus([decision("d-1", 1, card="card-1")], refs={"q-1": ["d-1"]}),
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
        b = bridge(tmp_path, FakeBus([decision("d-1", 1, card="card-1")], refs={"q-1": ["d-1"]}), o)
        record = b.run_once()
        assert record["resumed"] == 0
        assert o.logical_resumes == 0
        assert record.get("error")  # fail closed, recorded

    def test_a_store_fault_mid_cycle_never_raises_and_stops(self, tmp_path: Path) -> None:
        """A durability fault deciding one message must a) not escape
        ``run_once`` (its contract is "never raises") and b) stop the loop, so a
        later message's seal cannot advance the cursor past the unprocessed one."""
        o = FakeOwner()
        o.add(owner())
        store = BridgeStore(tmp_path / "bridge").open()

        def boom(receipt: dict[str, Any]) -> None:
            raise BridgeStoreError("disk full")

        store.record_intent = boom  # type: ignore[method-assign]
        messages = [
            decision("d-1", 1, card="card-1"),
            decision("d-2", 2, card="card-1"),
        ]
        b = bridge(tmp_path, FakeBus(messages, refs={"q-1": ["d-1", "d-2"]}), o, store=store)
        record = b.run_once()  # must not raise
        assert record["resumed"] == 0
        assert o.logical_resumes == 0
        assert record.get("error")
        # The second message was never attempted, so the cursor did not advance
        # past the still-unprocessed first decision.
        assert record["cursor_after"] == 0

    def test_a_receipt_read_fault_mid_cycle_stops_the_loop(self, tmp_path: Path) -> None:
        """The sibling of ``test_a_store_fault_mid_cycle_never_raises_and_stops``:
        a durability fault *reading* the receipt for the current decision must
        also stop the loop, so a later message's seal cannot advance the cursor
        past the decision whose receipt could not be read. (A receipt read that
        returns instead of raising would let the next message seal seq N+1 and
        drop this human verdict forever.)"""
        o = FakeOwner()
        o.add(owner())
        store = BridgeStore(tmp_path / "bridge").open()

        def boom(source_message_id: str) -> dict[str, Any] | None:
            raise BridgeStoreError("receipt unreadable: database is locked")

        store.receipt = boom  # type: ignore[method-assign]
        messages = [
            decision("d-1", 1, card="card-1"),
            decision("d-2", 2, card="card-1"),
        ]
        b = bridge(tmp_path, FakeBus(messages, refs={"q-1": ["d-1", "d-2"]}), o, store=store)
        record = b.run_once()  # must not raise
        assert record["resumed"] == 0
        assert o.logical_resumes == 0
        # The first decision's receipt could not be read, so the bridge must
        # refuse to resume and, crucially, must not advance the cursor past it.
        assert record.get("error")
        assert record["cursor_after"] == 0


# --- owner-side action-key dedup (spec item 4) ------------------------------


class TestDdOwnerSideDedup:
    """The production dd path enforces the same durable (action_key,
    generation) uniqueness the bridge's own receipts index enforces, so a
    SIGKILL replay of a resume cannot launch a second recovery.

    These drive the *real* ``DdControlPlane.gate`` against a recording launcher
    -- not a mock gate -- which is the path the acceptance process drill's fake
    owner cannot reach.
    """

    def _plane(self, tmp_path: Path) -> tuple[Any, Any]:
        from fleet_graph.dd.control_plane import DdControlPlane
        from fleet_graph.scheduler.launcher import LaunchResult

        class RecordingLauncher:
            dry_run = False

            def __init__(self) -> None:
                self.specs: list[Any] = []

            def launch(self, spec: Any) -> Any:
                self.specs.append(spec)
                return LaunchResult(spec.unit_name, True, "recorded")

        launcher = RecordingLauncher()
        binding = tmp_path / "plugin-binding.json"
        binding.write_text('{"plugin_producer": {}}', encoding="utf-8")
        plane = DdControlPlane(
            root=tmp_path / "dd",
            plugin_binding=binding,
            worktree_roots=(str(tmp_path),),
            working_directory=str(tmp_path),
            executable="/usr/local/bin/fleet-graph",
            launcher=launcher,
            unit_probe=lambda unit: False,
            board_factory=lambda: None,
            clock=lambda: 1_700_000_000.0,
        )
        return plane, launcher

    def _suspended(self, plane: Any, dev: str, tmp_path: Path) -> None:
        from fleet_graph.dd.control_plane import (
            CHECKPOINT_FILE,
            LAUNCHES_FILE,
            RECORD_FILE,
            RESULT_FILE,
        )

        dev_root = plane.root / dev
        dev_root.mkdir(parents=True, exist_ok=True)
        (dev_root / RECORD_FILE).write_text(
            json.dumps(
                {
                    "development_id": dev,
                    "generation": 1,
                    "repo_path": str(tmp_path),
                    "remote_url": "file:///dev/null",
                    "remote_ref": "refs/heads/dd/dev-abc",
                    "root_handoff_digest": "sha256:root",
                    "target_base_commit": "0" * 40,
                    "plugin_binding_path": str(tmp_path / "plugin-binding.json"),
                    "card_entity_id": "card-1",
                }
            ),
            encoding="utf-8",
        )
        (dev_root / RESULT_FILE).write_text(
            json.dumps(
                {
                    "development_id": dev,
                    "terminal": None,
                    "awaiting": {"question_note_id": "q-1", "card_entity_id": "card-1"},
                }
            ),
            encoding="utf-8",
        )
        (dev_root / CHECKPOINT_FILE).touch()
        # Production shape: an awaiting_gate development always reached the gate
        # through its *fresh* generation-N launch. The claim/act-window guard
        # must not mistake that fresh entry for a completed resume, or it reads
        # every interrupted claim as already_resumed.
        (dev_root / LAUNCHES_FILE).write_text(
            json.dumps(
                {
                    "seq": 1,
                    "unit": "fleet-graphd-dd-dev-abc.service",
                    "mode": "fresh",
                    "generation": 1,
                    "at": "2026-08-28T00:00:00Z",
                    "started": True,
                    "detail": "recorded",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_a_duplicate_resume_claim_does_not_launch_again(self, tmp_path: Path) -> None:
        plane, launcher = self._plane(tmp_path)
        self._suspended(plane, "dev-abc", tmp_path)
        action_key = "e1:d-1:dd:dev-abc:1"

        first = plane.gate("dev-abc", resume=True, action_key=action_key)
        assert first["resume"]["mode"] == "resume"
        assert not first.get("already_resumed")

        second = plane.gate("dev-abc", resume=True, action_key=action_key)
        assert second["resume"]["already_resumed"] is True
        assert second["already_resumed"] is True

        assert len(launcher.specs) == 1  # one launch, not two

    def test_distinct_action_keys_still_resume(self, tmp_path: Path) -> None:
        plane, launcher = self._plane(tmp_path)
        self._suspended(plane, "dev-abc", tmp_path)
        plane.gate("dev-abc", resume=True, action_key="e1:d-1:dd:dev-abc:1")
        third = plane.gate("dev-abc", resume=True, action_key="e1:d-2:dd:dev-abc:1")
        assert "already_resumed" not in third.get("resume", {})
        assert len(launcher.specs) == 2

    def test_dd_owner_resume_passes_the_action_key_through(self, tmp_path: Path) -> None:
        plane, _ = self._plane(tmp_path)
        self._suspended(plane, "dev-abc", tmp_path)
        source = DdOwnerSource(tmp_path / "dd")
        target = OwnerTarget("dd", "dev-abc", 1, "q-1", "card-1", "awaiting_gate")
        action_key = "e1:d-1:dd:dev-abc:1"

        assert source.resume(target, action_key).status == RESUME_RESUMED
        replay = source.resume(target, action_key)
        assert replay.status == RESUME_ALREADY_RESUMED

    def test_an_interrupted_claim_without_launch_is_completed(self, tmp_path: Path) -> None:
        """A crash in the claim/act window leaves a claim file but no launch
        entry; the gate must carry out the recovery then instead of reporting
        ``already_resumed`` for a recovery that never ran."""
        plane, launcher = self._plane(tmp_path)
        self._suspended(plane, "dev-abc", tmp_path)
        action_key = "e1:d-1:dd:dev-abc:1"

        # The durable claim exists, but no launch was ever recorded.
        assert plane._claim_resume_action("dev-abc", 1, action_key) is True

        result = plane.gate("dev-abc", resume=True, action_key=action_key)
        assert not result.get("already_resumed")
        assert result["resume"]["mode"] == "resume"
        assert len(launcher.specs) == 1  # the interrupted recovery completed once


class TestLineOwner:
    """The line resume owner: discovery over the scheduler's parked stall-state
    and recovery through the registered control entry, with durable dedup."""

    def _parked(self, run_root: Path, *, question: str = "q-1") -> Path:
        stall = run_root / ".scheduler" / "wf-1.json"
        stall.parent.mkdir(parents=True, exist_ok=True)
        stall.write_text(
            json.dumps(
                {
                    "generation": 2,
                    "board_card_entity_id": "card-1",
                    "board_question_note_id": question,
                    "parked_run_id": "run-1",
                    "parked_at": 1_700_000_000.0,
                }
            ),
            encoding="utf-8",
        )
        return stall

    def test_discover_maps_a_parked_line(self, tmp_path: Path) -> None:
        run_root = tmp_path / "runs"
        self._parked(run_root)
        source = LineOwnerSource(run_root, ["wf-1"])
        targets = source.discover("q-1")
        assert len(targets) == 1
        target = targets[0]
        assert target.kind == OWNER_KIND_LINE
        assert target.id == "wf-1"
        assert target.generation == 2
        assert target.card_entity_id == "card-1"
        assert target.state == "parked"

    def test_discover_ignores_non_parked_or_other_question(self, tmp_path: Path) -> None:
        run_root = tmp_path / "runs"
        self._parked(run_root)
        source = LineOwnerSource(run_root, ["wf-1"])
        assert source.discover("q-other") == []
        assert source.discover("not-parked-question") == []

    def test_resolve_returns_ok_for_a_parked_line_question(self, tmp_path: Path) -> None:
        """The swallowed-approve regression: a line parked with its
        ``board_question_note_id`` persisted must resolve a decision that
        references that question. Before the fix, the scheduler dropped the
        question id on the next accounted terminal, discovery read a null
        question, and the bridge sealed the human approve as
        ``no_waiting_owner``."""
        run_root = tmp_path / "runs"
        self._parked(run_root, question="q-1")
        source = LineOwnerSource(run_root, ["wf-1"])

        resolution = resolve_decision(
            decision("d-1", 1, card="card-1"), source, refs_to=refs_to({"q-1": ["d-1"]})
        )

        assert resolution.ok
        assert resolution.target is not None
        assert resolution.target.kind == OWNER_KIND_LINE
        assert resolution.target.id == "wf-1"
        assert resolution.question_note_id == "q-1"
        assert resolution.card_entity_id == "card-1"

    def test_discover_accepts_roster_dict_entries(self, tmp_path: Path) -> None:
        """Production loads the roster from config/ronin-lines.json, whose lines
        are dicts -- the owner must read ``folder_id`` from a dict, not treat the
        dict itself as the id."""
        run_root = tmp_path / "runs"
        self._parked(run_root)
        source = LineOwnerSource(run_root, [{"folder_id": "wf-1", "seat": "s", "generation": 2}])
        targets = source.discover("q-1")
        assert [t.id for t in targets] == ["wf-1"]

    def test_resume_wakes_once_and_dedups(self, tmp_path: Path) -> None:
        run_root = tmp_path / "runs"
        stall = self._parked(run_root)
        source = LineOwnerSource(run_root, ["wf-1"])
        target = source.discover("q-1")[0]
        action_key = "e1:d-9:line:wf-1:2"

        first = source.resume(target, action_key)
        assert first.status == RESUME_RESUMED

        after = json.loads(stall.read_text(encoding="utf-8"))
        assert after["parked_run_id"] is None
        assert after["parked_at"] is None

        replay = source.resume(target, action_key)
        assert replay.status == RESUME_ALREADY_RESUMED

    def test_resume_refuses_a_stale_question(self, tmp_path: Path) -> None:
        run_root = tmp_path / "runs"
        self._parked(run_root, question="q-1")
        source = LineOwnerSource(run_root, ["wf-1"])
        stale = OwnerTarget(OWNER_KIND_LINE, "wf-1", 2, "q-other", "card-1", "parked")
        assert source.resume(stale, "e1:d-9:line:wf-1:2").status == RESUME_REFUSED

    def test_resume_completes_an_interrupted_claim(self, tmp_path: Path) -> None:
        """A crash between the durable claim and the wake leaves a claim file
        while the line is still parked; a replay must wake it then rather than
        report ``already_resumed`` for a recovery that never happened."""
        run_root = tmp_path / "runs"
        stall = self._parked(run_root)
        source = LineOwnerSource(run_root, ["wf-1"])
        target = source.discover("q-1")[0]
        action_key = "e1:d-9:line:wf-1:2"

        # The durable claim exists, but the line is still parked (no wake ran).
        assert source._claim("wf-1", target.generation, action_key) is True

        result = source.resume(target, action_key)
        assert result.status == RESUME_RESUMED
        after = json.loads(stall.read_text(encoding="utf-8"))
        assert after["parked_run_id"] is None
        assert after["parked_at"] is None


class TestCompositeOwner:
    def test_discover_fans_out_and_resume_routes_by_kind(self, tmp_path: Path) -> None:
        run_root = tmp_path / "runs"
        stall = run_root / ".scheduler" / "wf-1.json"
        stall.parent.mkdir(parents=True, exist_ok=True)
        stall.write_text(
            json.dumps(
                {
                    "generation": 1,
                    "board_card_entity_id": "card-1",
                    "board_question_note_id": "q-1",
                    "parked_run_id": "run-1",
                    "parked_at": 1_700_000_000.0,
                }
            ),
            encoding="utf-8",
        )
        line = LineOwnerSource(run_root, ["wf-1"])

        class NoopDd(OwnerSource):
            def discover(self, question_note_id: str) -> list[OwnerTarget]:
                return []

            def discover_all(self) -> list[OwnerTarget]:
                return []

            def resume(self, target: OwnerTarget, action_key: str) -> OwnerResult:
                return OwnerResult(RESUME_RESUMED, "ok")

        dd = NoopDd()
        composite = CompositeOwnerSource([dd, line], kinds={"dd": dd, "line": line})
        targets = composite.discover("q-1")
        assert [t.kind for t in targets] == ["line"]
        result = composite.resume(targets[0], "e1:d-9:line:wf-1:1")
        assert result.status == RESUME_RESUMED


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

    def test_lines_config_points_at_the_installed_roster(self) -> None:
        """The roster's declared home is the repo `config/ronin-lines.json`,
        installed to ``/data/apps/fleet-graph/current/config/ronin-lines.json``
        -- the same path fleet-graphd.service reads. The unit must not point at
        a path nothing installs, which would turn the bridge's first tick into a
        permanent crash loop under Restart=on-failure."""
        joined = self._text().replace("\\\n", " ")
        argv = []
        for line in joined.splitlines():
            if line.startswith("ExecStart="):
                argv = line[len("ExecStart=") :].split()
        assert "--lines-config" in argv, argv
        value = argv[argv.index("--lines-config") + 1]
        assert value == "/data/apps/fleet-graph/current/config/ronin-lines.json", argv


class TestLineRosterFailSoft:
    """A missing or malformed goal-line roster is a recorded degradation, not a
    startup crash: the bridge still starts and recovers dd developments, and the
    preserved 60s poller keeps covering lines until the roster is fixed."""

    def test_a_missing_roster_yields_no_line_owners(self, tmp_path: Path) -> None:
        from fleet_graph.cli import _load_line_roster

        owners, run_root = _load_line_roster(str(tmp_path / "nope.json"))
        assert owners == []
        assert str(run_root) == "/data/fleet-graph/runs"

    def test_a_malformed_roster_yields_no_line_owners(self, tmp_path: Path) -> None:
        from fleet_graph.cli import _load_line_roster

        malformed = tmp_path / "ronin-lines.json"
        malformed.write_text("{not json", encoding="utf-8")
        owners, run_root = _load_line_roster(str(malformed))
        assert owners == []
        assert str(run_root) == "/data/fleet-graph/runs"

    def test_a_valid_roster_populates_lines_and_run_root(self, tmp_path: Path) -> None:
        from fleet_graph.cli import _load_line_roster

        roster = tmp_path / "ronin-lines.json"
        roster.write_text(
            json.dumps(
                {
                    "run_root": "/tmp/runs",
                    "_comment": "ignore me",
                    "lines": [
                        {"folder_id": "wf-1", "seat": "s", "generation": 2, "_provenance": "x"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        owners, run_root = _load_line_roster(str(roster))
        assert [o["folder_id"] for o in owners] == ["wf-1"]
        assert "_provenance" not in owners[0]
        assert str(run_root) == "/tmp/runs"

    def test_no_config_yields_no_line_owners(self) -> None:
        from fleet_graph.cli import _load_line_roster

        owners, run_root = _load_line_roster(None)
        assert owners == []
        assert str(run_root) == "/data/fleet-graph/runs"
