"""The decision bridge's resident loop: read verdicts, resolve, recover, seal.

This is what `fleet-graph decision-bridge run` executes. One cycle reads the
board forwards from the persisted cursor, and for each message either skips it
(as not a decision, or a decision already terminally sealed) or drives it to a
terminal disposition through the strict resolver and the owner source:

    read after cursor -> (decision?) -> resolve -> record intent
        -> owner.resume(action_key) -> seal terminal + advance cursor

The crash-safety property is the ordering: the intent lands *before* the
outward call, and the terminal seal (which also advances the cursor) lands only
*after* the owner's answer is in hand. A SIGKILL in between replays to a
durable finish with exactly one logical recovery, because the owner dedups on
the action key.

Every failure is a recorded fact, never a crash: a dead bus or an unreadable
cursor costs observation (zero resume), a refuse or transport failure seals a
terminal ``refused`` receipt, and a durability fault (an unreadable, unwritable,
locked or corrupt database) fails closed -- the bridge refuses to resume rather
than continuing on an in-memory cursor.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_graph.bus.board import DECISION_KIND, NOTE_KIND, WORK_NOTES
from fleet_graph.decision_bridge.owners import (
    RESUME_ALREADY_RESUMED,
    RESUME_REFUSED,
    RESUME_RESUMED,
    OwnerResult,
    OwnerSource,
    OwnerTarget,
)
from fleet_graph.decision_bridge.resolver import Resolution, action_key_for, resolve_decision
from fleet_graph.decision_bridge.store import (
    STATUS_INTENT_RECORDED,
    STATUS_NOOP,
    STATUS_REFUSED,
    STATUS_RESUMED,
    TERMINAL_STATUSES,
    BridgeStore,
    BridgeStoreError,
)

DEFAULT_STATE_DIR = Path("/data/fleet-graph/decision-bridge")
DEFAULT_BOARD_PAGE_LIMIT = 200

#: The channel this bridge reads, and the only channel the resolver accepts.
READ_CHANNEL = WORK_NOTES

#: The one decision kind the bridge maps. Read-only: nothing here publishes one,
#: so the decision-publish credential is never needed (and must never be held).
READ_KIND = DECISION_KIND


@dataclass
class DecisionBridgeConfig:
    state_dir: Path = DEFAULT_STATE_DIR
    poll_interval_seconds: float = 1.0
    board_page_limit: int = DEFAULT_BOARD_PAGE_LIMIT
    owner_url: str | None = None
    dd_root: Path = Path("/data/fleet-graph/dd")
    #: The line roster the production bridge may recover parked goal lines
    #: from. Empty (the default) means the bridge recovers dd developments
    #: only; populated, the bridge also discovers/resumes parked lines through
    #: their registered control entry (the scheduler's stall-state files).
    line_owners: list[Any] = field(default_factory=list)
    line_run_root: Path = Path("/data/fleet-graph/runs")
    #: Test seam for the crash-window drill: write a sentinel at this path and
    #: hold for ``kill_window_seconds`` after the owner's answer is in hand but
    #: before the terminal seal. Off in production (None).
    kill_window_file: Path | None = None
    kill_window_seconds: float = 2.0


@dataclass
class _CycleRecord:
    cursor_before: int | None = None
    cursor_after: int | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    owner_calls: int = 0
    resumed: int = 0
    bus: str = "available"
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "owner_calls": self.owner_calls,
            "resumed": self.resumed,
            "bus": self.bus,
            "actions": self.actions,
        }
        if self.error is not None:
            record["error"] = self.error
        return record


class DecisionBridge:
    def __init__(
        self,
        config: DecisionBridgeConfig,
        *,
        bus: Any = None,
        owner_source: OwnerSource | None = None,
        store: BridgeStore | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.bus = bus
        self.owner_source = owner_source
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self._question_texts_cache: dict[str, str] | None = None

    # --- assembly helpers -------------------------------------------------

    def _ensure_owner_source(self) -> OwnerSource:
        if self.owner_source is not None:
            return self.owner_source
        if self.config.owner_url:
            from fleet_graph.decision_bridge.owners import HttpOwnerSource

            self.owner_source = HttpOwnerSource(self.config.owner_url)
            return self.owner_source
        from fleet_graph.decision_bridge.owners import DdOwnerSource

        dd_source = DdOwnerSource(self.config.dd_root)
        if not self.config.line_owners:
            self.owner_source = dd_source
            return self.owner_source
        from fleet_graph.decision_bridge.owners import CompositeOwnerSource, LineOwnerSource

        line_source = LineOwnerSource(self.config.line_run_root, self.config.line_owners)
        self.owner_source = CompositeOwnerSource(
            [dd_source, line_source], kinds={"dd": dd_source, "line": line_source}
        )
        return self.owner_source

    def _ensure_store(self) -> BridgeStore:
        if self.store is None:
            self.store = BridgeStore(self.config.state_dir).open()
        return self.store

    # --- the cycle --------------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        """One poll-and-process cycle. Never raises; returns what it did."""
        record = _CycleRecord()
        try:
            self._ensure_store()
        except BridgeStoreError as exc:
            record.error = str(exc)[:400]
            return record.as_dict()  # fail closed: zero resume

        try:
            record.cursor_before = self._ensure_store().cursor()
        except BridgeStoreError as exc:
            record.error = f"cursor unreadable: {exc}"[:400]
            return record.as_dict()

        if self.bus is None:
            record.bus = "unavailable"
            record.actions.append({"action": "bus_unavailable", "source": "bridge"})
            record.cursor_after = record.cursor_before
            return record.as_dict()

        try:
            messages, _head = self.bus.messages(
                READ_CHANNEL, after_seq=record.cursor_before, limit=self.config.board_page_limit
            )
        except Exception as exc:
            record.bus = "error"
            record.actions.append(
                {"action": "bus_error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
            )
            record.cursor_after = record.cursor_before
            return record.as_dict()

        for message in messages:
            try:
                action = self._process_message(message)
            except BridgeStoreError as exc:
                # Fail closed, never a crash: a durability fault mid-decision
                # must not escape ``run_once`` (which promises to never raise),
                # and it must stop the loop rather than continue to a later
                # message that could seal and advance the cursor past this
                # still-unprocessed decision.
                record.actions.append(
                    {"action": "failed_closed:store_error", "error": str(exc)[:300]}
                )
                record.error = f"store error mid-cycle: {exc}"[:400]
                break
            record.actions.append(action)
            record.owner_calls += int(action.get("owner_calls", 0))
            if action.get("logical_resume"):
                record.resumed += 1

        try:
            record.cursor_after = self._ensure_store().cursor()
        except BridgeStoreError as exc:
            record.error = f"cursor unreadable after cycle: {exc}"[:400]
        return record.as_dict()

    def run_forever(
        self,
        *,
        observe: Callable[[dict[str, Any]], None] | None = None,
        ticks: int | None = None,
    ) -> None:
        remaining = ticks
        while remaining is None or remaining > 0:
            record = self.run_once()
            if observe is not None:
                observe(record)
            if remaining is not None:
                remaining -= 1
                if remaining == 0:
                    return
            self.sleep(self.config.poll_interval_seconds)

    # --- one message ------------------------------------------------------

    def _process_message(self, message: dict[str, Any]) -> dict[str, Any]:
        seq = int(message.get("channel_seq") or 0)
        source_message_id = str(message.get("message_id") or "")

        if message.get("kind") != READ_KIND:
            self._advance(seq)
            return {"action": "skipped:not_a_decision", "seq": seq}

        if not source_message_id:
            self._advance(seq)
            return {"action": "skipped:no_message_id", "seq": seq}

        try:
            existing = self._ensure_store().receipt(source_message_id)
        except BridgeStoreError:
            # Fail closed: we cannot read whether we already handled this
            # decision, so we must not resume it (a blurry replay could
            # double-apply). This must *stop* the message loop rather than
            # return an action the loop would record and move past: a later
            # message in the same page could seal and advance the cursor past
            # this still-unread decision, silently dropping its human verdict.
            # ``run_once`` catches the error, records it, and breaks.
            raise

        if existing is not None and existing["status"] in TERMINAL_STATUSES:
            self._advance(seq)
            return {
                "action": "skipped:terminal_receipt",
                "seq": seq,
                "status": existing["status"],
            }

        if existing is not None and existing["status"] == STATUS_INTENT_RECORDED:
            return self._complete_intent(existing, seq, source_message_id)

        return self._fresh_decision(message, seq, source_message_id)

    def _advance(self, seq: int) -> None:
        # Failing closed on a skip: the cursor does not move, so the same
        # message is re-read next cycle. That is safe (skips are idempotent)
        # and never advances past an unprocessed decision.
        with contextlib.suppress(BridgeStoreError):
            self._ensure_store().advance_cursor(seq)

    def _refs_to(self, entity_id: str) -> list[dict[str, Any]]:
        """The real bus reverse-refs surface, or [] when the bus is unavailable.

        agent-bus stores a forward reference on its *target* entity, so the
        bridge asks ``GET /v1/entities/<entity>/refs`` for the messages that
        reference it. A missing bus, or a client that predates ``refs_to``,
        fails open to "no references" (resolve nothing) -- never a crash.
        """
        refs_to = getattr(self.bus, "refs_to", None)
        if refs_to is None:
            return []
        return refs_to(entity_id)

    def _question_texts(self) -> dict[str, str]:
        """Question note id -> immutable text, for the legacy-owner fallback.

        The bounded legacy fallback (spec item 6) matches a decision ref to a
        question whose immutable text carries the exact ``folder_id``. The
        reverse-refs surface cannot list a decision's forward refs, so the
        resolver needs the candidate question texts supplied by the caller.
        Built here, once, read-only against the board channel; a missing bus or
        an unreadable channel degrades to an empty map (no legacy recovery).
        """
        if self._question_texts_cache is not None:
            return self._question_texts_cache
        texts: dict[str, str] = {}
        if self.bus is None:
            self._question_texts_cache = texts
            return texts
        try:
            messages, _head = self.bus.messages(READ_CHANNEL, limit=self.config.board_page_limit)
        except Exception:
            messages = []
        for message in messages:
            if message.get("kind") != NOTE_KIND:
                continue
            payload = message.get("payload") or {}
            if payload.get("note_type") != "question":
                continue
            texts[str(message.get("message_id") or "")] = str(payload.get("note") or "")
        self._question_texts_cache = texts
        return texts

    def _fresh_decision(
        self, message: dict[str, Any], seq: int, source_message_id: str
    ) -> dict[str, Any]:
        resolution = resolve_decision(
            message,
            self._ensure_owner_source(),
            refs_to=self._refs_to,
            channel_id=READ_CHANNEL,
            question_texts=self._question_texts(),
        )
        if not resolution.ok:
            self._seal_noop(resolution, seq, source_message_id, message)
            return {
                "action": f"noop:{resolution.category}",
                "seq": seq,
                "reason": resolution.reason[:300],
            }

        target = (
            resolution.target
            if resolution.target is not None
            else OwnerTarget("", "", 1, "", "", "")
        )
        action_key = action_key_for(source_message_id, target.kind, target.id, target.generation)
        # A legacy-owner resolution (spec item 6) is recorded durably so an
        # operator can tell a fallback recovery apart from the normal path.
        origin = "legacy_owner_resolution" if resolution.legacy else None
        self._ensure_store().record_intent(
            {
                "source_message_id": source_message_id,
                "action_key": action_key,
                "target_kind": target.kind,
                "target_id": target.id,
                "generation": target.generation,
                "question_note_id": target.question_note_id,
                "card_entity_id": target.card_entity_id,
                "reason": origin or "",
                "source_event": message,
            }
        )

        return self._resume_and_seal(
            target, action_key, seq, source_message_id, message, recovery=False, origin=origin
        )

    def _complete_intent(
        self, existing: dict[str, Any], seq: int, source_message_id: str
    ) -> dict[str, Any]:
        """Replay completion: the intent is on disk, the crash landed before the
        terminal seal. Re-call the owner with the same action key; the owner's
        durable dedup makes the replay exactly-once."""
        target = OwnerTarget(
            kind=str(existing.get("target_kind") or ""),
            id=str(existing.get("target_id") or ""),
            generation=int(existing.get("generation") or 1),
            question_note_id=str(existing.get("question_note_id") or ""),
            card_entity_id=str(existing.get("card_entity_id") or ""),
            state="",
        )
        action_key = str(existing.get("action_key") or "")
        return self._resume_and_seal(
            target,
            action_key,
            seq,
            source_message_id,
            json.loads(existing["source_event"]),
            recovery=True,
        )

    def _resume_and_seal(
        self,
        target: OwnerTarget,
        action_key: str,
        seq: int,
        source_message_id: str,
        source_event: dict[str, Any],
        *,
        recovery: bool,
        origin: str | None = None,
    ) -> dict[str, Any]:
        try:
            owner_result = self._ensure_owner_source().resume(target, action_key)
        except Exception as exc:
            owner_result = OwnerResult(RESUME_REFUSED, f"{type(exc).__name__}: {exc}")

        status = (
            STATUS_RESUMED
            if owner_result.status in {RESUME_RESUMED, RESUME_ALREADY_RESUMED}
            else STATUS_REFUSED
        )
        reason = (
            ""
            if owner_result.status == RESUME_RESUMED
            else f"{owner_result.status}: {owner_result.detail}"[:300]
        )
        if origin and not reason:
            reason = origin

        if self.config.kill_window_file is not None:
            self._hold_kill_window(source_message_id)

        self._seal_terminal(
            source_message_id=source_message_id,
            action_key=action_key,
            target=target,
            seq=seq,
            source_event=source_event,
            status=status,
            reason=reason,
        )
        return {
            "action": "completed" if status == STATUS_RESUMED else "refused",
            "seq": seq,
            "status": status,
            "logical_resume": owner_result.logical,
            "owner_calls": 1,
            "recovery": recovery,
            "action_key": action_key,
        }

    def _hold_kill_window(self, source_message_id: str) -> None:
        sentinel = self.config.kill_window_file
        if sentinel is None:
            return
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(
                json.dumps({"source_message_id": source_message_id, "held": True}),
                encoding="utf-8",
            )
        except OSError:
            pass
        self.sleep(self.config.kill_window_seconds)

    def _seal_noop(
        self,
        resolution: Resolution,
        seq: int,
        source_message_id: str,
        source_event: dict[str, Any],
    ) -> None:
        self._seal_terminal(
            source_message_id=source_message_id,
            action_key=None,
            target=OwnerTarget("", "", 1, "", "", ""),
            seq=seq,
            source_event=source_event,
            status=STATUS_NOOP,
            reason=resolution.reason,
        )

    def _seal_terminal(
        self,
        *,
        source_message_id: str,
        action_key: str | None,
        target: OwnerTarget,
        seq: int,
        source_event: dict[str, Any],
        status: str,
        reason: str,
    ) -> None:
        self._ensure_store().seal_terminal(
            {
                "source_message_id": source_message_id,
                "action_key": action_key,
                "target_kind": target.kind,
                "target_id": target.id,
                "generation": target.generation,
                "question_note_id": target.question_note_id,
                "card_entity_id": target.card_entity_id,
                "status": status,
                "reason": reason,
                "source_event": source_event,
            },
            advance_seq=seq,
        )


def run_decision_bridge(
    config: DecisionBridgeConfig,
    *,
    bus: Any = None,
    owner_source: OwnerSource | None = None,
    store: BridgeStore | None = None,
    observe: Callable[[dict[str, Any]], None] | None = None,
    ticks: int | None = None,
) -> None:
    """Run the bridge until told otherwise, streaming one JSON line per cycle."""
    bridge = DecisionBridge(config, bus=bus, owner_source=owner_source, store=store)
    bridge.run_forever(observe=observe, ticks=ticks)


__all__ = [
    "DEFAULT_BOARD_PAGE_LIMIT",
    "DEFAULT_STATE_DIR",
    "READ_CHANNEL",
    "READ_KIND",
    "DecisionBridge",
    "DecisionBridgeConfig",
    "run_decision_bridge",
]
