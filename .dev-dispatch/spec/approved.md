# Bind production ReconcileSource and close B3 end-to-end evidence

## Trigger

This corrective development is opened for the newly observed fact `RECONCILE_SOURCE_UNBOUND`: the already-registered production `wf_reconcile` MCP tool is discoverable, but `serve()` constructs the MCP server without a concrete `ReconcileSource`, so real calls refuse rather than reconciling live work-folder residue.

## Governance and workspace constraints

- All implementation and all code review MUST be performed by dev-dispatch stages. The caller will not write or review code.
- Every H0 operation, git check, implementation, review, and acceptance operation MUST run only in the dedicated independent worktree `/data/worktrees/fleet-graph-reconcile-source-binding-h0-20260829` or dev-dispatch-created independent worktrees beneath `/data/worktrees`.
- `initial_handoff.worktree_path` MUST be an absolute path beneath `/data/worktrees` and MUST NOT name the production main checkout.
- The production main checkout is read-only for this development: do not run checkout, switch, reset, detach, code edits, tests, or acceptance there. It may only receive `git pull --ff-only` after the accepted change is merged to remote `main`.
- Preserve opaque work-folder addressing. Public MCP responses/errors must not disclose physical data-repository paths.

## Required implementation

1. Implement a concrete production `ReconcileSource` backed by the real governed work-folder storage/repository used by the production MCP deployment. Bind it in the actual `serve()` / production construction path so `wf_reconcile` no longer returns `RECONCILE_SOURCE_UNBOUND` in production.
2. Preserve the existing two-step CAS contract: dry-run classifies without mutation and returns a safe plan/token; confirm adopts only the exact append-only bytes bound by that token; replay remains idempotent.
3. Ensure stale tokens and every unsafe residue class refuse closed without any data mutation. At minimum verify rewrite/non-append residue, and retain the existing deletion/conflict/untracked/binary/cross-folder/dirty-control protections.
4. Add an end-to-end acceptance harness that invokes the real production MCP service surface with the concrete source, not a FakeSource or directly constructed test-only reconciler. It must create isolated disposable governed work-folder fixtures, print a fresh UTC timestamp during the acceptance run, and print raw JSON/text request/response evidence for: safe dry-run, successful CAS confirmation of append-only residue, stale-token refusal with before/after byte identity, and unsafe-residue refusal with before/after byte identity. It must exit nonzero if any invariant is absent. Never print a physical backing-store path.
5. Update `docs/findings/work-folder-residue-reconciliation.md` to add the `RECONCILE_SOURCE_UNBOUND` phenomenon, mechanism/root cause, production binding, and immutable B3 evidence anchors. Distinguish proven facts from hypotheses and cite the new real-surface tests/acceptance receipt.
6. Add focused unit/integration tests for source inspection/adoption, production binding, opaque addressing, CAS behavior, refusal immutability, and service lifecycle/configuration. Keep changes minimal and fail closed.
7. Merge/push the accepted result to remote `main` through the governed dev-dispatch merger and provide a merge/deployment receipt identifying the accepted commit, remote-main commit, production fast-forward/pull status if that deployment action is available to the governed merger, and production MCP restart/health status. Do not claim deployment actions that were not actually observed.

## Acceptance evidence requirements

The acceptance output itself is part of the requested evidence. It must contain this run's real UTC timestamp and raw echoed payloads/results proving all four scenarios. Tests alone or synthesized prose are insufficient. Acceptance and git cleanliness checks must execute only in an independent `/data/worktrees` worktree.

```dd-acceptance
uv run pytest tests/test_work_folder_reconcile.py tests/test_dd_service.py
uv run ruff check src tests scripts/reconcile_source_binding_acceptance.py
uv run python scripts/reconcile_source_binding_acceptance.py
```
