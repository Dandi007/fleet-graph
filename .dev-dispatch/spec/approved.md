# A1 replacement: user-session bus acceptance environment

## Goal

Replace the terminal `dev_fleet_a1_service_surface_20260827` with a protocol-v1 development that retains the approved fleet-graph dev-dispatch service-surface intent while making the acceptance environment capable of reaching the existing user session bus.

## Root Cause Evidence

- The terminal development's acceptance command allowed only `PATH` and `HOME`.
- Its `make verify` failed after systemd user-manager checks reported missing `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`.
- In this replacement H0 worktree, the explicitly reconstructed user-bus address is `unix:path=/run/user/1000/bus`; its socket exists and `systemctl --user is-system-running` reaches the manager and reports `degraded`, rather than a connection failure.

## Scope

- Preserve the approved 13-route localhost MCP service-surface objective and its direct contract tests.
- Acceptance and setup commands must explicitly provide and allow `XDG_RUNTIME_DIR=/run/user/1000` and `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`.
- Record UTC timestamps, the raw `systemctl --user is-system-running` output and exit code, and the raw `make verify` output and exit code in the acceptance evidence.

## Constraints

- All business-code changes and all code review are performed only by dev-dispatch workers in isolated worktrees.
- All Git branching, writing, commits, pushes, and verification occur only under `/data/worktrees`; production main is never checked out, switched, reset, detached, or validated.
- Do not stop, disable, restart, archive, or modify any legacy engine unit.
- Keep protocol validation fail-closed; do not replace H0, durable-MR, receipt, evidence, revision, or idempotency checks with best-effort behavior.

## Acceptance

1. With the explicit user-session environment, capture the raw output and exit code of `systemctl --user is-system-running`; `running` and `degraded` are both connected-manager states, while connection errors fail acceptance.
2. In that identical explicit environment, run `make verify` and require exit code 0.
3. Directly exercise all 13 service routes and required guards against the isolated candidate endpoint.
4. Preserve an open durable MR and export the complete development evidence chain.
