# Spec ⑮-b (wf-8d9737): old-code red / new-code green dual-run evidence

Spec ⑮-b's acceptance clause requires the implementer to deliver the echo of
the double run that proves the red target is *red on the old code* and *green
on the new code*: on base `7f20b340a69b` the red-target cases of
`tests/test_15b_gate_reject_source_binding.py` must FAIL (the old engine
silently dispatched an unbound rework -- the dev-fg-eee4da1e3649 g3 live
defect), and on the delivered tree the same file must pass. This file is that
echo, durably committed. The graded verification for any attempt remains the
attempt envelope's `verification_record` (final-state runs only); this report
is secondary, human-readable evidence for the one run that by definition
cannot be a final-state run: the run against the pre-fix base.

## Run A -- old code (base 7f20b340a69b) must be RED

Base tree extracted read-only from git history (no checkout of any protected
worktree), the delivered test file copied in unchanged, then the spec's test
command run against the base sources:

```text
$ git archive 7f20b340a69bb8e2ed29964c9abff5a54419cd09 | tar -x -C <base-dir>
$ cp tests/test_15b_gate_reject_source_binding.py <base-dir>/tests/
$ cd <base-dir> && uv sync --frozen && uv run pytest -q tests/test_15b_gate_reject_source_binding.py
exit code: 1
...
E       Failed: DID NOT RAISE PromptError
E       Failed: DID NOT RAISE ControlPlaneError
E       Failed: DID NOT RAISE ControlPlaneError
E       Failed: DID NOT RAISE RuntimeError
...
FAILED tests/test_15b_gate_reject_source_binding.py::TestSourceRebind::test_gate_reject_json_is_bound_to_the_board_decision
FAILED tests/test_15b_gate_reject_source_binding.py::TestFullTextUnderTheAnchor::test_the_implement_prompt_carries_the_full_rationale_and_message_id
FAILED tests/test_15b_gate_reject_source_binding.py::TestFullTextUnderTheAnchor::test_the_anchor_renderer_refuses_an_unbound_payload
FAILED tests/test_15b_gate_reject_source_binding.py::TestUnboundVerdictRefusesDispatch::test_an_empty_decision_message_id_refuses_the_start
FAILED tests/test_15b_gate_reject_source_binding.py::TestUnboundVerdictRefusesDispatch::test_a_missing_verdict_record_refuses_instead_of_terminal_facts
FAILED tests/test_15b_gate_reject_source_binding.py::TestUnboundVerdictRefusesDispatch::test_the_dispatch_face_refuses_an_unbound_mandate_file
6 failed, 1 passed
```

The failing shapes are exactly the g3 defect pinned by board 2815: the two
red-target start cases fail with `DID NOT RAISE ControlPlaneError` -- the old
`_seal_gate_rework` silently dispatched the unbound rework (empty
`decision_message_id`, and the `terminal-facts` fallback for a missing
record) instead of refusing with `REWORK_DECISION_UNBOUND`; the prompt layer
rendered a hollow task book (`DID NOT RAISE PromptError`); and `dd run`'s
dispatch face accepted an unbound `--gate-reject-file` payload
(`DID NOT RAISE RuntimeError`).

## Run B -- delivered tree must be GREEN

The same file on the delivered sources, via the spec's acceptance command:

```text
$ uv sync --frozen && uv run pytest -q tests/test_15b_gate_reject_source_binding.py
exit code: 0
7 passed
```

The second acceptance command, `make verify`, is green on the delivered tree
as well (ruff clean, full suite green, conformance checks clean); the
authoritative exit codes for the delivered tree live in the attempt's
`verification_record`, re-run at the final head commit.
