"""Domain code lifted from loop-engine-development-mcp.

Vendored rather than re-implemented: this is 30k lines of hard-won git and
handoff discipline, and the refactor's premise is that the *orchestration
shell* is what needs rewriting, not the domain assets underneath it.

Provenance: loop-engine-development-mcp, branch fix/dd-rootfix-20260824.
That branch, not main, is what production runs -- see the spike note in
wf-3f30cd findings. Re-vendoring must record the source commit.

- `git_ops.py` + `external_ops.py`: exact-workspace git discipline.
- `plugin_adapter.py` (from 8fc5a4ea5a31dbd608affbd97171ef4174d57d6e): the
  pinned plugin capability check and the two materializer invocations. Worth
  naming what this file revealed: **the per-stage sealer is not dd code at
  all**. `stage_handoffs.py` is a 79-line transport that shells out to a
  script in the dev-dispatch plugin bundle, gated by a digest lock. The
  authoritative commit has always been produced across a process boundary --
  which is the fourth anti-lock-in invariant, already satisfied upstream. So
  fleet-graph invokes the same primitive rather than reimplementing sealing.

Local edits are kept to the minimum needed to make the import graph resolve,
so a future diff against upstream stays readable. What the adapter needed was
one import block, redirected at `dd/upstream_constants.py`.
"""
