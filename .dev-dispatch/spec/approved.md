# U2 pending to rejected edge

Implement the smallest missing U2 state-machine edge in the production goal MCP surface, matching the existing `goal_admit` shape and authority boundary.

## Scope

- Add `goal_reject(folder_id, decision_ref, decided_by)` to the goal MCP surface, at the same supervisor-only identity guard level as `goal_admit`.
- Only `pending -> rejected` is legal. Persist `status="rejected"`, the exact `decision_ref`, and append one history row without deleting or rewriting prior rows.
- Reuse the existing queue primitive/state-machine conventions where possible. Do not broaden semantics, alter `goal_withdraw`, or change roster behavior.
- Same decision repeated after rejection is idempotent: return the existing rejected entry with an idempotent/already-rejected indication and do not append another history row. A different decision ref on rejected is refused with the existing not-pending refusal shape.
- Non-supervisor reject is refused with the supervisor-only refusal and makes no mutation.
- Rejecting an admitted or withdrawn entry is refused and makes no mutation.
- `goal_withdraw` must remain distinct; it must not produce a rejected status or serve as a reject alias.

## Tests

Add/adjust focused tests for the MCP tool listing and service/queue behavior, including all negative cases above, exact decision_ref persistence, history append/no row deletion, idempotent repeat, and the distinction from withdraw. Keep existing admitted/withdrawn behavior green.

All implementation and review must run through dev-dispatch. Use an independent worktree under `/data/worktrees/`; do not write code, switch branches, or run verification in `/data/code/self/fleet-graph`.

```dd-acceptance
make verify
```
