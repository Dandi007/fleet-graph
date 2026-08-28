# F6: real bus refs interface minimal correction

## Objective
Implement the smallest correct fix for the F6 discrepancy between the resolver's assumed bus interface and the real bus refs endpoint. Establish the actual endpoint contract from repository code and runtime-facing tests; do not preserve an incorrect mock-only interface.

## Delegation boundary
All business-code implementation, test-code changes, and code review are performed exclusively by dev-dispatch agents. The coordinator must not write business code or conduct the review.

## Scope and constraints
- Work only in this dedicated H0 worktree.
- Base is frozen at origin/main commit c3393ce9e90a5243ab7666653ea1ed09354b4ede.
- Make the minimal compatibility/correctness change required by the real refs endpoint.
- Add or adjust focused resolver coverage that exercises the real refs endpoint contract, not just a mock shape.
- Do not merge, deploy, restart any unit, or alter the production checkout.

## Required acceptance evidence
For every command or live drill, record in durable development evidence: UTC start/end, exact argv, exit code, and unabridged raw trailing output.

1. Run `uv sync --frozen`.
2. Run `make verify`.
3. Run the focused resolver test(s), using the repository's exact resolver test selector/path discovered by the implementer.
4. Exercise the real bus refs endpoint, not a mocked adapter: publish a valid human `work.decision.v1` whose refs contain the actual gate question-note entity id, invoke resume, and demonstrate decision-to-resume latency under 5 seconds. Record the real entity/message identifiers, UTC timestamps, argv/API invocation, exit status, and raw tail.
5. Perform a restart-idempotency drill against the same development/run context: demonstrate that a repeated/restarted invocation re-adopts rather than duplicates sealed work or dispatches a second run. Record identifiers, UTC timestamps, argv/API invocation, exit status, and raw tail.
6. An independent dev-dispatch code-review stage must review the final diff and report findings or explicit approval in the durable evidence.

```dd-acceptance
uv sync --frozen
make verify
uv run pytest -q -k resolver
```
