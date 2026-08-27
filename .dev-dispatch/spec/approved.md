# A1 New DD Real-Spec Verification

## Goal

Add `docs/a1-new-dd-real-spec-verification.md` containing exactly the heading `# A1 New DD Real-Spec Verification` and one sentence stating that this change is a durable-MR verification fixture for the fleet-graph DD service.

## Constraints

- Change only that new documentation file.
- Do not modify services, deployment configuration, credentials, or legacy-engine units.
- Preserve all existing behavior.

## Acceptance

```dd-acceptance
make verify
```