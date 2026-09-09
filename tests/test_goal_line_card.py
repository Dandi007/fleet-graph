"""Scheduler goal-line card materialisation: one card per line.

The scheduler daemon's parking escalation materialises a goal line's board card
through the shared idempotency key and payload constructor, and threads the
materialised ``board_card_entity_id`` through ``spec_for`` -> ``--board-card`` ->
``LineConfig`` so the next launch carries it. The fake board here faithfully
models real idempotency semantics -- same key + identical payload => deduplicate
and return the existing entity; same key + different payload => conflict --
because the whole bug is a contract-shape bug the fake must not paper over.

The E2 in-graph interrupt runtime that used to *reuse* (or race) the scheduler's
card was removed in dd-41-3; the scheduler-side card materialisation and the
pass-through wiring it rests on remain.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fleet_graph.bus.board import (
    BusConflict,
    goal_line_card_key,
    goal_line_card_payload,
)
from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig
from fleet_graph.scheduler.launcher import LaunchResult
from fleet_graph.scheduler.wake import parse_bus_timestamp

FOLDER_ID = "wf-1"
ALIAS = "canary"
BLOCKED_AT = "2026-08-27T10:00:00Z"
BLOCKED_EPOCH = parse_bus_timestamp(BLOCKED_AT)
PRIME_EPOCH = BLOCKED_EPOCH - 1800.0


# --- fakes ------------------------------------------------------------------


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeUnits:
    def is_active(self, unit_name: str) -> bool:
        return False


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        return LaunchResult(spec.unit_name, True, "")


class FakeProber:
    def check(self, seat: str) -> bool:
        return True


class FakeWake:
    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        return False

    def goal_revision(self, folder_id: str) -> str:
        return "sha256:rev-1"


class IdempotentFakeBoard:
    """Faithful bus idempotency for ``work.card.v1`` publishes.

    - fresh key => new entity, ``deduplicated=False``;
    - same key + identical payload => return the existing entity,
      ``deduplicated=True`` (no second entity);
    - same key + different payload => raise ``BusConflict`` (the real bus
      answers ``409 IDEMPOTENCY_CONFLICT``).
    """

    def __init__(self) -> None:
        self.cards: dict[str, dict[str, Any]] = {}
        self.card_publishes: list[str] = []
        self.deduplicated_publishes: list[str] = []

    def publish_card(self, payload: dict[str, Any], idempotency_key: str) -> Any:
        self.card_publishes.append(idempotency_key)
        if idempotency_key in self.cards:
            existing = self.cards[idempotency_key]
            if existing["payload"] != payload:
                raise BusConflict(409, "IDEMPOTENCY_CONFLICT: same key, different payload")
            self.deduplicated_publishes.append(idempotency_key)
            return SimpleNamespace(entity_id=existing["entity_id"], deduplicated=True)
        entity_id = f"card-{idempotency_key}"
        self.cards[idempotency_key] = {"payload": payload, "entity_id": entity_id}
        return SimpleNamespace(entity_id=entity_id, deduplicated=False)


# --- helpers ----------------------------------------------------------------


def make_scheduler(tmp_path: Path, board: Any) -> Scheduler:
    return Scheduler(
        SchedulerConfig(
            lines=[
                LineSpec(folder_id=FOLDER_ID, seat="opencode-dsv4pro", alias=ALIAS, enabled=True)
            ],
            run_root=tmp_path / "runs",
            maintenance_stop_path=tmp_path / "maintenance-stop",
        ),
        prober=FakeProber(),
        launcher=FakeLauncher(),
        units=FakeUnits(),
        clock=Clock(PRIME_EPOCH),
        sleep=lambda _s: None,
        wake=FakeWake(),
        board=board,
    )


def stall_file(tmp_path: Path) -> Path:
    return tmp_path / "runs" / ".scheduler" / f"{FOLDER_ID}.json"


# --- the fake must not paper over the contract-shape bug --------------------


class TestFaithfulIdempotency:
    def test_same_key_different_payload_conflicts(self) -> None:
        """Why the original failure existed: same key + divergent payload is a
        409, not a dedup. The fake models this so a regression to a divergent
        payload fails loudly instead of quietly passing."""
        board = IdempotentFakeBoard()
        board.publish_card(
            goal_line_card_payload(folder_id=FOLDER_ID, title=ALIAS),
            idempotency_key=goal_line_card_key(FOLDER_ID),
        )
        with pytest.raises(BusConflict):
            board.publish_card(
                {
                    "title": FOLDER_ID,
                    "status": "doing",
                    "intent": "divergent",
                    "work_folder_id": FOLDER_ID,
                },
                idempotency_key=goal_line_card_key(FOLDER_ID),
            )

    def test_same_key_identical_payload_deduplicates(self) -> None:
        board = IdempotentFakeBoard()
        first = board.publish_card(
            goal_line_card_payload(folder_id=FOLDER_ID, title=ALIAS),
            idempotency_key=goal_line_card_key(FOLDER_ID),
        )
        second = board.publish_card(
            goal_line_card_payload(folder_id=FOLDER_ID, title=ALIAS),
            idempotency_key=goal_line_card_key(FOLDER_ID),
        )
        assert second.deduplicated is True
        assert second.entity_id == first.entity_id
        assert len(board.cards) == 1


# --- the pass-through wiring (no graph, no board needed) --------------------


class TestPassThroughWiring:
    def test_spec_for_threads_the_stall_card_into_the_launch_argv(self, tmp_path: Path) -> None:
        stall = stall_file(tmp_path)
        stall.parent.mkdir(parents=True, exist_ok=True)
        stall.write_text(json.dumps({"board_card_entity_id": "card-xyz"}), encoding="utf-8")

        scheduler = make_scheduler(tmp_path, board=None)
        spec = scheduler.spec_for(
            LineSpec(folder_id=FOLDER_ID, seat="s", alias=ALIAS, enabled=True)
        )
        assert spec.board_card_entity_id == "card-xyz"
        argv = spec.argv()
        assert argv[argv.index("--board-card") + 1] == "card-xyz"

    def test_an_absent_stall_card_means_no_flag(self, tmp_path: Path) -> None:
        scheduler = make_scheduler(tmp_path, board=None)
        spec = scheduler.spec_for(
            LineSpec(folder_id=FOLDER_ID, seat="s", alias=ALIAS, enabled=True)
        )
        assert spec.board_card_entity_id == ""
        assert "--board-card" not in spec.argv()

    def test_the_cli_parses_board_card_into_the_line_config(self, tmp_path: Path) -> None:
        from fleet_graph.cli import build_parser
        from fleet_graph.graphs.runner import LineConfig

        args = build_parser().parse_args(
            ["line", "run", "--folder", FOLDER_ID, "--seat", "s", "--board-card", "card-xyz"]
        )
        config = LineConfig(
            folder_id=args.folder,
            seat=args.seat,
            run_root=tmp_path,
            alias=args.alias,
            board_card_entity_id=args.board_card or "",
        )
        assert config.board_card_entity_id == "card-xyz"
