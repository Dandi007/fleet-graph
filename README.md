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

## Status

P0 (project setup and architecture doc). Nothing here orchestrates anything
yet; `fleet-graph --version` is the whole surface. Phases P1–P7 are tracked in
work folder `wf-3f30cd`.
