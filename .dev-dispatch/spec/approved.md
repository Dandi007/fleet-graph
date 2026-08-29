# Work item 1: GitWorkFolderSource single-repository subdirectory layout

## Objective

Fix only the governed-base read path for the production work-folder layout: one Git repository contains each opaque folder as a subdirectory, and the resolver returns that folder subdirectory. `GitWorkFolderSource.inspect()` must read committed blobs relative to that resolver-returned cwd instead of treating logical filenames as repository-root paths. This prevents valid governed files from receiving `base=None` and being classified ambiguous solely because the folder is a monorepo subdirectory.

## Required implementation

- In `src/fleet_graph/dd/work_folder_store.py`, make the smallest correct change to `_governed_base`: address the blob using Git's cwd-relative revision syntax (`HEAD:./{filename}`) when invoking Git with the resolved folder directory as cwd.
- Preserve all existing fail-closed behavior: a genuine Git/blob read failure still returns `None`; unsafe paths, opaque folder handling, working-byte reads, reconciliation classification, and adoption semantics must not be loosened.
- Do not implement work item 2 (locking or governed transaction changes), do not refactor `adopt()`, and do not broaden the patch beyond code/docs/tests directly needed for this layout fix.

## Mandatory regression fixture

Add `TestGovernedGitSource.test_monorepo_subdirectory_reads_cwd_relative_governed_base` to `tests/test_work_folder_reconcile.py`. It must be a real disposable Git fixture with this exact topology:

- one Git repository initialized and committed at the fixture root;
- a governed folder such as `wf-governed/` below that repository root (not a nested Git repository);
- at least `wf-governed/progress.md` committed in the repository, with append-only working bytes after the commit;
- the `GitWorkFolderSource` resolver returns the `wf-governed/` subdirectory.

The regression must prove behavior, not merely mock argv: assert the inspected `progress.md` has the exact committed bytes as `base`, the exact appended working bytes as `current`, `tracked=True`, and `base is not None`. It must also pass the inspection through `WorkFolderReconciler.plan` and assert `progress.md` is adoptable rather than ambiguous. Keep existing repo-per-folder coverage intact.

## Boundaries and governance

- All implementation, verification, and code review are owned by dev-dispatch in this development.
- Work only in the dedicated `/data/worktrees/fleet-graph-wf-a87b04-monorepo-layout-20260829` handoff worktree or dev-dispatch-created independent `/data/worktrees` verification worktrees. `initial_handoff.worktree_path` must never point at `/data/code/self/fleet-graph` or any production checkout.
- Never checkout, switch, reset, detach, edit, or verify in `/data/code/self/fleet-graph`.
- The durable `dd/*` branch must not be merged directly to `main`; stop after the governed gate and wait for the supervision plane to harvest it.
- Do not deploy or restart `fleet-graph-dd-mcp` (and in particular do not do so while `dev-fg-26909eac1867` is non-terminal).

## Done criteria

- The mandatory real single-repository/subdirectory regression passes and would fail with `HEAD:{filename}`.
- Existing governed-source reconciliation tests remain green.
- Ruff passes on the touched source and test.
- Independent dev-dispatch code review finds no correctness or boundary issue.

```dd-acceptance
uv sync --frozen
uv run pytest -q tests/test_work_folder_reconcile.py::TestGovernedGitSource::test_monorepo_subdirectory_reads_cwd_relative_governed_base
uv run pytest -q tests/test_work_folder_reconcile.py
uv run ruff check src/fleet_graph/dd/work_folder_store.py tests/test_work_folder_reconcile.py
```