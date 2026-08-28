# Line runner resilience: typed timeout, fault terminal, discoverable logs, bounds forwarding

All implementation and every code review must be performed by dev-dispatch in this isolated H0 worktree only.

## Background (verified incident, 2026-08-28)

Line `wf-a87b04` g3: worker seat's agent-session send blocked for exactly 3000s
(`TURN_TIMEOUT: opencode turn exceeded 3000s`, zero tokens produced), then the
line process crashed with exit 1 and **no terminal.json**, leaving a 12-hour-old
stale terminal.json from g2 masquerading as current state. Full traceback in
`/data/fleet-graph/logs/wf-a87b04.log:57-97`. Four independent defects:

## Required changes

### 1. Typed timeout exception (the crash amplifier)

`src/fleet_graph/executors/agent_session.py:125` raises
`AgentSessionError(RuntimeError)` for in-band envelope failures, including
`code == "TURN_TIMEOUT"`. `src/fleet_graph/graphs/goal_line.py:270-288`
guards worker turns with `except TimeoutError` — which can never catch it,
so the graceful path (`record_timeout()` → append round with
`reason: "worker_turn_timeout"` → continue; `bounds` terminal on streak) is
dead code for this seat type.

- Introduce a dedicated exception for in-band turn timeouts that inherits
  from `TimeoutError` (e.g. `AgentSessionTimeout(AgentSessionError, TimeoutError)`
  or equivalent), raised when the envelope failure code is `TURN_TIMEOUT`.
- Do NOT widen the goal_line catch to all `AgentSessionError` — non-timeout
  seat errors must keep their current behavior.
- Also cover the out-of-band source: `agent_session.py:213`
  `subprocess.run(timeout=...)` raises `subprocess.TimeoutExpired`
  (not a `TimeoutError` either); map it to the same typed timeout.

### 2. Exception boundary writes a fault terminal

`src/fleet_graph/graphs/runner.py:169-180` invokes the compiled graph bare.
Any unexpected node exception → exit 1 with no terminal.json (terminal is only
written by the `finalise` node). Add a boundary in `run_line`: on unexpected
exception, write `terminal.json` with `terminal: "fault"`, the exception class,
a one-line message, and a truncated traceback summary, then re-raise / exit
non-zero. A crash must never leave a stale previous-generation terminal.json
as the freshest signal.

### 3. Run root points at the log

Line stdout/stderr goes to `/data/fleet-graph/logs/{folder_id}.log`
(scheduler/launcher.py:50,81-82) — disconnected from `runs/{folder_id}/`.
Make the run root self-describing: include `log_path` in heartbeat.json and
in the terminal.json written by both `finalise` and the new fault boundary.

### 4. Launcher forwards bounds

`scheduler/launcher.py:83-104` argv only forwards `--max-rounds`;
`noop-limit` / `timeout-limit` are silently left at runner defaults
(`runner.py:39` timeout_limit=2) regardless of what a line declares.
Add optional `noop_limit` / `timeout_limit` fields to roster line entries
(config/ronin-lines.json schema) and forward them as `--noop-limit` /
`--timeout-limit` when present. Defaults unchanged when absent. Sync
`tests/test_ronin_lines_config.py` if its schema assertions need it.

## Required tests

- Typed timeout: a worker turn raising the in-band TURN_TIMEOUT is caught by
  the goal_line timeout path (round appended with `worker_turn_timeout`,
  no crash); streak still ends in `bounds` terminal. Same for
  `subprocess.TimeoutExpired`. Negative: a non-timeout AgentSessionError still
  propagates exactly as today.
- Fault boundary: a node raising an arbitrary exception produces
  `terminal: "fault"` terminal.json (with traceback summary) and non-zero exit.
- log_path present in heartbeat and both terminal paths.
- Launcher argv includes the forwarded bounds when configured, omits when not.

## Constraints

- No changes to dd/ control plane, supervise/, or bus protocol.
- Existing lifecycle and receipt behavior untouched.
- Full suite must stay green.

## Acceptance

```dd-acceptance
uv run pytest -q
```
