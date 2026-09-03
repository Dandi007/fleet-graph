"""M3: the dispatching line's self-gate -- six duties, then the verdict.

The gate belongs to the dispatching line (design §6.3 "DD 闸不经人"): when the
line's ``dd_awaiting_gate(dev_id)`` wake fact fires (M1), the line performs the
six evidence duties itself and delivers ``APPROVE``/``REJECT`` through M2's dd
single path (:func:`fleet_graph.decision_mcp.deliver_decision`). ``decided_by``
is the line's own principal, and the delivery path validates it against the
single's ``record.json.dispatched_by`` -- a line can only rule on the singles
it dispatched, which is the authorization model the whole self-gate stands on
(S11).

The six duties (spec §2) are the gate's mandatory fields; missing any one is a
REJECT with the failing duties named:

1. three-way acceptance argv equality (spec == record == stage receipt),
2. the product diff stays inside the spec's declared surface,
3. zero test deletions in ``base..head``,
4. the acceptance commands re-run personally at the gate, echo captured,
5. the final_review mutation receipt verifies against a mechanical
   enumeration -- verified only, never re-run (S12.3),
6. full regression against the *frozen* ``target_base_commit`` baseline:
   the red set must not grow and any green->red flip refuses (S9).
"""

from fleet_graph.self_gate.runner import (
    DUTY_ACCEPTANCE_RUN,
    DUTY_ACCEPTANCE_THREE_WAY,
    DUTY_MUTATION_RECEIPT,
    DUTY_PRODUCT_DIFF_BOUNDARY,
    DUTY_REGRESSION_BASELINE,
    DUTY_ZERO_TEST_DELETIONS,
    EVIDENCE_VERSION,
    SelfGateDecision,
    deliver_self_gate_decision,
    duty_acceptance_run,
    duty_acceptance_three_way,
    duty_mutation_receipt,
    duty_product_diff_boundary,
    duty_regression_baseline,
    duty_zero_test_deletions,
    handle_dd_awaiting_gate_wake,
)

__all__ = [
    "DUTY_ACCEPTANCE_RUN",
    "DUTY_ACCEPTANCE_THREE_WAY",
    "DUTY_MUTATION_RECEIPT",
    "DUTY_PRODUCT_DIFF_BOUNDARY",
    "DUTY_REGRESSION_BASELINE",
    "DUTY_ZERO_TEST_DELETIONS",
    "EVIDENCE_VERSION",
    "SelfGateDecision",
    "deliver_self_gate_decision",
    "duty_acceptance_run",
    "duty_acceptance_three_way",
    "duty_mutation_receipt",
    "duty_product_diff_boundary",
    "duty_regression_baseline",
    "duty_zero_test_deletions",
    "handle_dd_awaiting_gate_wake",
]
