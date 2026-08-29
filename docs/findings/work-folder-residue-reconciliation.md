# work-folder residue reconciliation — B3 findings

> Narrow correction for parent work folder `wf-a87b04` (alias `ronin-wf-robust`,
> "work-folder 治理层健壮性"). Self-contained so it can be copied into
> `wf-a87b04/findings.md` after approval without inventing any missing claim.

## Phenomenon

Two newly observed facts prompted this corrective development:

1. A real MCP invocation of `wf_reconcile` returned the raw error
   `Unknown tool: wf_reconcile`.
2. The parent work folder had a tracked, append-only extension to an allowed
   bookkeeping file (`progress.md`) that outlived its governed transaction.

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

Immutable anchors referenced above: mechanism symbol `RECONCILE_MECHANISM`
(`WorkFolderReconciler.adopt`), the classification constants
(`CLS_ADOPTABLE`, `CLS_REWRITE`, `CLS_CONFLICT`, `CLS_DELETION`, `CLS_UNTRACKED`,
`CLS_BINARY`, `CLS_CROSS_FOLDER`, `CLS_DIRTY_CONTROL`), the digest helper
`compute_digest`/`compute_json_digest`, and the MCP tool name `wf_reconcile`.

Remaining hypotheses (explicitly *not* proven by this correction, distinguished
from the facts above): the production wiring of the `work_folders` source seam to
the live work-folder store (katana work-folder MCP). A server built without a
bound source refuses with `RECONCILE_SOURCE_UNBOUND`; deriving the governed
base-vs-current inspection against the live store is a separate follow-up, not part
of this correction's accepted behaviour.