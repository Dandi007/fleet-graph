"""The E1 decision event bridge: read published verdicts, map each to exactly
one waiting owner, and recover it through an existing controlled entry.

This is the *resume side* of the human gate. Where the supervisor observer (E1
`board_question`) and the preauth-released decision publisher sit on the
"someone should look at this" and "reinstate a gate" faces, this bridge sits on
the "a human already cast ``work.decision.v1`` -- now make the waiting owner
advance" face. It is deliberately read-only against the bus: it pulls decision
messages off `board:work-notes`, resolves them strictly, and then calls the
*owner's own* controlled recovery entry (the dd `gate(resume=True)` adapter, or
a registered line entry) -- it never publishes a decision, never consumes an
inbox, and never writes into a supervised line's state.

Four properties make the bridge trustworthy, and each is pinned by tests:

- **Real SQLite durability.** WAL + `synchronous=FULL`; the same immediate
  transaction advances the `board_seq` source cursor and seals the receipt.
  The cursor advances only after an event gets a terminal disposition. Any
  read/write/lock/corruption failure is fail-closed: the bridge records the
  fault and refuses to resume, never falling back to an in-memory cursor.
- **Durable receipt state machine.** `intent_recorded` is persisted *before*
  the outward recovery call, so a SIGKILL between "I mean to resume" and "I
  have resumed" replays to a deterministic finish instead of losing or
  duplicating the recovery. A receipt carries the source message, the exact
  target/generation/question, the action key, status, reason and source event.
- **Strict resolver.** Only `board:work-notes` and exactly
  `work.decision.v1`; payload/decision/refs are validated, and the waiting
  owner's card, question, generation and current state are re-verified against
  the same facts the gate uses. Zero matches, multiple matches, a stale owner
  or an invalid decision each seal a structured terminal no-op receipt --
  never a fuzzy guess, never an arbitrary URL/target.
- **Owner-side action-key dedup.** The action key is exactly
  ``e1:<source_message_id>:<target_kind>:<target_id>:<generation>``. The store
  enforces a unique index on it, the adapter passes it through to the owner,
  and a duplicate transport call returns the same logical success instead of
  accepting or recovering twice.

The bridge process is a standalone user unit
(`fleet-graph-decision-bridge.service`), independent of `fleet-graphd.service`
and of the supervisor observer; none of them inherit a decision-publish
credential.
"""

from __future__ import annotations

from fleet_graph.decision_bridge.bridge import (
    DecisionBridge,
    DecisionBridgeConfig,
    run_decision_bridge,
)
from fleet_graph.decision_bridge.owners import (
    DdOwnerSource,
    HttpOwnerSource,
    OwnerResult,
    OwnerTarget,
)
from fleet_graph.decision_bridge.resolver import (
    action_key_for,
    resolve_decision,
)
from fleet_graph.decision_bridge.store import (
    STATUS_INTENT_RECORDED,
    STATUS_NOOP,
    STATUS_REFUSED,
    STATUS_RESUMED,
    TERMINAL_STATUSES,
    BridgeStore,
    BridgeStoreError,
)

__all__ = [
    "STATUS_INTENT_RECORDED",
    "STATUS_NOOP",
    "STATUS_REFUSED",
    "STATUS_RESUMED",
    "TERMINAL_STATUSES",
    "BridgeStore",
    "BridgeStoreError",
    "DdOwnerSource",
    "DecisionBridge",
    "DecisionBridgeConfig",
    "HttpOwnerSource",
    "OwnerResult",
    "OwnerTarget",
    "action_key_for",
    "resolve_decision",
    "run_decision_bridge",
]
