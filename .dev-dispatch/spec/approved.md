# A1 corrective replacement: use-case family and user-session acceptance evidence

## Goal

Create a protocol-v1 dev-dispatch development that completes the fleet-graph A1 service-surface cutover objective under the supervision-plane equivalence decision: behavioral equivalence for the real consumer use-case family, rather than reproduction of all legacy tool contracts.

## Corrective Basis

- `dev_fleet_a1_user_session_bus_replacement_20260827` is terminal FAILED because its immutable acceptance record accepted `systemctl --user` exit code 1 without interpreting the raw state, and its verification schema could not retain required UTC timestamps or raw command output.
- This replacement must make the acceptance evidence authoritative: it must distinguish connected `running` or `degraded` user-manager states from connection failures, and preserve timestamped raw stdout, stderr, and exit statuses.

## Scope

- Implement and behaviorally validate only the live consumer use-case family: `create`, `start`, `get`, `list`, `events`, `evidence`, and `gate`.
- For excluded legacy-only operations, return explicit documented NOT_SUPPORTED behavior rather than silently approximating their legacy semantics.
- Keep A2's reversible ronin-mcp endpoint cutover and A4's 24-hour no-legacy-dd-traffic observation requirements unchanged.

## Acceptance Environment and Evidence

1. Every acceptance command must explicitly set `XDG_RUNTIME_DIR=/run/user/1000` and `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`.
2. Run `deploy/verify-user-session-bus.sh` and record UTC timestamps plus unmodified stdout, stderr, and exit status.
3. Run `systemctl --user is-system-running` in that same environment and record UTC timestamp, raw stdout, raw stderr, and exit status. Only raw states `running` and `degraded` prove a connected user manager; an exit code of 1 alone is not acceptable and connection errors fail acceptance.
4. In the identical environment, rerun `make verify`; record UTC timestamp, raw stdout, raw stderr, and exit status, and require exit status 0.
5. Exercise the seven scoped operations against the isolated candidate endpoint, including durable identity, lifecycle progression, immutable events/evidence, and revisioned gate behavior. Include a temporary unused localhost-port reachability check without touching any legacy engine unit.

## Constraints

- All business-code writing and all code review are exclusively performed by dev-dispatch workers; coordinators do neither.
- All branching, protocol writing, commits, pushes, and verification occur only in the isolated `/data/worktrees` H0 worktree. The production main repository is read-only: do not checkout, switch, reset, detach, or validate it.
- Do not stop, disable, restart, archive, or modify any legacy engine unit.
- Preserve fail-closed H0, durable-MR, receipt, evidence, revision, idempotency, capability-lock, and worktree-root validation.

## Completion Evidence

Keep an open durable MR and export the complete bootstrap, implementation, review, acceptance, and gate evidence chain. The acceptance record must retain the session-bus and `make verify` raw outputs from this run; old PASS evidence cannot substitute for a command that cannot run.
