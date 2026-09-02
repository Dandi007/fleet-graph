# M2(a) 裁决面补全 —— decision_deliver 增加区分「线 / dd 闸」的目标参数

## 背景
`decision_mcp.py` 的 `decision_deliver(line, decision, reason)` 仍是三参，只认「驻停 goal 线」。`decision_bridge/owners.py` 已有 `OWNER_KIND_DD="dd"` 与 `DdOwnerSource`（`DdControlPlane.gate(development_id, resume=True)` 无 value resume），但 MCP 面未暴露目标参数，dd 闸（占裁决总量约 21%）落在覆盖面外，被当线处理或静默缺失（监督面 2026-09-03 goal 级验收判红 DoD2(a)）。

## 交付物
1. 给 `decision_deliver` 增加一个能区分「线」与「dd 闸」的目标参数（形状自决：如 `target_kind` ∈ {line, dd} + 目标 id，或一个含 kind/id 的 target 参数——但 `inputSchema` 必须显式可区分，不得一个 path 参数吞两种语义）。现役三参「线」路径向后兼容保持。
2. dd 闸投递：target 为 dd 时，服务端从 dd control plane 的 `awaiting_gate` 记录解析 question/card（`DdOwnerSource`），并经 `DdControlPlane.gate(development_id, resume=True)` 恢复投递；返回 `delivered`/`consumed`（含 resume 状态）或明确拒绝码（未知 dd 单 → 明确拒绝码，非 awaiting 状态 → 明确拒绝码）。永不 HTTP 200 静默吞。
3. 既有四发阴性保持不变：`LINE_NOT_PARKED` / `NO_WAITING_PARTY` / `QUESTION_CARD_UNRESOLVED` / `OWNER_REFUSED`，以及非法载荷 `DecisionPayloadError` → 调用点 `DECISION_DELIVER_REFUSED`。
4. 各补一条能红的阴性用例：把一个 dd 闸 id 用「线」路径投递（或对 dd 目标投给不存在/非 awaiting 的单），必须得到明确拒绝码，证明 dd 闸不再被静默当线处理（不能是「读 line stall 状态 → LINE_NOT_PARKED」这种把 dd id 当 folder_id 处理的旁路），也不能是 Unknown tool。

## 双向判据（对齐 goal.md M2(a)，不可弱）
- 阳性：对 dd 闸投递 → 返回 delivered/consumed 或明确拒绝码，dd 闸不再落在覆盖面外。
- 阴性：目标类型显式区分，dd 目标被静默当线处理须有用例变红。

## 红线
- 与 `decision_mcp.py` 既有 R2 端口纪律、ledger/metrics、闭集拒绝码口径一致；不引入新 native 面。
- 不删除既有测试；新增/扩展的测试放 `tests/test_decision_mcp.py`（既有 decision_mcp 测试文件）。
- prod 主 checkout 只 ff-only；改动只进 worktree。

## 验收
```dd-acceptance
uv sync --frozen
uv run pytest -q tests/test_decision_mcp.py tests/test_decision_bridge.py
```