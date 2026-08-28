# B1-B3: Isolated Scope, Automatic Adoption, and Evidence-Backed Recovery

## Boundary

This is a new, standalone development rooted directly at target base
`afb43460866477903e5444046d877b440240cbe2`. It must not reuse, continue,
or depend on `katana#150` or `katana#151`.

Only B1, B2, and B3 are in scope. B4 is explicitly deferred and must not be
implemented, enabled, tested as a requirement, or otherwise pulled into this
development.

All production-code changes and all code review must be performed by
`dev-dispatch` actors. The coordinating worker must not directly author or
review business code.

## B1: Scope Isolation

Make the B1 scope a hard boundary in the fleet-graph development workflow.
The system must reject or quarantine work that crosses the declared B1-B3
boundary, including references or handoffs that attempt to add B4 or revive
`katana#150`/`katana#151`. The rejection must be attributable to the declared
scope rule, not merely an incidental downstream failure.

## B2: Automatic Adoption and MCP Human Recovery

When eligible in-flight or recoverable work is discovered, automatically adopt
it into the governed workflow rather than requiring a manual bookkeeping
intervention. Adoption must be idempotent: replaying the same discovery cannot
create duplicate adopted work or fork its history.

When automation cannot safely resolve the situation, expose a deliberate human
recovery exit through the MCP control surface. The exit must be authenticated
by the existing MCP/governance path, record the human decision and its target
in the immutable evidence trail, and allow the suspended work to resume only
from that recorded decision. It must not create a bypass around normal scope,
lease, or receipt validation.

## B3: Mechanical Phenomenon-to-Mechanism-to-Evidence Acceptance

For each B1/B2 behavior, acceptance must be mechanically checkable as a linked
chain:

1. A deterministic test or fixture demonstrates the externally observable
   phenomenon.
2. The test identifies the governing mechanism that enforces or recovers it.
3. The resulting artifact/receipt/event is asserted as evidence of that exact
   mechanism and is bound to the tested subject.

The checks must fail if any link is removed, substituted with an unrelated
event, or if a human-recovery decision has no immutable target reference.
Tests must include both success and refusal cases for B1, idempotent replay for
B2 adoption, and a suspended-to-resumed MCP human-recovery case. Do not accept
log text, a mocked success flag, or an unbound receipt as sufficient evidence.

## Non-Goals

- Do not implement B4.
- Do not migrate, repair, or extend `katana#150` or `katana#151`.
- Do not loosen existing scope, lease, receipt, actor, or human-decision
  safeguards to make the new paths pass.

## Completion Criteria

1. Focused regression tests cover the B1, B2, and B3 cases above.
2. Existing verification remains green.
3. The final review independently verifies that the code and tests implement
   only this spec and identifies the mechanism and immutable evidence for each
   phenomenon.

```dd-acceptance
uv sync --frozen
make verify
```
