# U4 closeout: wire supervisor admission surface

## Initial handoff

`initial_handoff.worktree_path` MUST be `/data/worktrees/fleet-graph-h0-wf-c106b9-u4-admit-20260901`, an independent H0 worktree at current `main` HEAD `f84e778eec92bcca2e2d8e64a081ac1316dea4cc`. Never use, modify, or name the production main checkout as the implementation worktree.

## Diagnosis and scope

This is a missing-call-surface wiring change, not a state-machine rewrite. `src/fleet_graph/goal_enroll/queue.py` already contains the write-back primitives `mark_admitted` and `mark_rejected` (around lines 216/228) on main and deployed release `20260901-220832-f84e778eec92`; repository-wide there is currently no caller. Goal MCP (around line 5611) exposes only `goal_enroll`, `goal_list`, `goal_status`, and `goal_withdraw`, with no admit tool or server-side supervisor release path.

Implement the smallest correct edge for `pending -> admitted`:

1. Expose an admit-capable goal MCP tool, or an equivalent server-side path invoked by the supervisor release action. Prefer the minimal design consistent with existing MCP/auth patterns.
2. Admission authority remains exclusively with the supervisor plane. This development creates the callable capability only and MUST NOT broaden or alter the authorization boundary.
3. Reuse/wire the existing `mark_admitted` primitive. Do not rewrite the queue state machine.
4. A successful admission writes `status='admitted'`, persists `decision_ref` from the supervisor release verdict message ID, and appends history without deleting or replacing existing history rows.
5. The real U4 closeout decision reference is `msg_01M1EK40MW5PKWB8HKQF1EH9HJ` (`work.decision.v1`, board seq 1564).
6. Repeated admission of the same already-admitted enrollment with the same decision is idempotent.

## Required negative and regression tests

Add tests proving all of the following:

- a non-supervisor identity cannot invoke admission;
- admission of an already `rejected` enrollment is refused;
- admission of an already `withdrawn` enrollment is refused;
- repeated admission is idempotent and does not duplicate/destructively rewrite history;
- the MCP `tools/list` surface contains the new admit capability and its required arguments;
- successful admission exposes `status='admitted'` and the exact `decision_ref` through the queue/read model and `/v1/enrollments`.

Preserve existing behavior and test coverage for enroll/list/status/withdraw and queue transitions.

## U4 harvest and production evidence

After code, review, merge, and governed deployment/harvest, use the already-existing real release facts rather than recreating them:

- enrollment/work folder: `wf-e7b0dd`;
- roster PR #214 merged to main at `3ad6937`;
- alias correction PR #215 merged at `80e43a6`;
- release decision `work.decision.v1` board seq 1564, message ID `msg_01M1EK40MW5PKWB8HKQF1EH9HJ`;
- the line has already been ignited and ended `terminal=done`.

Invoke the newly deployed supervisor-only admission path for `wf-e7b0dd` using that existing decision fact. Capture fresh machine evidence that:

- goal MCP `tools/list` includes the admit surface;
- queue/read model reports `status='admitted'` and `decision_ref='msg_01M1EK40MW5PKWB8HKQF1EH9HJ'`;
- `/v1/enrollments` reports the same admitted status and decision reference;
- history retained the original rows and appended the admission transition.

Do not fabricate or recreate the decision, roster, alias, or terminal facts. If production deployment or supervisor credential/action must be performed by a separately governed operator, produce an explicit deploy/harvest handoff with exact command/API and required identity rather than weakening authorization or substituting a test-only write.

## Governance

All implementation and review are performed by dev-dispatch. All changes go through the feature worktree and PR/merger path; production checkout is ff-only. Acceptance criteria are frozen. Do not directly edit main. Do not add backward-compatibility shims unless demanded by an existing persisted/external contract.

```dd-acceptance
make verify
uv run python scripts/e2_goal_enroll_acceptance.py
```
