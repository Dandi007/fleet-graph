# PumpDown roster and systemd liveness correctness

## Goal

Eliminate the eleven PumpDown false positives in the repository that owns the
legacy PumpDown exporter and rules: `Dandi007/fleet-graph`.

Make the fleet-graph roster's `enabled` state the sole source of monitored
lines, and evaluate each enabled line against liveness of its matching
`fleet-graph-line-*-g1.service` systemd unit.

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
  dev-dispatch workers in isolated `/data/worktrees` worktrees.
- The production checkout is never checked out, switched, reset, detached,
  written, or validated. Production may only run `git pull --ff-only` after a
  remote-main merge.
- Do not request Controller-owned receipts, working-directory fields, or raw
  stdout/stderr from implementers.
- Do not query production or live PumpDown state from an isolated candidate
  worktree.
- Do not gate, merge, or deploy in this development.

## Acceptance

dev-dispatch must publish its normal development receipt, with readable,
non-404 development status and evidence endpoints, and a candidate review
result. The candidate must pass:

1. Dynamic line-metrics tests that prove enabled, disabled, retired, removed,
   healthy, and stopped-line behavior.
2. `promtool check config` for the Prometheus configuration.
3. `promtool check rules` for the PumpDown alert rules.
4. `promtool test rules` for the PumpDown rule tests.

The development receipt and standard controller evidence are sufficient
evidence. No additional transcript, cwd, stdout, stderr, or production query
is required.
