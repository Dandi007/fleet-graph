# A2 arbiter managed periodic path

Decision: `msg_01M15Z6TYCTXMCABKKG42ZQ13Z`. Approved for development only. This development must not deploy, install, start, enable, or restart units/timers and must not create a production principal, alias, or token.

## Required implementation

1. Add `deploy/systemd/fleet-graph-arbiter.service`: systemd user `Type=oneshot`; one bounded tick through `/data/apps/fleet-graph/current/.venv/bin/fleet-graph arbiter run --publish --alias arbiter` (`arbiter` maps to inbox `agent:arbiter`); mandatory dedicated `EnvironmentFile=%h/.config/fleet-graph/arbiter.env`; bounded runtime/network ordering; no restart loop. The committed unit may reference only the protected EnvironmentFile path for credentials. No credential/token value or token-file value in git, argv, stdout/stderr, receipt, or journal. Never reference the decision-publish credential.
2. Add `deploy/systemd/fleet-graph-arbiter.timer` with documented bounded cadence and only the oneshot service target. Ship install metadata but do not install/start/enable it. Document a one-line rollback for a later approved window; do not execute it.
3. Add validation-only principal/alias reconciliation before model work/publication. Idempotently verify authenticated principal is the expected arbiter principal and binding resolves to `agent:arbiter`. Correct existing state continues without mutation. Missing/mismatched/rebound/unauthorized state reports a clear non-secret error and exits nonzero before publication. Expose no create/register/token-mint/token-write fallback or mutation call.
4. A successful tick prints a bounded machine-readable receipt of counts/kinds/refs without credentials. Mechanical emitted-output audit must assert at least one referenced `work.note.v1` finding/progress or explicit suggestion in a positive isolated fixture and must assert arbiter-emitted `work.decision.v1` count=0, `work.decision.v2` count=0, and decision-marked chat count=0. Decision-shaped reasoner output must remain coerced to note/suggestion-only output.
5. Add deployment-contract tests for service/timer syntax, exact command, mandatory EnvironmentFile, no secret/token material in argv/unit, cadence, and no enable/start side effect. Add reconciliation tests for correct/missing/mismatched/rebound states proving no mutation. Add `scripts/a2_managed_path_acceptance.py` (or equivalently named bounded harness matching the frozen argv) that emits a referenced suggestion fixture and prints the required counters. Update operating docs with prerequisites, separate deployment gate, final artifacts, acceptance queries, and rollback, without claiming activation.

## Explicit non-goals

No production deployment/release/snapshot switch/daemon-reload/start/enable/restart; no production principal/alias/token creation or token file write; no decision publication, decision-marked chat, gate release, merge decision, or control action; no credentials in fixtures/logs.

```dd-acceptance
uv sync --frozen
uv run pytest -q tests/test_arbiter.py tests/test_arbiter_managed_path.py tests/test_deploy_unit.py
uv run python scripts/check_supervisor_conformance.py
uv run python scripts/a2_managed_path_acceptance.py
```

The last command must exit 0 and print bounded JSON proving `referenced_note_or_suggestion_count >= 1`, `work.decision.v1 == 0`, `work.decision.v2 == 0`, and `decision_marked_chat == 0`.