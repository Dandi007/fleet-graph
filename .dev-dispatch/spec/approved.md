# Targeted infrastructure fix: stale controller run-config survives replay trim

## Incident facts

- Do not start or reconfigure citizen development `dev-fg-3a5778fc6a35`.
- The predecessor infrastructure fix `dev-fg-6808b0d75c7b` is merged/deployed, but citizen generation 5 still faults in `final_review` with raw error `materialize failed on final_review: ACTOR_RESERVED_PATH_CHANGED: Reviewer worktree has staged or unstaged .dev-dispatch/** changes`.
- Direct diagnosis in the citizen development's dedicated worktree showed exactly ` M .dev-dispatch/run-config.json`.
- Its worktree diff is controller-produced stale state: committed generation 1 config versus an unstaged generation 4 reconfigured acceptance declaration.
- The predecessor fix only defers a newly reproduced `run-config.json` until acceptance. It does not cover stale controller-owned `run-config.json` already left dirty by an earlier failed generation when replay preparation finds `HEAD == replay tip`; `_prepare` therefore skips its existing `reset --hard` branch and the stale dirt survives into the fresh final reviewer.

## Required change

Implement the smallest fail-closed replay/materialization fix for that uncovered path. Before a replayed review prefix hands control to a real final reviewer, remove only provably controller-owned stale `run-config.json` residue from a previous generation, including the `HEAD == replay tip` case. Preserve the predecessor behavior that the current generation's reconfigured declaration is materialized only for acceptance. Do not broadly clean `.dev-dispatch/**`, do not hide arbitrary reserved-path changes, and do not weaken the plugin's `ACTOR_RESERVED_PATH_CHANGED` guard.

All implementation and review are performed through dev-dispatch stages. Do not deploy or restart production services.

## Required tests

Add a regression reproducing the post-deploy generation-5 shape: the replay tip is already `HEAD`, a previous generation's controller-produced reconfigured `run-config.json` is dirty before replay, configure/implement/continuous_review replay, and a real final_review materializes successfully; acceptance must observe the current declaration.

Also prove a real final-review actor write under `.dev-dispatch/**` is still refused with `ACTOR_RESERVED_PATH_CHANGED`. Prefer extending the existing replay regression around `TestAReconfiguredReplayDoesNotBlameTheReviewer` and its real reserved-path actor case.

## Constraints

- Git operations and verification only in this dedicated worktree.
- Do not operate on the production main checkout.
- Do not deploy or restart any production service.
- Keep the fix scoped to fleet-graph replay/materialization infrastructure.

```dd-acceptance
uv sync --frozen
uv run pytest tests/test_dd_replay.py -q
uv run pytest tests/test_dd_materializer.py tests/test_dd_control_plane.py -q
make verify
```
