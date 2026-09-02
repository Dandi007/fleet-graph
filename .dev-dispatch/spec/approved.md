# dd 终态故障退场语义 + human_gate REJECT 非故障（P1）

## 缺陷

1. 监督面 `DevelopmentTerminalFault` 告警对任何 `terminal != none` 且带 `failure`
   记录的 dd 单开火；终态在 `status.json` 不可变 → 指标 sticky → 无退场条件，成了
   永久告警。
2. `classify_failure` 把 `GATE_REJECTED`（human_gate REJECT）归入
   `IMPLEMENTATION_CODES` → `class="implementation"` → 被下游读作 fault，卖出
   「REJECT=故障」的假阳性。

## 要什么

1. 拒绝类不算故障：human_gate REJECT（`GATE_REJECTED`）必须有独立于
   environment_contract / implementation / fabrication 三分类的「拒绝」分类；对
   REJECT 不得产出会被读作 fault 的 failure 记录（fault 信号恒 0）。
2. 退场语义：拒绝/终态故障在正确处置（recover 恢复决策 / reconfigure + 新代际）
   后必须确定性退场——derived status 的 `failure`/fault 信号随新代际 terminal 重置
   而清除，拒绝类不残留为永久告警。

## 判据（验收）

- 阴性（卖不出假阳性）：`classify_failure(terminal="refused",
  terminal_code="GATE_REJECTED")` 产出拒绝分类而非 implementation / environment
  contract；断言 REJECT 不发射 fault 信号；若实现仍把 REJECT 归为 fault 必红。
- 阳性：真故障（如 environment_contract 未归类码）仍分类为 fault 且不变；且一条
  refused/故障单在 start 出新代际后其 derived status 不再带 `failure`（fault 信号
  清除 = 告警可退场）。
- 不回归：三分类其余契约与 `classify_failure` 语义（fabrication final、默认
  environment/contract、retryable 位）零回归。

## 交付约束

- 只改 `src/fleet_graph/dd/control_plane.py`（分类表 + `classify_failure` + 必要时
  derived status 组装）与 `tests/test_dd_control_plane.py`。
- 不改 fleet-sentinel 的告警规则/导出器（告警规则镜像由监督面在其自仓跟进，不在
  本线 dd 范围；本开发只保证 fleet-graph 侧 fault 信号分类正确且新代际可退场）。
- 不部署、不重启、不触碰生产 checkout。

```dd-acceptance
uv sync --frozen
make verify
```