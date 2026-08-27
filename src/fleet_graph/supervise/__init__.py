"""The supervision face: read, verify, report. Never decide.

Two tools live here. `inbox` renders the pending-verdict view straight off the
board -- the inbox is a view, not a store (r4-design §4): a question note with
no decision referencing it *is* the pending state, and no second copy of it
exists to drift. `audit` mechanises the evidence checklist a human was running
by hand (r4-design §2, gather_evidence + rerun_acceptance): receipts, commit
chains, identity bindings, and a one-shot independent re-run of the frozen
acceptance argv.

Neither tool publishes a `work.decision.v1`, and nothing in this package can:
verdicts stay the human's (bus/board.py module rule). The audit's only write
anywhere is an `evidence` note.
"""
