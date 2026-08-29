# Spec — E2 goal-line card pass-through: eliminate the dual card / 409 IDEMPOTENCY_CONFLICT

This development implements the formal fix for the E2 card bug recorded in goal.md (🔴 监督面立案): the scheduler daemon and the in-graph interrupt runtime must converge on **one** board card per goal line instead of publishing two cards.

## Context

- Scheduler daemon `_ask_board` publishes the line card with idempotency key `goal-line-card:<folder>` and persists `board_card_entity_id` into `.scheduler/<folder>.json` (`src/fleet_graph/scheduler/daemon.py:829-889`).
- The E2 interrupt runtime `LineInterruptPort.ask` currently publishes its own card with key `e2-goal-line-card:<folder>` (`src/fleet_graph/goal_interrupt/runtime.py:92-104`) — the hotfix that stopped the 409 but left one extra card per line.
- Original failure: when the runtime shared the daemon's key with a *different* payload, the bus returned `409 IDEMPOTENCY_CONFLICT`, `decision_interrupt` raised `BusConflict`, and the line `pump_fault`ed at round 0.

Design input (authoritative detail): `design-e2-card-passthrough.md` in work folder wf-d002a6. Follow it. This spec freezes the scope below.

## Decision

1. One shared card identity per line: a single shared constructor (`goal_line_card_key(folder_id)` + `goal_line_card_payload(...)`, one `intent` string) used by **both** the scheduler daemon and the interrupt runtime. Same key + same payload, so the bus idempotency converges the two producers onto one card instead of 409-ing or duplicating.
2. Pass-through of the scheduler's card: thread `board_card_entity_id` from the stall state into the line process (`LaunchSpec` → `systemd-run` argv `--board-card` → `LineConfig.board_card_entity_id` → `_build_interrupt` → `LineInterruptPort(card_entity_id=...)`), so the runtime reuses the existing card.
3. Runtime fallback: when no card entity id is available at first ask, publish through the shared constructor + shared key, adopt the returned entity id including the `deduplicated=True` case, and persist it into the interrupt checkpoint.
4. Delete the `e2-goal-line-card:` key once both producers converge; no migration code may depend on the old dual card.

## Scope (do)

- Add the shared card constructor + shared key, imported by both `scheduler/daemon.py` and `goal_interrupt/runtime.py`.
- Add `board_card_entity_id` to `LaunchSpec`, `spec_for`, `cli --board-card`, `LineConfig`, `_build_interrupt`, and `LineInterruptPort`.
- Make `LineInterruptPort.ask` reuse the passed-in card and, on fallback, publish via the shared constructor and adopt a `deduplicated` result.
- Regression tests covering the two timing paths:
  - **Path A — daemon creates the card first, runtime reuses** (exactly one `work.card.v1`; interrupt checkpoint `card_entity_id` == stall-state `board_card_entity_id`; no `BusConflict`; no `e2-goal-line-card:` publish).
  - **Path B — both producers race the first create** (exactly one `work.card.v1`; both adopt the same `card_entity_id`; runtime adopts `deduplicated=True` result; no 409 surfaces as a line fault).
  - The test fake bus must faithfully model real idempotency semantics (same key + identical payload ⇒ deduplicate and return the existing entity; same key + different payload ⇒ conflict), because the whole bug is a contract-shape bug the fake must not paper over.
- 12:4x verification — decision-content injection in the production resume path (see below).
- A real-bus stall → resume drill (see below).

## Scope (do not)

- Do not alter agent-bus semantics or touch the bus kernel.
- Do not remove the polling/parking fallback, the wake probe, or GateAutoResumer.
- Do not change terminal-view work (E3), E4a/E5 increments, or the coordinator/worker prompts.
- Do not auto-publish human decisions.
- Do not deploy, restart `fleet-graphd`/`fleet-graph-dd-mcp`/any production unit, or run any git operation in the production main checkout. All git work is in the dedicated `/data/worktrees/` worktree. Production may only `git pull --ff-only` after a separately approved remote-main merge.

## 12:4x verification — decision-content injection

Findings 12:4x suspects a resumed coordinator round in production may not carry the decision payload. Code reading shows the graph wiring is present (`_coordinator_input(..., decision=decision)` injects `decision` + `resume_key`; `resume_line` records the full `DecisionInput` before invoking). Close this mechanically on the production-shaped path (bridge `resumer_for` → `resume_goal_line` → `resume_line` → graph), not only the harness path:

- assert the persisted `coord/round-N-input.json` on the resumed round contains a `decision` object whose `message_id`/`decision`/`rationale`/`refs` equal the injected `DecisionInput`, plus a `resume_key`;
- assert the bridge resumer records the full decision before resume, so a crash between record and invoke cannot lose the payload;
- assert a coordinator that re-declares the same `blocked + waiting_on=decision` without acknowledging `message_id` is rejected by the N7 round-zero-repark guard.

## Real-bus stall → resume drill

One acceptance scenario must run against the real agent-bus at `http://127.0.0.1:7490`:

1. Publish a card + question on the real bus with the shared key/constructor; read back real entity ids.
2. Suspend a synthetic, drill-tagged line through the E2 interrupt against a real SQLite store + real checkpointer.
3. Publish a real `work.decision.v1` referencing the question; run the real `GoalInterruptBridge` once; assert the line resumes, the resumed coordinator round input carries the decision payload, and exactly one card exists for the drill folder.
4. Emit JSON evidence: UTC timestamps, real card/question/decision message ids, pre/post cursor, generation/round, direct exit code.

Credentials come from `--bus-token-file` / `--decision-token-file` flags (never hard-code tokens). The drill publishes throwaway entities tagged with a unique run id and must not touch any real line's card or question.

## Acceptance

The following argv are the acceptance contract. The new `tests/test_goal_line_card.py` and `scripts/e2_card_passthrough_acceptance.py` must exist and pass; the focused regression argv below are required and may be supplemented, not replaced.

```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_goal_interrupt.py -q
uv run pytest tests/test_parking.py -q
uv run pytest tests/test_line_restart.py -q
uv run pytest tests/test_goal_line_card.py -q
uv run python scripts/e2_card_passthrough_acceptance.py --scenario daemon-first-runtime-reuses
uv run python scripts/e2_card_passthrough_acceptance.py --scenario concurrent-first-create
uv run python scripts/e2_card_passthrough_acceptance.py --scenario decision-content-in-production-resume
uv run python scripts/e2_card_passthrough_acceptance.py --scenario real-bus-stall-resume --bus-url http://127.0.0.1:7490 --bus-token-file /data/agent-bus/tokens/fleet-graph.token --decision-token-file /data/agent-bus/tokens/fleet-graph-decision.token
```

## Definition of done

- Both producers use one shared key + one shared payload constructor; `e2-goal-line-card:` is gone.
- `board_card_entity_id` threads from the scheduler stall state into `LineInterruptPort`.
- The two timing paths are pinned by regression tests with faithful idempotency semantics.
- The 12:4x mechanical proof passes on the production-shaped resume path.
- The real-bus stall→resume drill passes and emits JSON evidence.
- The final dev-dispatch receipt records each acceptance argv and its direct exit code.
