# Work item 2: make reconciliation adoption compatible with the governed write gate

## Confirmed baseline

- Production layout is one Git repository with opaque work folders as subdirectories. Main `8242daebaac9bc5038f623d90c36828c3fda763f` has harvested `dev-fg-30957d025e47`; `_governed_base` now uses `HEAD:./{filename}` and its real monorepo fixture passed.
- `GitWorkFolderSource.adopt()` still performs its byte CAS, writes, `git add`, and `git commit` directly, without the governed repository's cross-process mutation lock.
- The canonical work-folder governed writer serializes every mutation with an exclusive `fcntl.flock` on `<git-common-dir>/katana-governed.lock`, resolving the common directory via `git rev-parse --git-common-dir`. Its mutation contract then checks CAS/cleanliness, journals declared paths, commits, and emits auditable mutation evidence while still holding that lock.
- Therefore reconciliation can currently interleave with an MCP governed mutation between its byte check, writes, staging, and commit. That can bypass the write gate, overwrite a winner, stage another process's bytes, or leave residue that neither transaction can attribute.

## Objective

Make `GitWorkFolderSource.adopt()` protocol-compatible with the same cross-process governed repository lock, while preserving the existing reconciliation API and all fail-closed, append-only, atomicity, and path-redaction guarantees. This item is only write-gate compatibility; do not deploy, bind the production root, or broaden into unrelated reconciliation behavior.

## Required implementation

1. Implement or reuse a small repository mutation lock compatible with the canonical governed writer:
   - resolve the Git common directory from the resolver-returned folder/subdirectory using `git rev-parse --git-common-dir`;
   - lock exactly `<resolved-git-common-dir>/katana-governed.lock` with exclusive `fcntl.flock`, so a folder-subdirectory resolver and the repository-root MCP process contend on the same inode;
   - use bounded wait/poll behavior and convert resolution/open/acquisition timeout failures into `ReconcileError` with no mutation and no physical path disclosure;
   - never fall back to unlocked adoption.
2. Hold that lock across the complete adoption critical section: filename validation, current-byte/append-only CAS revalidation, writes, staging, commit, and receipt construction. In particular, no byte check may occur before lock acquisition and then be trusted after waiting.
3. Preserve atomic/fail-closed semantics:
   - only the confirmed logical filenames may be staged and committed;
   - exact bytes remain `base + appended`;
   - a concurrent winner that changes HEAD or working bytes causes the loser to refuse without overwriting, staging, committing, or cleaning the winner;
   - successful adoption leaves no tracked/staged/untracked residue in its scope and creates exactly one adoption commit;
   - lock failure, Git failure, or CAS mismatch must not report success.
4. Preserve audit/security semantics:
   - keep the reconciler's existing immutable confirmation token and second CAS check;
   - keep the dedicated reconciler commit identity/message and logical `committed_files` receipt, with no physical backing path in results/errors;
   - do not weaken the MCP governed transaction's CAS, cleanliness, journal, ledger, idempotency, or audit-trailer rules;
   - document the compatibility boundary and why sharing the lock, rather than bypassing the governed write gate, is required.

## Mandatory real concurrency regression

Add `tests/test_work_folder_write_gate.py` using a real disposable single Git repository with `wf-governed/` as a subdirectory and real OS processes (not only threads or mocked call ordering). A process that represents the MCP governed writer must acquire the canonical `<git-common-dir>/katana-governed.lock`; reconciliation must use the production `GitWorkFolderSource.adopt()` path.

The tests must deterministically prove both serialization and loser safety:

- `test_adopt_waits_for_cross_process_governed_lock`: while the governed-writer process holds the lock, adoption cannot enter its byte-check/write/add/commit critical section; after release it succeeds, commits exactly the append, returns only logical audit fields, and leaves the repository clean.
- `test_concurrent_governed_winner_is_never_overwritten_or_left_as_residue`: arrange for the governed writer to win under the shared lock and commit a competing/current state before adoption acquires it. Adoption must re-read under lock and refuse closed. Assert the governed winner's HEAD and bytes remain exact, the index/worktree are clean, there is no extra adoption commit, and no untracked or staged residue exists.
- At least one assertion must prove repo-root and folder-subdirectory participants resolve/contend on the same Git-common-dir lock, preventing a false test that uses two unrelated lock files.
- Use bounded process joins/timeouts so a broken lock fails the test instead of hanging acceptance.

Existing repo-per-folder and monorepo governed-source tests must remain green.

## Governance boundaries

- All business-code implementation and both continuous/final code review belong to dev-dispatch.
- Work only in `/data/worktrees/fleet-graph-wf-a87b04-write-gate-20260829` or dev-dispatch-created independent `/data/worktrees` verification worktrees. Never run Git inspection or verification in a production main checkout.
- Do not checkout, switch, reset, detach, write, verify, directly merge a durable `dd/*` branch, or deploy from `/data/code/self/fleet-graph`.
- Stop at the governed development terminal and leave harvesting/main integration and deployment to the supervision plane.

## Done criteria

- Both mandatory real cross-process concurrency tests pass and would fail if `adopt()` did not take the canonical lock or trusted a pre-lock CAS.
- The complete existing reconciliation suite passes.
- Ruff passes for all touched source and tests.
- Dev-dispatch continuous and final reviewers confirm no relaxation of atomicity, auditability, path secrecy, CAS, or governed transaction semantics.

```dd-acceptance
uv sync --frozen
uv run pytest -q tests/test_work_folder_write_gate.py
uv run pytest -q tests/test_work_folder_reconcile.py tests/test_work_folder_write_gate.py
uv run ruff check src/fleet_graph/dd/work_folder_store.py tests/test_work_folder_reconcile.py tests/test_work_folder_write_gate.py
```