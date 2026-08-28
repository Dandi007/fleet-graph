# Fix reconfigure run-config reserved-path attribution

## Incident evidence

`dev-fg-3a5778fc6a35` generations 2 and 3 both replay configure/implement/continuous review, then run a read-only final reviewer successfully (`exit_code=0`, `verdict=APPROVE`) but terminate with:

`materialize failed on final_review: ACTOR_RESERVED_PATH_CHANGED: Reviewer worktree has staged or unstaged .dev-dispatch/** changes`

In the dedicated development worktree at subject `f79735afc3897509bd8785a641ee532b32e5d003`, the only dirty reserved path is `.dev-dispatch/run-config.json`. Its committed generation-1 contents declare `make verify`; the controller-owned reconfigure path rewrites it in place to generation 3/4 plus the new setup and two acceptance commands before final review. The reviewer is read-only and does not write the path. An isolated acceptance worktree at the same subject proves both currently declared commands exit 0 (`tsc` clean; 166 pass, 0 fail).

## Required behavior

1. A reconfigured successor generation must not enter an actor stage with controller-owned staged or unstaged `.dev-dispatch/run-config.json` drift that is later attributed to the actor.
2. Replayed configure receipts and a changed acceptance context must produce a clean, correctly sealed/materialized subject before final review materialization, or otherwise keep controller-owned runtime configuration outside actor-change attribution.
3. Preserve the reserved-path guard for actual actor writes. A reviewer or implementer that modifies `.dev-dispatch/**` must still be rejected.
4. Preserve acceptance authority and tamper detection: the effective setup, argv and environment remain the operator-declared reconfigure context, and product code cannot silently replace them.
5. Add a regression test matching the incident shape: generation 1 committed run config, reconfigure to generation >1 with changed setup/argv, replay through final review with a read-only APPROVE result, then materialize without `ACTOR_RESERVED_PATH_CHANGED`. Assert the resulting receipt/commit chain is clean and acceptance sees the declared context.
6. Add the negative control proving a genuine actor reserved-path modification still fails.
7. Do not change agent-runtime product code, do not weaken unrelated materialization invariants, and do not merge, deploy, restart services, or manipulate any production checkout.

```dd-acceptance
uv sync --frozen
uv run pytest tests/test_dd_replay.py tests/test_dd_materializer.py tests/test_dd_control_plane.py -q
make verify
```
