# Spec — E1 缺口 #4 修复：line 进程 inbox 内容路径接通（alias 穿传 + 凭证收敛）

This development implements the two structural fixes for E1 gap #4 (findings 2026-08-29 18:2x): the coordinator's inbox content never reaches the coordinator because (A) the scheduler never threads `--alias` into the line process, and (B) the line's inbox drain authenticates with the service token, which structurally 403s on `agent:*`.

Design input (authoritative detail): `design-e1-gap4-inbox-content-path.md` in work folder wf-d002a6. Follow it. This spec freezes the scope below.

## Decision

1. **Thread alias** (`scheduler/launcher.py` + `scheduler/daemon.py` + `cli.py` + `graphs/runner.py`):
   - `LaunchSpec` gains `alias: str | None = None`; `argv()` appends `--alias <alias>` when set.
   - `Scheduler.spec_for` passes `alias=line.alias`.
   - The line process then builds a real `Inbox` instead of `_NullInbox` (runner.py `Inbox(...) if config.inbox_alias else _NullInbox()`).

2. **Credential convergence** (`bus/inbox.py` consumer side + shared token helper):
   - Extract the line-token resolution currently private in `LiveWakeSignals._line_token` (`scheduler/wake.py`, `LINE_TOKEN_PATH_TEMPLATE = "/data/ronin/secrets/{alias}.token"`) into a shared helper.
   - The line's inbox client must authenticate with the line's own token (same credential family as the wake probe), NOT the service token (`FLEET_GRAPH_BUS_TOKEN_FILE`), so `Inbox.consume` on `agent:{alias}` succeeds.
   - The wake probe and the line inbox both use the shared helper (one change, both sites).

3. **Fail-open degradation**: if the line token is missing/unreadable, the inbox drain degrades without faulting the whole line (explicitly recorded, never silently pretending messages were read). Never fault the line solely because an inbox credential is absent.

## Scope (do)

- Add `--alias` pass-through from scheduler launch to line config; produce a real `Inbox` for the line.
- Shared line-token resolution used by both the wake probe and the line inbox.
- Line inbox drain authenticates with the line's own token.

## Scope (do not)

- Do not change agent-bus kernel semantics or the service token's structural `agent:*` 403 (it is by-design; the wake probe already documents it).
- Do not touch the E2 card pass-through (separate development dev-fg-b96d3a37c2a9).
- Do not alter parking/wake policy, the decision bridge, terminal-view (E3), or E4/E5 increments.
- Do not deploy, restart any production unit, or run git operations in the production main checkout. All git work is in the dedicated `/data/worktrees/` worktree. Production may only `git pull --ff-only` after a separately approved remote-main merge.

## Regression tests (new `tests/test_inbox_content_path.py`)

1. **alias pass-through**: `LaunchSpec(alias=...)` argv contains `--alias`; `LineConfig(alias=...)` → `build_line` yields a real `Inbox` (not `_NullInbox`); coordinator `drain_then_ack` receives and persists a controlled message (`inbox_messages` non-empty).
2. **credential mismatch negative**: a faithful fake (modeling real ACL: service-token auth on `agent:*` → 403, line-token → 200) asserts the line inbox uses the line token, not the service token; the service-token 403 is asserted as the pre-fix failure mode and does not occur on the fixed path.
3. **degradation**: missing line token → inbox drain degrades explicitly and does not fault the line.

## Real-bus controlled roundtrip

One acceptance scenario runs against the real bus: a synthetic drill alias (never a real line's inbox) → publish a controlled message with the line token → build a `LineConfig` carrying `--alias` → `build_line`'s inbox drain receives that message (`inbox_messages` contains the sent message_id) → emit JSON evidence (UTC timestamps, send-side message_id, head_seq before/after, drain result) with direct exit code. Token arrives via `--line-token-file`; never hard-code a token.

## Acceptance

The following argv are the acceptance contract. The new `tests/test_inbox_content_path.py` and `scripts/e2_inbox_content_path_acceptance.py` must exist and pass; the focused regression argv below are required and may be supplemented, not replaced.

```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_parking.py -q
uv run pytest tests/test_goal_line.py -q
uv run pytest tests/test_goal_interrupt.py -q
uv run pytest tests/test_inbox_content_path.py -q
uv run python scripts/e2_inbox_content_path_acceptance.py --scenario alias-passthrough-drain-receives-message
uv run python scripts/e2_inbox_content_path_acceptance.py --scenario service-token-403-asserted
uv run python scripts/e2_inbox_content_path_acceptance.py --scenario real-bus-inbox-roundtrip --bus-url http://127.0.0.1:7490 --line-token-file /data/agent-bus/tokens/e2-wakecheck-repro-20260829.token
```

## Definition of done

- `--alias` threads from the scheduler into the line; the line runs a real `Inbox`.
- The line inbox drain uses the line's own token (shared helper with the wake probe), never the service token.
- Missing line token degrades without faulting the line.
- The three regression criteria pass, plus repository verification (`make verify`).
- The real-bus roundtrip passes and emits JSON evidence.
- The final dev-dispatch receipt records each acceptance argv and its direct exit code.
