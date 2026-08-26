"""Domain code lifted from loop-engine-development-mcp.

Vendored rather than re-implemented: this is 30k lines of hard-won git and
handoff discipline, and the refactor's premise is that the *orchestration
shell* is what needs rewriting, not the domain assets underneath it.

Provenance: loop-engine-development-mcp, branch fix/dd-rootfix-20260824.
That branch, not main, is what production runs -- see the spike note in
wf-3f30cd findings. Re-vendoring must record the source commit.

Local edits are kept to the minimum needed to make the import graph resolve,
so a future diff against upstream stays readable.
"""
