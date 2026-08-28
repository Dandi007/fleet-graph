# E1 Decision Event Bridge

## Goal

Bridge each newly observed, unresolved `work.note.v1` question on
`board:work-notes` into one supervisor audit event. E1 is an observation
only: it must not publish a decision, resume a gate, change a work card, or
start a line directly.

## Authoritative Facts

The bus is the source of truth. A note is an E1 candidate only when all of
the following hold:

1. `kind == "work.note.v1"` and `payload.note_type == "question"`.
2. It is newer than the persisted board cursor.
3. `Board.decision_for(GateTicket(...))` finds no v1 or v2 decision whose
   immutable refs target that exact question message.

The bridge must use `Board.decision_for`, not a copied decision table or a
payload-only heuristic. This makes its unresolved predicate identical to the
gate's predicate.

## Event And Idempotency Contract

Emit `SupervisorEvent(type="board_question")` with:

- `key = sanitize_key("e1-" + question_note_id)`
- payload containing `question_note_id` and `card_entity_id`
- initial `attempt = 1`

The persistent cursor state is `{ "board_seq": int | null, "attempts":
object }` under the scheduler run root. The first observation adopts the
current bus head without replaying historical questions. Later scans page
forward from `board_seq`.

For one event key, the bridge's existing idempotency chain is mandatory:

- a stable transient unit name prevents concurrent duplicate launches;
- the persisted attempt counter assigns stable thread identity for an
  in-flight launch and a new attempt identity only after an explicit retry;
- an existing completion receipt suppresses another launch;
- an active unit does not consume an attempt or create another unit.

Cursor advancement is transactional in effect: advance past messages that
were non-candidates, already decided, or conclusively considered. When the
per-tick launch budget defers an unresolved question, leave the cursor before
that message so a later tick retries it. A bus or cursor failure is
fail-open for scheduling: record the observation failure and do not raise
from the scheduler tick.

## Restart And Recovery

The cursor and attempts survive daemon restart. On restart, replay of the
same question must derive the same E1 key and re-adopt the active unit/thread
rather than launch a second audit. If the prior audit completed, its receipt
suppresses it. `supervisor reset e1-<question-id>` deletes only the receipt,
retains attempts, and rewinds the cursor to immediately before the matching
question when it can locate it; it must never guess or move the cursor
forward.

## Non-Goals And Safety

No second polling loop is introduced; E1 runs inside the scheduler's existing
tick. E1 receives no decision credential and may not import the decision
publisher. The bridge is read-only against the bus except for the transient
supervisor launch. It does not deploy, restart production units, or mutate
production checkouts.

## Acceptance

```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_supervisor_events.py -q -k 'BoardScan or attempts_survive_a_restart'
```

Acceptance must demonstrate all of the following in tests:

1. An unresolved post-baseline question emits exactly one `board_question`
   E1 event with key `e1-<question message id>`; a decision referencing that
   question suppresses the event.
2. Repeated ticks and a newly constructed observer using the same state
   re-adopt/suppress the same E1 event without a duplicate launch; a deferred
   question is retried because its cursor position was retained.
3. A kill-restart simulation with an active E1 unit/thread preserves its
   launch identity and produces no second unit; completion receipt suppression
   remains true after restart.
4. The bridge fails open for unavailable bus/cursor storage and cannot
   publish decisions.
```
