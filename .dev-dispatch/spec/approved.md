# Review subject binding must survive metadata-only materialization commits

All implementation and every code review must be performed by dev-dispatch in this isolated H0 worktree only.

## Verified incident (2026-08-28, dev-fg-5aae3dd96e49)

continuous_review REJECTED an implement handoff on a purely procedural ground:
the handoff's recorded `input_commit`/`work_head_commit` (e8826333) does not
equal the review's `subject_commit` (a4250349). The implement actor DISPUTED
with a mechanically verified argument, confirmed independently by the
supervisor: a4250349 is the materializer's own metadata commit
("dev-dispatch: materialize implement ...") stacked on top of e8826333, and
`git diff --exit-code e8826333 a4250349 -- src tests profiles bin deploy
scripts` exits 0 — the product trees are byte-identical. The SHA inequality is
produced by the control plane itself, so the procedural check rejects every
correctly-behaving development whose materialization adds a metadata commit.

## Required behavior

1. The subject binding presented to reviewers (and any chain/audit check that
   compares a handoff's product commit against a review subject) must be
   product-consistent: either bind the review subject to the product commit
   itself, or record both commits and define the procedural check as
   "product-path tree equality across the metadata materialization commit"
   (the exact product path set used elsewhere in the plugin contract).
2. A genuine mismatch — product trees that actually differ — must still be
   rejected exactly as today. Do not weaken the binding into prose.
3. Existing receipts, replay semantics, and the rework chain rules must remain
   valid; if the canonical digest chain references subject commits, keep the
   linkage well-defined for both old and new bindings.

## Required tests

- Regression reproducing the incident shape: implement product commit +
  metadata-only materialization commit on top → procedural binding check
  passes; reviewer input carries a consistent subject.
- Negative: materialization commit whose product tree differs from the
  handoff's work_head → rejected.
- Full suite green.

## Constraints

- No changes to supervise/, bus protocol, or scheduler.
- Do not modify production main checkout or deploy in this development.

## Acceptance

```dd-acceptance
make verify
```
