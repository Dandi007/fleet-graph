# E2: in-graph decision interrupt

## Goal
Implement the approved `design-e2.md` contract in fleet-graph. Replace the normal goal-line `blocked + waiting_on=decision` parking path with a durable graph interrupt which resumes the same generation and continuation after a validated board decision. This is a code-and-review task exclusively for dev-dispatch.

## Required behavior

1. On a human-decision wait, atomically persist an interrupt checkpoint containing `folder_id`, `generation`, `round_id`, `question_note_id`, `card_entity_id`, `prior_terminal_digest`, and `resume_key`. `resume_key` is `e2:<folder_id>:<generation>:<question_note_id>` and is unique.
2. Do not schedule a new coordinator round or charge a turn merely to suspend. A valid resume continues from the interrupt, preserving `generation`, `round_id`, `turn_id`, and prior accounting.
3. Construct an immutable `DecisionInput` from the validated board entity and refs, with `message_id`, `channel_seq`, `decision`, `rationale`, `decided_by`, `question_note_id`, `card_entity_id`, `refs`, `decided_at`, and `resume_key`. Persist it in the resumed envelope/checkpoint before invoking the coordinator. The coordinator structured result must acknowledge `message_id`.
4. N7 must reject a round-zero re-park that repeats the same `prior_terminal` blocker without a new contradictory mechanical fact.
5. Preserve all existing parking/wake polling, GateAutoResumer, and compatibility fallback code. E2 must not delete, disable, or make these unreachable.
6. For legacy parked owners without a question id, retain the existing owner resolver and add only bounded fallback: match one decision ref to one question whose immutable text has the exact `folder_id`, require exactly one non-terminal legacy parked owner, record `legacy_owner_resolution`, and use the existing escape hatch. Zero/multiple matches are safe no-resume `legacy_owner_ambiguous` outcomes; do not fabricate or mutate a question id.
7. On each resume, compensate an event-page/restart cursor gap by querying the authoritative decision chain for the suspended question, choosing the newest valid decision by `(channel_seq, message_id)`, and recording a local `cursor_compensation` receipt when it is newer than `last_decision_message_id`. Never roll back the bridge cursor or republish a decision. Both paths must converge through the same `resume_key`.
8. Deduplicate externally observable execution: one resumed envelope per `(folder_id, generation, question_note_id)`, no round replay/reset, at most one usage/charge row for a `turn_id`, no second model invocation after duplicate delivery/cursor compensation/restart, and stale decisions cannot resume a newer generation. A crash after turn claim must re-adopt the same turn.
9. Do not change agent-bus semantics, terminal-view work, human-decision publishing, or deployment automation. Do not restart, stop, start, or otherwise operate any production unit. Do not merge any branch.

## Test requirements
Use real checkpoint persistence, bridge process behavior, receipt/usage ledger, and a controllable clock; handler-only mocks are insufficient. The 24-hour test may advance a controllable clock by 86400 seconds and must prove durable suspension/resume, not merely a wall-clock sleep.

Add/extend tests and an acceptance script so these scenarios prove:
- decision content is injected into the resumed envelope and an old blocker cannot immediately re-park;
- unique legacy owner fallback resumes only that owner; ambiguity performs no resume and leaves fallback active;
- cursor compensation recovers a missed decision without cursor rollback, republish, or duplicate logical resume;
- SIGKILL after persisted turn claim restarts without round replay, duplicate model invocation, or duplicate charge;
- a 86400-second suspended interrupt resumes through the bridge in the same generation/continuation, without a new question, new round, polling-only dependency, or duplicate charge.

## Constraints
All implementation and both code-review stages are dev-dispatch responsibilities. Work only in the dev-dispatch-created isolated worktree. Keep the product diff scoped to this task and do not include `.dev-dispatch/` artifacts in product changes. Production checkout must not be used for code, review, validation, checkout/switch/reset/detach, or deployment. After a separate remote-main merge and explicit deployment approval, production may only use `git pull --ff-only`.

## Acceptance
```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_line_restart.py -q
uv run pytest tests/test_goal_interrupt.py -q
uv run python scripts/e2_goal_interrupt_acceptance.py --scenario decision-content-injected
uv run python scripts/e2_goal_interrupt_acceptance.py --scenario legacy-owner-fallback
uv run python scripts/e2_goal_interrupt_acceptance.py --scenario cursor-compensation
uv run python scripts/e2_goal_interrupt_acceptance.py --scenario kill-restart-no-replay --kill-after turn_claimed
uv run python scripts/e2_goal_interrupt_acceptance.py --scenario suspend-24h-then-bridge-resume --suspend-seconds 86400
```