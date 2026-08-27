# fleet-graph

LangGraph orchestration spine for the ronin fleet and the dev-dispatch pipeline.
It replaces loop-engine (job execution), the `/data/ronin` bare scripts
(babysitter / auto-gate / supervisor-guard), and the goal-agent pump, while
leaving agent-runtime, agent-bus, the katana MCPs, and the New API gateway
untouched.

**Read [`docs/architecture.md`](docs/architecture.md) first** — it carries the
layering, the four anti-lock-in invariants, and the old→new component map.

## Quickstart

```bash
make sync     # uv sync into .venv (Python 3.11)
make verify   # ruff check + format check + pytest — the same gate CI runs
```

## Work-board Surface

The supported live consumer operations are `create`, `start`, `get`, `list`,
`events`, `evidence`, and `gate` (`Board.ask` / `Board.decision_for`). Other
legacy-only operations are not implemented and must be reported as
`NOT_SUPPORTED`; they must not be treated as approximations of legacy behavior.

`deploy/verify-user-session-bus.sh` records the raw user-manager response and
accepts only `running` or `degraded`, so systemctl's degraded exit status is not
mistaken for a disconnected session bus.

## Status

P0 (project setup and architecture doc). Nothing here orchestrates anything
yet; `fleet-graph --version` is the whole surface. Phases P1–P7 are tracked in
work folder `wf-3f30cd`.
