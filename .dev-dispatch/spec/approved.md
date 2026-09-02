# M2 —— 裁决面补两个缺口（(a) dd 闸覆盖 / (b) 送达必须唤醒线）

现有 `fleet-graph-decision` 面（`src/fleet_graph/decision_mcp.py`，`decision_deliver(line, decision, reason)`）已能同步定论、拒绝码明确、有账本与 metrics（监督面 2026-09-02 真机验过，四发阴性全中）。剩余两个已知缺口（wf-525fd4 goal.md M2，其中 b 为 P0）。

## 交付物

1. 缺口 (a)：现有面只认「哪条线」（`line` 参数），dd 闸（dd 单 awaiting_gate，占裁决总量约 21%）落在覆盖面外。补上对 dd 闸的投递路径——调用方能对一张 dd 单投递裁决，且「线/单该归谁裁决」的对应关系由**服务端**解析，调用方不猜 question/card。
2. 缺口 (b)：送达单据 ≠ 唤醒线。现状是裁决落板、单推进到终态，而**驻停等裁决的线仍驻停**（调度器 refusal 原文「parked until a wake fact appears」），监督面被迫直写该线 goal 手工唤醒。补上：投递成功后触发该线 wake（经其注册控制入口解除驻停/点火）。
3. `tests/test_m2_decision_gaps.py`（本单验收目标，含独立 negative 用例）。

## 双向判据（不可弱，逐字对齐 goal.md M2）

- **阳性**：对一条驻停等裁决的线投递 → 返回「已送达且被消费」，**且该线在 N 个调度 tick 内点火**。
- **阴性**：线未驻停 / 无此等待方 / 载荷非法 → 明确拒绝码，**不得静默吞掉**（既有四发阴性保持，另新增：对不存在的 dd 单/闸投递必须显式拒绝，不得 HTTP 200 后静默失败）。

## 红线纪律

- 既有四发阴性（LINE_NOT_PARKED / QUESTION_CARD_UNRESOLVED / NO_WAITING_PARTY / 载荷非法）必须保持绿；只增不减。
- 缺口 (a) 不允许「一个 call 工具 + 一个 path 参数」假 MCP；dd 闸与线的裁决入口切分窄且自解释（golden-order 第 2 条）。
- 缺口 (b) 唤醒必须走既有控制入口（stall-state wake / 注册 control entry），复用既有唤醒机制，不得另造绕过调度器的「直写」后门。
- `build_*` 可无传输层单测；测试不触碰生产账本/生产文件（health-isolation）。
- 不可达/不可判定必须显式报红并给证据，不得静默绿。
- 不删除/改写既有测试或 scripts/；`make verify` 不回归（新增文件过 ruff/format）。

## 验收

```dd-acceptance
uv sync --frozen
uv run pytest -q tests/test_m2_decision_gaps.py
```