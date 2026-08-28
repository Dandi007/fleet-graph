# E3 - Terminal as a Derived View

## Scope

Make the durable goal-line checkpoint state, read through `get_state`, authoritative for normal scheduler account and parking decisions. `terminal.json` becomes a derived compatibility view and fault-recovery fallback; it must not decide the ordinary terminal path.

## Required behavior

1. The scheduler obtains normal line state from checkpoint `get_state`. An absent, stale, or conflicting `terminal.json` must not change a normal terminal, account, or parking decision while checkpoint state is available.
2. Terminal materialization remains best-effort and derived for external readers. A terminal write failure or intentionally stale/missing terminal artifact must not cause replay, re-accounting, re-parking, or a duplicate coordinator round.
3. On an actual checkpoint-read fault, retain the existing explicit fault fallback and observable reason. Never silently treat an unreadable checkpoint as a completed terminal.
4. Preserve sentinel/PumpDown compatibility: its existing terminal-facing contract continues via the derived view or documented fallback.
5. Add structural tests proving the ordinary scheduler path does not read `terminal.json` as its decision source, plus regression tests for checkpoint terminal state, stale/missing terminal artifacts, checkpoint-read fault fallback, and sentinel/PumpDown compatibility.

## Constraints

- All implementation and code review are performed only by dev-dispatch in this dedicated `/data/worktrees/` worktree.
- Preserve heartbeat/liveness, fault-path terminal supplementation, parking/wake fallbacks, and terminal artifact format while external consumers require it.
- Do not remove `terminal.json`, alter agent-bus semantics, deploy/restart any unit, modify the legacy loop-engine, or combine this development with E2 or stale-running recovery.
- Do not reuse, reconfigure, copy, or otherwise alter `dev-fg-9cf82e169fa4`.
- Do not perform Git operations, writes, or validation in the production checkout. Production checkout activity is outside this development; after separately approved remote-main merge it may only run `git pull --ff-only`.

```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_goal_line.py -q
uv run pytest tests/test_parking.py -q
uv run pytest tests/test_supervisor_events.py -q
uv run pytest tests/test_line_restart.py -q
```