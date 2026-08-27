# A1 New DD Real-Spec Verification 2

## Goal

Add `docs/a1-new-dd-real-spec-verification-2.md` containing exactly the heading `# A1 New DD Real-Spec Verification 2` and one sentence stating that this change is the R4-3 fourth-gate end-to-end verification fixture for the fleet-graph DD service.

## Constraints

- Change only that new documentation file.
- Do not modify services, deployment configuration, credentials, or legacy-engine units.
- Preserve all existing behavior.

## Acceptance

```dd-acceptance
make verify
```
