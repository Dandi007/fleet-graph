# A1: fleet-graph dev-dispatch service surface

## Goal

Implement a localhost MCP service surface for `fleet_graph.dd` that is semantically equivalent to the 13 active dev-dispatch tools, without changing or disabling any legacy engine unit.

## Scope

- Add the new fleet-graph service surface as a thin adapter over graph APIs.
- Preserve the 13 tool contracts documented in `wf-a08949/findings.md`: two deployment compatibility operations, revisioned lifecycle control, and the ten attempt-context operations.
- Select a new localhost port only after checking active listeners. It must not conflict with 5606, 7455, 7460, 8113, 5601, 5602, 5605, 5608, 7490, 9090, 9093, 3300, 8101, or 3002.
- Add direct contract and reachability tests for every tool.

## Constraints

- All business-code changes and all code review are performed by dev-dispatch workers in isolated worktrees.
- Do not stop, disable, archive, restart, or modify a legacy engine unit.
- Do not write to or run validation in the production main checkout.
- Keep protocol validation fail-closed; do not replace revision, idempotency, H0, durable-MR, receipt, or evidence checks with best-effort behavior.

## Acceptance

1. Directly prove the selected new port is free before service start and the new endpoint is reachable after start.
2. Directly exercise all 13 tool routes against the service, including their required validation guards.
3. Run `make verify` directly in the isolated candidate checkout and require exit code 0.
4. Preserve an open durable MR for the development and export the development evidence chain.
