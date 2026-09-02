# Spec M1（wf-8d9737）· 等待即驻停（waiting = parked, zero LLM）

> 状态：已获监督面派单指令（2026-09-03 01:4x CST，「本轮执行一个真实前向动作：派发 spec-m1-waiting-park.md」）。判据锚：goal.md §二 M1（含阳性/阴性原文）、design.md §6.3（E3 状态词表）、§6.5（E5 唤醒事实）、§8 行「等待零消耗」。本 spec 与 design.md 冲突时以 design.md 为准。

## 要交付的行为（全部在 fleet-graph 仓）

1. **线状态词表落地**：goal 线对外状态收敛为封闭词表 `working / waiting_dd / waiting_decision / waiting_external / done / failed`，写入两处权威文件：`runs/<wf>/terminal.json` 与 `runs/.scheduler/<wf>.json`。自判做不下去 `failed` 与机械故障 `fault` 分开（fault 语义保留现状，不并入 failed）。
2. **派单即驻停、不轮询**：goal 线在 `development_create`（或 start）成功返回后进入 `waiting_dd` 并驻停：等待期间调度器不点火该线、不产生任何 LLM 调用；**删除/绕过「每轮起 LLM 去看 dd 状态」的轮询分支**（现状 rounds.jsonl verdict 只有 continue/invalid 的根因）。
3. **唤醒事实点火**：调度器 tick 只认事实（封闭词表先行落地两条）：`dd_awaiting_gate(dev_id)` 与 `dd_terminal(dev_id)`。dd 单到 `awaiting_gate` 或落终态 → 下一 tick 点火派单线新代 unit（generation + 1）。每 tick 日志含 refusal 行的惯例保留。
4. **waiting_decision / waiting_external** 至少把状态词与驻停字段接通（裁决送达唤醒归 M2；外部对象唤醒可先占位为显式声明对象清单，不实现探测）。

## 判据（goal.md §二 M1 原文内联）

- 阳性：一条线 `waiting_dd` 期间，按其 alias 计的模型请求数为 0；dd 到闸后下一 tick 出现新代 unit。
- 阴性：**删掉唤醒事实的发射 → 线永远不被点火**的用例必须红（不是靠轮询兜住）。
- design.md §8「等待零消耗」行由此变绿（verify-lim.sh 05 项断言生产事实，本单不要求改 verify-lim.sh）。

## 测试与验收

- 新增 `tests/test_m1_waiting_park.py`（命名照 wf-525fd4 的 test_m2_decision_gaps.py 惯例）：覆盖词表写入、派单驻停零点火、dd_terminal/dd_awaiting_gate 点火、阴性用例（删发射不点火）。**零测试删除**；既有测试更新断言到新真值不算删除。
- 全量绿基线：base `54f4230` 起跑（`make verify` 2522 passed / 1 skipped，S6 代理陷阱——验收在 dd unit 环境无 SOCKS env）。

## 边界

- 只动 fleet-graph 仓（scheduler/、graphs/goal_line.py、terminal/.scheduler 写入面）；不改 decision-bridge（M8 退役项）、不动 dd 引擎本身、不做 M2 的裁决接口、不做 release 分支（M5）。
- 与 wf-525fd4 M1「线状态只读 MCP」不重叠：它做读视图，本单做状态本体与驻停/唤醒机制。

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_m1_waiting_park.py'
bash -lc 'make verify'
```