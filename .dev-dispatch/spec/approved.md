# A1 user-systemd acceptance successor

## Goal

Create the single successor to the cancelled A1 service-surface developments. Preserve the reviewed implementation lineage and supply an explicit usable user-systemd and DBus session runtime for acceptance.

## Reviewed lineage

- Bootstrap H0: `0069998dd5c9115ca7c7ce7aff6a354f04dfe5ca`.
- Rework implementation: `35cd4f9d7613108b0d57f31c6a2a0d053183ccc1`, receipt `sha256:622e7d452a6ae2b8aab7d3cf559fddcf1fc36423940726362d75d5ff70b92344`.
- Continuous approval candidate: `2526d49f13600e3f05b740925e782309744416f7`, receipt `sha256:7216b8e55aa25a648f2a7185eae3d5763a2f7d9020b3b3351f6ed838a34bf478`.
- Final approval output: `d8707d45c20ea650b5a258444c5f6bc6f8ba1775`, receipt `sha256:e7a581747a3ca4c1d61220b0b9d306efa6f4eb43568e001766672297a49617c9`.
- Acceptance failure: receipt `sha256:40c29aa097678d366582098dbdac3ae1bac9c053b147e9881aa54744e8430532`, proof `sha256:1a0d57962aa69ebdbe089b192233187c59c66bd578f5e7a411e9ef0aee7c7709`.

## Constraints

- All business-code changes and all code review are performed by dev-dispatch workers in isolated worktrees.
- Acceptance must execute with a usable user-systemd manager and DBus session. The prior missing-manager condition is not a passing or skippable outcome.
- Retain all three anti-vacuity systemd tests unchanged; do not skip, weaken, mark xfail, or bypass them.
- Run complete `make verify` in the isolated candidate checkout and require exit code 0.
- Do not write to, validate in, or alter the production main checkout. Do not deploy or alter the dev-dispatch production control plane.
