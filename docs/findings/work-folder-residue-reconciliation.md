# work-folder residue reconciliation — B3 findings

> Narrow correction for parent work folder `wf-a87b04` (alias `ronin-wf-robust`,
> "work-folder 治理层健壮性"). Self-contained so it can be copied into
> `wf-a87b04/findings.md` after approval without inventing any missing claim.

## Phenomenon

Three newly observed facts prompted this corrective development:

1. A real MCP invocation of `wf_reconcile` returned the raw error
   `Unknown tool: wf_reconcile`.
2. The parent work folder had a tracked, append-only extension to an allowed
   bookkeeping file (`progress.md`) that outlived its governed transaction.
3. `RECONCILE_SOURCE_UNBOUND`: even after `wf_reconcile` was registered on the
   surface, `serve()` constructed the MCP server without a concrete
   `ReconcileSource`, so the registered-but-unbound tool refused real calls with
   `RECONCILE_SOURCE_UNBOUND` instead of reconciling live work-folder residue.

Because of that residue, the whole-repository/clean guard — `_admit_repo` in
`src/fleet_graph/dd/control_plane.py`, which runs `git status --porcelain` and
refuses any non-empty output as `WORKTREE_DIRTY` — classified the folder as
un-attributable. The result was a prolonged `WORKTREE_DIRTY` livelock: the folder
could be neither admitted nor exited, and the intended governed exit (`wf_reconcile`)
did not exist on the MCP surface to dissolve it.

## Mechanism

Exactly how the append escaped and why the old guard could not attribute it:

- **No reconciliation step existed.** Bookkeeping appends landed directly in the
  working tree, and the only gate on that state was an admit-time whole-repo clean
  check. Nothing ever asked *what* changed or *whether it was safe*, so a harmless
  append and an unsafe rewrite were indistinguishable.
- **The clean guard answered a binary question.** `git status --porcelain` non-empty
  → refuse. A tracked, text, pure append to `progress.md` produced exactly the same
  `WORKTREE_DIRTY` refusal as a deletion, replacement, prepend, mid-file edit, or
  conflict marker — un-attributable by construction, not by choice.
- **The MCP control path was absent.** The dev-dispatch surface registered only the
  consumed development family (`development_*`) plus explicitly-refused legacy names.
  No `wf_reconcile` route was registered, so a locally present helper had no server
  route and any real invocation failed as `Unknown tool`.

The correction restores a governed exit with a deterministic seam:

- `src/fleet_graph/dd/reconcile.py` — `WorkFolderReconciler` and `classify_file`.
  Only a tracked, text, pure-append, no-conflict residue to an allowed bookkeeping
  file is `adoptable`; every other shape refuses closed (`deletion`, `rewrite`
  (replacement / prepend / mid-file edit), `conflict`, `untracked`, `binary`,
  `cross_folder`, `dirty_control`, `ambiguous`).
- `plan` is the dry-run: no mutation; it returns opaque `folder_id`, logical
  filenames, classifications, content/base/appended digests, and a `token` that is
  a CAS binding over the exact base and bytes.
- `confirm` adopts the exact appended bytes through the source seam only when the
  token still binds to the current base/bytes, seals a receipt
  (`RECONCILE_MECHANISM = "WorkFolderReconciler.adopt"`), and replays idempotently
  from a token-keyed ledger — never duplicating content or forking history.
- `src/fleet_graph/dd/service.py` — `wf_reconcile` is registered on the real MCP
  surface (`build_mcp_server(..., work_folders=...)`), the same surface the B2
  `development_adopt` / `development_recover` tools are on.

### Root cause of `RECONCILE_SOURCE_UNBOUND`

The seam was defined and the tool was registered, but `serve()` never wired the
two together: it called `build_mcp_server(control)` with no `work_folders`, so the
`wf_reconcile` handler took its `source is None` branch and returned
`RECONCILE_SOURCE_UNBOUND` for every real call. The consequence is that the
governed exit existed by name but was structurally unusable in production.

The correction binds a concrete production `ReconcileSource` in the `serve()`
construction path (`fleet_graph.dd.work_folder_store.GitWorkFolderSource`, built by
`governed_work_folder_store`):

- **Read side** — `inspect(folder_id)` walks the governed repository tree
  (`git ls-tree HEAD`) for each logical filename's committed base and reads the
  working tree for its current bytes, and lists non-governed files as untracked
  residue. A deleted file reports `current=None`; an unreadable governed blob
  reports `base=None` so the reconciler classifies it `ambiguous` and refuses.
- **Write side** — `adopt(folder_id, entries)` re-checks the append-only CAS
  binding at the store (the working bytes must still equal `base + appended`),
  writes the exact bytes, and commits them atomically, returning a receipt that
  names logical files and never a path.
- **Opaque addressing** — the physical repository stays behind the `resolve` seam
  and never crosses back. A `folder_id` that does not look like an opaque token,
  an unresolvable folder, or any git failure all refuse closed without mutation.
- **Fail-closed without a root** — `serve()` reads the store root from
  `--work-folder-root` / `FLEET_GRAPH_WORK_FOLDER_ROOT`; when it is absent the
  bound source is still concrete and refuses per-folder with `RECONCILE_REFUSED`,
  never `RECONCILE_SOURCE_UNBOUND` and never a silent no-op route.

## Evidence

Proven facts, each pinned to a deterministic test in
`tests/test_work_folder_reconcile.py`:

| Proven fact | Test anchor |
|---|---|
| Pure append classifies `adoptable`; confirmed execution adopts the exact appended bytes and records an immutable receipt (`mechanism`, `digest`, `adopted[].appended_digest`); folder returns clean; replay is idempotent. | `test_dry_run_classifies_and_mutates_nothing`, `test_confirmed_execution_adopts_exact_bytes_and_records_evidence`, `test_replay_is_idempotent_and_never_forks` |
| Deletion / rewrite / conflict / untracked / binary / cross-folder / dirty-control residue refuses closed and remains byte-for-byte unchanged. | `test_residue_refuses_closed_and_remains_byte_for_byte_unchanged` |
| Dry-run makes no mutation; a stale, changed, or wrong-folder confirmation refuses without mutation. | `test_stale_or_changed_confirmation_refuses_without_mutation`, `test_wrong_folder_refuses_without_mutation` |
| `wf_reconcile` appears in real MCP tool discovery; a protocol-level call drives dry-run then confirmed execution over the wire. | `test_wf_reconcile_is_listed_and_drives_dry_run_then_confirm` |
| No public payload or error exposes a physical data-repository path. | `test_refusal_over_the_wire_carries_no_physical_path`, `test_an_unbound_server_refuses_explicitly` |
| The concrete `GitWorkFolderSource` derives governed base vs working bytes from a real repository, commits exactly the appended bytes, reports untracked residue, and refuses rewrites without mutation. | `TestGovernedGitSource::test_inspect_derives_governed_base_and_working_bytes`, `test_adopt_commits_exactly_the_appended_bytes`, `test_untracked_residue_is_reported_as_untracked`, `test_rewrite_refuses_closed_without_mutation` |
| Opaque addressing holds at the store for unknown and path-like folder ids. | `test_an_unknown_folder_refuses_without_disclosing_a_path`, `test_a_path_like_folder_id_is_refused_as_opaque` |
| The production construction binds a concrete source even without a configured root; an unconfigured store refuses with `RECONCILE_REFUSED`, never `RECONCILE_SOURCE_UNBOUND`. | `TestReconcileSourceBinding::test_the_production_store_is_concrete_even_without_a_root`, `test_a_bound_but_unconfigured_store_refuses_not_unbound` |
| The real MCP surface with the concrete source satisfies all four end-to-end scenarios (safe dry-run, CAS confirmation, stale-token refusal, unsafe-residue refusal) with before/after byte identity, printing a fresh UTC timestamp and raw request/response evidence. | `scripts/reconcile_source_binding_acceptance.py` |

Immutable anchors referenced above: mechanism symbol `RECONCILE_MECHANISM`
(`WorkFolderReconciler.adopt`), the classification constants
(`CLS_ADOPTABLE`, `CLS_REWRITE`, `CLS_CONFLICT`, `CLS_DELETION`, `CLS_UNTRACKED`,
`CLS_BINARY`, `CLS_CROSS_FOLDER`, `CLS_DIRTY_CONTROL`), the digest helper
`compute_digest`/`compute_json_digest`, the MCP tool name `wf_reconcile`, and the
concrete source `GitWorkFolderSource` / `governed_work_folder_store`.

Remaining hypotheses (explicitly *not* proven by this correction, distinguished
from the facts above): the exact physical location the production `serve()`
deployment resolves a live `folder_id` to (the store root is supplied via
`FLEET_GRAPH_WORK_FOLDER_ROOT` / `--work-folder-root`, and the production value is a
deployment decision, not a code fact). The concrete source, the binding, the CAS
contract, and the refusal disciplines are all proven; only the deployed root value
is configuration that this correction does not invent.