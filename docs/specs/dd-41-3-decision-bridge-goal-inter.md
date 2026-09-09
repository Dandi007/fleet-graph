# 批次 3 前置：旧图与 decision_bridge 解耦 goal_interrupt

## 背景
decommission 批次 3 要删 `src/fleet_graph/goal_interrupt/`(1441) + `src/fleet_graph/decision_bridge/`(1817)，前置条件是保留侧不再 import goal_interrupt。minimal 的取代物：没有 in-graph interrupt，引擎在步骤边界读 `control.jsonl`（`minimal/control.py` + `stagerunner.py`，design §7.6），人经 MCP `goal_message` / `goal_steer` 介入。

在 release head 3867f2ba 实测的引用点：`src/fleet_graph/graphs/runner.py:30-32`（`goal_interrupt.contract.DecisionInput`、`runtime.LineInterruptPort` / `resume_line`、`store.GoalInterruptStore`）、`src/fleet_graph/graphs/goal_line.py:37`（contract 的若干符号）、`src/fleet_graph/decision_bridge/resolver.py:239`（延迟 import `goal_interrupt.resolver`）、`src/fleet_graph/cli.py:1255-1256`（`goal-interrupt run` 子命令，属删除张不属本张）。本张**只解耦，不删这两个包**。

## 要改什么
1. `src/fleet_graph/graphs/runner.py`、`src/fleet_graph/graphs/goal_line.py`：摘掉 in-graph interrupt 集成——相关 import、构造参数/依赖 seam、节点与边、resume/中断分支全部删除，旧图退回「没有 in-graph 中断」的形态。删除的中断能力**不做迁移、不留替代**（GO-14 B6：重复的旧部件下线）。
2. `src/fleet_graph/decision_bridge/resolver.py`：删掉对 `goal_interrupt.resolver` 的延迟 import 及其分支（该桥本身在批次 3 一并下线）。
3. 测试：`tests/test_goal_line_card.py` 里依赖 goal_interrupt 的用例（约 line 43-44、288、392）整例删除，不要改成 skip；其它保留侧测试若断言的是被摘掉的中断行为，同样删该用例并在 PR 说明里逐条列出删了什么。`tests/test_goal_interrupt.py` 原样保留（下一张删包时一并删）。
4. `docs/specs/minimal/decommission.md`：批次 3 一行改为「前置解依赖已完成（本 PR）：graphs/goal_line.py、graphs/runner.py、decision_bridge/resolver.py 已不引用 goal_interrupt；剩余引用者只有 cli.py 的 `goal-interrupt run` 子命令与两个包自身及其测试」；`context.md` 同步一句。相关运维文档若描述了被摘掉的中断路径，加一行下线注记即可。

## 不改什么
- 不删 `src/fleet_graph/goal_interrupt/`、`src/fleet_graph/decision_bridge/` 的任何文件，不动 cli.py（删除张的活）。
- 不动 `src/fleet_graph/minimal/**` 与 `tests/test_minimal_*.py`。
- 不动 `graphs/` 其余图（dd_pipeline、research_* 等）与 `bus/`、`state/`、`supervise/`。
- 不顺手重构旧图的其它逻辑，改动限于中断集成的摘除。

## 怎么验收
`make verify` 全绿；`src/fleet_graph/graphs/` 与 `src/fleet_graph/decision_bridge/` 下无 goal_interrupt 字样；goal_line / runner 相关测试单独跑绿。
