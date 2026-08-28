# Stale-running development recovery

## Scope
Fix the development-dispatch control-plane defect in which a development is persisted as `running` while its generation systemd unit is no longer live (`inactive/dead`, no PID), causing the supported `development_start` interface to return `started=false, already_running=true` indefinitely.

This is a control-plane repair only. Do not modify, reconfigure, create a new generation for, or re-dispatch the existing E2 development `dev-fg-b5479a961625` as part of implementation. The original development will be recovered only after this repair is accepted, exclusively through its supported interface.

## Required behavior
1. Add an automated reproduction for a stale-running record whose recorded generation unit is inactive/dead (or otherwise conclusively not active) while the development API says `running`.
2. The public supported recovery path must reconcile this contradiction. Invoking the documented start/recovery interface must not report `already_running` for a dead unit; it must either resume the existing generation safely or return/persist a clear terminal state with a concrete reason.
3. Preserve normal behavior for an actually active generation: it remains a no-op and must not duplicate dispatch.
4. Recovery must be idempotent and must not create a new development or silently re-dispatch a completed sealed stage.
5. Record sufficient state/event evidence for callers to distinguish resumed recovery from terminal resolution.

## Constraints
- All implementation and code review must be performed by dev-dispatch.
- All git and validation work occurs only in the dedicated `/data/worktrees/` worktree.
- Do not deploy, restart production units, or modify production checkouts.

```dd-acceptance
uv sync --frozen
make verify
uv run pytest -q
```
