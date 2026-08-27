# A1 acceptance-contract successor

## Goal

Create the single successor to the failed A1 acceptance. Preserve the reviewed candidate and correct only the acceptance contract.

## Reviewed lineage

- Bootstrap H0: `0069998dd5c9115ca7c7ce7aff6a354f04dfe5ca`.
- Rework implementation: `35cd4f9d7613108b0d57f31c6a2a0d053183ccc1`, receipt `sha256:622e7d452a6ae2b8aab7d3cf559fddcf1fc36423940726362d75d5ff70b92344`.
- Continuous approval candidate: `2526d49f13600e3f05b740925e782309744416f7`, receipt `sha256:7216b8e55aa25a648f2a7185eae3d5763a2f7d9020b3b3351f6ed838a34bf478`.
- Final approval output: `d8707d45c20ea650b5a258444c5f6bc6f8ba1775`, receipt `sha256:e7a581747a3ca4c1d61220b0b9d306efa6f4eb43568e001766672297a49617c9`.
- Latest final-review output candidate: `7389dc6a5a1f55091de79fa052f189cbf26a6044`, receipt `sha256:e6defc96503393e238ea479a413553b35120e6c2a9d7d689a28acdba91585057`.
- Latest acceptance result: `make verify` exited 0 with 714 passed; the sole failure was a duplicate host verify, `systemctl --user is-system-running`, because the acceptance environment has no DBus session.

## Constraints

- All business-code changes and all code review are performed by dev-dispatch workers in isolated worktrees.
- Acceptance executes exactly one command in the isolated candidate checkout: `make verify`, requiring exit code 0.
- Do not configure any host verify command. In particular, do not run `systemctl --user is-system-running`; it duplicates the hermetic test coverage and requires a DBus session unavailable to the acceptance executor.
- Retain all three anti-vacuity systemd checks unchanged. They must execute through `make verify`; do not skip, weaken, delete, mark xfail, bypass, or replace any of them.
- The candidate under acceptance is exactly `7389dc6a5a1f55091de79fa052f189cbf26a6044`; no business-code change is requested.
- Do not write to, validate in, or alter the production main checkout. Do not deploy or alter the dev-dispatch production control plane.
