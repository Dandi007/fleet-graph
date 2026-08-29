# Narrow correction: governed work-folder residue reconciliation and B3 findings

## Trigger and boundary

This corrective development is prompted by two newly observed facts:

1. A real MCP invocation of `wf_reconcile` returned the raw error `Unknown tool: wf_reconcile`.
2. Parent work folder `wf-a87b04` has no `findings.md`, so its required B3 phenomenon -> mechanism -> evidence chain is not closed.

This is a narrow correction. Do not implement B4 observability, deployment, unrelated development adoption, or broad refactors. Do not weaken clean-tree, CAS, scope, lease, receipt, idempotency, or immutable-evidence safeguards. Do not expose, return, log, or require a physical work-folder data-repository path. All implementation and all review are performed by dev-dispatch actors.

## Required externally observable behavior

### R1: Safe automatic reconciliation

For a governed work folder whose only residue is a tracked, append-only extension to an allowed bookkeeping file (including `progress.md`), with a readable diff and no conflict markers:

- reconciliation classifies the residue as attributable and safely adoptable;
- it atomically adopts the exact appended bytes into governed history;
- it preserves all appended content, produces an auditable immutable receipt, and returns the folder to a clean governed state;
- replay is idempotent and cannot duplicate content or fork history.

Refuse closed for deletion, replacement, prepend/mid-file edits, binary/unreadable diff, untracked residue, conflict markers, cross-folder changes, dirty governance control files, or ambiguous/mixed residue. Tests must prove at least the pure-append success case and representative refusal cases.

### R2: MCP-only human recovery exit

Expose the recovery operation as a real MCP tool named `wf_reconcile` (or, only if an already-established public contract requires a more precise name, retain a compatible real `wf_reconcile` entry point). It must support a two-step flow:

1. dry-run returns a stable plan with opaque `folder_id`, logical filenames, classifications, content/diff digests, and a confirmation token or equivalent CAS binding, while making no mutation;
2. confirmed execution is accepted only when bound to that exact dry-run plan/current base and then performs the governed adoption.

The response and errors must never expose the physical data-repository root or require client-side Git commands. Stale confirmation, changed bytes/base, unsafe residue, or wrong folder must refuse without mutation. Register the tool in the actual MCP server surface and add a protocol-level test that lists/calls the real tool, so a locally present helper with an unregistered server route cannot pass.

### R3: Closed B3 findings artifact

Create a durable repository findings artifact for this incident, named `docs/findings/work-folder-residue-reconciliation.md` unless repository conventions require a more precise existing findings location. It must close the chain with concrete immutable anchors:

- Phenomenon: the original append residue produced prolonged `WORKTREE_DIRTY`, and the attempted governed exit returned `Unknown tool: wf_reconcile`.
- Mechanism: identify exactly how append bytes escaped or outlived a governed transaction, why the old whole-repository/clean guard classified them as un-attributable, and why the MCP registration/control path was absent.
- Evidence: cite the deterministic fixtures/tests, mechanism symbols, receipt/digest assertions, MCP tool-list/call assertion, and refusal assertions that prove the correction. Distinguish proven facts from remaining hypotheses.

The artifact must be self-contained enough to be copied into parent `wf-a87b04/findings.md` after approval without inventing any missing claim.

## Mechanical acceptance requirements

Tests must demonstrate all of the following:

1. Tracked, text, pure append, no-conflict residue is dry-run classified as adoptable and confirmed execution adopts exact bytes, records immutable evidence, leaves clean state, and is idempotent on replay.
2. At least deletion/rewrite or conflict-marker residue refuses closed and remains byte-for-byte unchanged.
3. Dry-run has no mutation; stale or mismatched confirmation refuses without mutation.
4. MCP tool discovery contains `wf_reconcile`; a real protocol-level call exercises dry-run then confirmed execution.
5. Every public payload and error is asserted not to contain the physical data-repository path.
6. The B3 findings document contains explicit Phenomenon, Mechanism, Evidence sections and immutable test/symbol anchors.
7. Existing verification remains green.

```dd-acceptance
uv sync --frozen
make verify
```
