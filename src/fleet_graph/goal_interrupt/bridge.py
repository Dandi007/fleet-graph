"""The resident loop that turns board decisions into goal-line resumes.

E1's bridge reads a board cursor forward and resolves each decision to a parked
owner. E2's bridge does the same for *suspended* goal lines, but its resume path
is deliberately cursor-independent: for every still-suspended question it asks
the authoritative decision chain directly and picks the newest valid decision by
``(channel_seq, message_id)``. That is what spec item 7's cursor compensation
buys -- a decision the board cursor already paged past is still recovered, never
by rolling the cursor back or republishing, only by re-reading the chain.

Two paths, one ``resume_key``:

- a decision observed after the cursor resolves to a suspended question and
  resumes it;
- a suspended question whose decision the cursor missed is found by the chain
  query and resumes through the same ``resume_key``, recording a
  ``cursor_compensation`` receipt when the recovered decision is newer than
  ``last_decision_message_id``.

Every failure is a recorded fact, never a crash, and never a second model
invocation: the resume receipt and the per-turn usage ledger dedup both.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fleet_graph.bus.board import DECISION_KINDS
from fleet_graph.goal_interrupt.resolver import decision_input_from_message
from fleet_graph.goal_interrupt.store import GoalInterruptStore

DEFAULT_BOARD_PAGE_LIMIT = 200

#: Upper bound on how far back the decision-chain walk goes for a still-
#: suspended question. The bus pages ascending and a plain ``messages(...,
#: limit=N)`` returns the *oldest* N messages, so the chain must be read
#: backward from the head (see ``_decision_chain``). A suspended question's
#: decision sits near the new end, so a generous window covers every realistic
#: board; the cap only keeps a permanently-unanswered question from turning
#: each bridge cycle into an unbounded walk of a very long channel.
MAX_CHAIN_SCAN_PAGES = 50

#: Board channels the bridge reads decisions from. Same read surface as E1.
WORK_NOTES = "board:work-notes"


@dataclass
class GoalInterruptBridgeConfig:
    board_page_limit: int = DEFAULT_BOARD_PAGE_LIMIT
    poll_interval_seconds: float = 1.0


class GoalInterruptBridge:
    def __init__(
        self,
        config: GoalInterruptBridgeConfig,
        *,
        store: GoalInterruptStore,
        bus: Any = None,
        resumer: Callable[[Any], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = store
        self.bus = bus
        self.resumer = resumer or (lambda decision: "no_resumer")
        self.clock = clock

    # --- decision chain ----------------------------------------------------

    def _head(self) -> int:
        """The channel's head seq. ``limit=1`` still reports ``head_seq``, which
        is all we need to page backward from the new end of the channel."""
        if self.bus is None:
            return 0
        try:
            _messages, head = self.bus.messages(WORK_NOTES, limit=1)
        except Exception:
            return 0
        return int(head or 0)

    def _decision_chain(self, question_note_id: str) -> list[dict[str, Any]]:
        """The authoritative decisions answering one question, newest first.

        The bus pages *ascending*: a plain ``messages(..., limit=N)`` returns
        the *oldest* N messages, so on a long-lived channel the decision that
        answers a suspended question -- which sits at the new end -- would never
        be in the fetched page and the line would stay suspended forever. The
        chain is therefore read backward from the head (the client's documented
        recipe: learn ``head_seq`` with ``limit=1``, then re-read from just
        below it), page by page, and stops at the first page that holds a
        candidate decision. Pages are newest-first, so that decision is the
        newest one by ``(channel_seq, message_id)``.
        """
        if self.bus is None:
            return []
        refs_to = getattr(self.bus, "refs_to", None)
        if refs_to is None:
            return []
        try:
            refs = refs_to(question_note_id)
        except Exception:
            return []
        candidate_ids = {str(ref.get("message_id") or "") for ref in refs if isinstance(ref, dict)}
        if not candidate_ids:
            return []
        upper = self._head()
        pages = 0
        while upper > 0 and pages < MAX_CHAIN_SCAN_PAGES:
            window_start = max(0, upper - self.config.board_page_limit)
            try:
                messages, _head = self.bus.messages(
                    WORK_NOTES, limit=self.config.board_page_limit, after_seq=window_start
                )
            except Exception:
                return []
            decisions = [message for message in messages if strategy(message, candidate_ids)]
            if decisions:
                return sorted(
                    decisions,
                    key=lambda m: (int(m.get("channel_seq") or 0), str(m.get("message_id") or "")),
                    reverse=True,
                )
            upper = window_start
            pages += 1
            if window_start == 0:
                break
        return []

    def _references(self, question_note_id: str) -> list[dict[str, str]]:
        refs_to = getattr(self.bus, "refs_to", None)
        if refs_to is None:
            return []
        try:
            refs = refs_to(question_note_id)
        except Exception:
            return []
        return [
            {"message_id": str(ref.get("message_id") or ""), "target_entity": question_note_id}
            for ref in refs
            if isinstance(ref, dict)
        ]

    def _cursor(self) -> int:
        try:
            return self.store.cursor()
        except Exception:
            return 0

    def _board_head(self) -> int:
        return self._head()

    def _resume_question(
        self, interrupt: dict[str, Any], decision: dict[str, Any], *, prior_cursor: int
    ) -> str:
        question_note_id = str(interrupt["question_note_id"])
        resume_key = str(interrupt["resume_key"])
        seq = int(decision.get("channel_seq") or 0)
        message_id = str(decision.get("message_id") or "")

        decision_input = decision_input_from_message(
            decision,
            resume_key=resume_key,
            question_note_id=question_note_id,
            card_entity_id=str(interrupt.get("card_entity_id") or ""),
            references=self._references(question_note_id),
        )

        # Cursor compensation (spec item 7): a decision sitting at or behind the
        # cursor position this cycle started from is one the cursor has already
        # paged past -- an event-page or restart gap -- so a local receipt is
        # recorded. A decision ahead of the cursor is observed in order and is
        # not a compensation. The receipt is forward-monotonic, so a replayed
        # decision can never roll it back or duplicate it.
        if seq <= prior_cursor:
            self.store.record_compensation(resume_key, message_id, seq)

        return self.resumer(decision_input)

    # --- the cycle ----------------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        record: dict[str, Any] = {"actions": [], "resumed": 0}
        try:
            interrupts = self.store.interrupts()
        except Exception as exc:  # fail closed: durable state unreadable
            record["error"] = f"{type(exc).__name__}: {exc}"[:400]
            return record

        # Cursor compensation bookkeeping: remember the cursor position the
        # cycle started from so a decision behind it is a recoverable gap, and
        # drive the cursor forward (monotonic, never backwards) after processing.
        prior_cursor = self._cursor()
        head = self._board_head()

        for interrupt in interrupts:
            if self.store.resume_receipt(str(interrupt["resume_key"])) is not None:
                record["actions"].append(
                    {"resume_key": interrupt["resume_key"], "action": "skipped:already_resumed"}
                )
                continue
            chain = self._decision_chain(str(interrupt["question_note_id"]))
            if not chain:
                record["actions"].append(
                    {"resume_key": interrupt["resume_key"], "action": "no_decision"}
                )
                continue
            try:
                status = self._resume_question(interrupt, chain[0], prior_cursor=prior_cursor)
            except Exception as exc:  # never crash, never a second invoke
                record["actions"].append(
                    {
                        "resume_key": interrupt["resume_key"],
                        "action": "failed",
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                continue
            record["actions"].append(
                {"resume_key": interrupt["resume_key"], "action": f"resumed:{status}"}
            )
            record["resumed"] += 1

        # Cursor evidence is never worth crashing the cycle.
        with contextlib.suppress(Exception):
            self.store.advance_cursor(head)
        return record

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
            time.sleep(self.config.poll_interval_seconds)


def strategy(message: dict[str, Any], candidate_ids: set[str]) -> bool:
    """A decision worth considering: the right kind and one of the question's refs."""
    return (
        str(message.get("message_id") or "") in candidate_ids
        and message.get("kind") in DECISION_KINDS
    )


__all__ = [
    "DEFAULT_BOARD_PAGE_LIMIT",
    "MAX_CHAIN_SCAN_PAGES",
    "GoalInterruptBridge",
    "GoalInterruptBridgeConfig",
    "strategy",
]
