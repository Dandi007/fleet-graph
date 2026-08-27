# PumpDown roster and systemd liveness correctness

## Goal

Eliminate the eleven PumpDown false positives by making the fleet-graph roster's
`enabled` state the sole source of monitored lines and by evaluating each enabled
line against the liveness of its corresponding `fleet-graph-line-*-g1.service`
systemd unit.

## Scope

- Derive monitoring series exclusively from enabled roster entries.
- Emit no monitoring series for disabled or retired roster entries.
- Remove a series within one scrape interval when its roster label disappears.
- Preserve genuine failure detection: a stopped enabled line unit must alert in
  minutes, while a healthy enabled line must not alert.
- Do not conceal failures with PromQL exclusions, label filters, silences, or
  alert suppression, and do not restart or change legacy pumps.

## Constraints

- All implementation, tests, and every code review are performed by
  dev-dispatch workers in isolated worktrees.
- The production checkout is never checked out, switched, reset, detached,
  written, or validated. Production may only run `git pull --ff-only` after a
  remote-main merge.

## Frozen Acceptance

1. `uv run pytest -q tests/test_ronin_lines_config.py`
2. `uv run pytest -q tests/test_scheduler.py`
3. `uv run pytest -q tests/test_scheduler_daemon.py`
4. `make verify`

Each command must run from the isolated candidate worktree with complete stdout,
stderr, exit status, and cwd preserved in the acceptance receipt. After a
candidate exists, independently query the live PumpDown alert state to confirm
the original eleven false positives are absent and that the monitoring semantics
remain observable.
