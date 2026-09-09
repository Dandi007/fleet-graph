# 旧部件下线盘点（decommission inventory，dd-36）

> 依据：GO-14 B6（「新的写好了的话 重复的全都下线」）、design §9 第三条、§8 第二条重复清单。本 DD 只盘点并加机械守卫，**不删任何生产代码**；分批删除待后续 DD，release→main 合并是人工闸，删除批次由人过目。盘点基于本分支（dd-36）2026-09-09 的树；行数为 `wc -l` 实测。

## 判定标准

一个单元**只有同时满足以下三条**才进可删批次：

1. design §8 第二条明确列为重复（scheduler/wake/parked、decision MCP、decision-bridge、arbiter、看板投票、harvest、supervisor 图、goal_interrupt，共 8 项）；
2. 不被 `fleet_graph.minimal` 包 import（由 `tests/test_minimal_decommission.py` 守卫 (a) 常驻防回归；当前树已核实 minimal 零旧包引用）；
3. 不被 minimal 的测试（`tests/test_minimal_*.py`）依赖（当前树已核实：28 个 minimal 测试文件均不 import 旧包）。

判定取值：

- `batch-1 可删`：三条件全过，且保留下来的生产代码无人 import 它（只剩 `cli.py` 接线、systemd 单元、自身测试/脚本）——可直接进删除批次；
- `需先解依赖`：三条件全过，但被**保留下来的**旧生产代码或保留侧测试 import——需先来一张解依赖 DD，再删；
- `保留`：不满足进批条件（不在 §8 第二条清单，或无 minimal 对应物）。

## 表格解析规则（守卫 (b) 按此解析，测试里写死）

数据行以 `` | `` 开头、第一列是反引号包住的仓库相对路径；该路径必须在树上真实存在。表头行、`|---` 分隔行及其它非路径首列的行不解析。

## design §8 第二条 8 项速查

- scheduler/wake/parked → `src/fleet_graph/scheduler/` → 需先解依赖
- decision MCP → `src/fleet_graph/decision_mcp.py` → batch-1 可删
- decision-bridge → `src/fleet_graph/decision_bridge/` → 需先解依赖
- arbiter → `src/fleet_graph/arbiter/` → 保留（无对应物，非 minimal 范围）
- 看板投票 → `src/fleet_graph/supervise/decision_publisher.py`（+`preauth.py`）→ batch-1 可删
- harvest → `src/fleet_graph/supervise/harvest.py`（+`harvest_ops.py`/`harvest_allowlist.py`）→ batch-1 可删
- supervisor 图 → `src/fleet_graph/graphs/supervisor.py` → batch-1 可删
- goal_interrupt → `src/fleet_graph/goal_interrupt/` → 需先解依赖

## 盘点表

| 路径 | 约 py 行数 | 对外入口 | 仓内引用者 | 被 minimal 的哪个模块取代 | 判定 | 批次顺序与理由 |
|---|---|---|---|---|---|---|
| `src/fleet_graph/scheduler/` | 4427（10 文件） | console script `fleet-graph`（子命令 `scheduler run`、`line set-seat` / `line revive` / `line overrides`）；systemd `deploy/systemd/fleet-graphd.service` | 生产：`src/fleet_graph/dd/control_plane.py`、`src/fleet_graph/supervise/events.py`、`src/fleet_graph/cli.py`；测试：`tests/test_scheduler.py`、`tests/test_scheduler_daemon.py`、`tests/test_launcher.py`、`tests/test_m1_waiting_park.py` 等 | 常驻引擎进程内调度：`src/fleet_graph/minimal/engine.py` + `goalgraph.py` / `ddgraph.py`（design §1「引擎是常驻进程」、§7.2；wake/parked 停车态不存在，等待人=引擎在步骤边界读 control） | 需先解依赖 | §8 项 scheduler/wake/parked（`wake.py` 与 parked 停车态都在包内）。批次 4：`dd/control_plane.py` 与 `supervise/events.py` 都不在 §8 第二条清单、保留，必须先拆掉它们对本包的 import，才可连同 cli 子命令、systemd fleet-graphd 与簇内测试一起删。 |
| `src/fleet_graph/decision_bridge/` | 1817（5 文件） | console script `fleet-graph`（子命令 `decision-bridge run` / `decision-bridge status`）；systemd `deploy/systemd/fleet-graph-decision-bridge.service` | 生产：`src/fleet_graph/decision_mcp.py`（批次 1 先走）、`src/fleet_graph/goal_interrupt/resolver.py`（候选）、`src/fleet_graph/cli.py`；测试：`tests/test_decision_bridge.py`、`tests/test_fleet_state_readmodel.py`（state 侧，保留）等 | 无独立桥：MCP→引擎只经每 goal 一个 control.jsonl（design §7.6），`src/fleet_graph/minimal/control.py` | 需先解依赖 | §8 项 decision-bridge。批次 3：与 goal_interrupt 互为引用、同批走；前置=批次 1 删 decision MCP + 拆 graphs/goal_line 与 runner 对 goal_interrupt 的引用（见下），保留侧测试 `test_fleet_state_readmodel.py` 同步改造。 |
| `src/fleet_graph/arbiter/` | 1336（6 文件） | console script `fleet-graph`（子命令 `arbiter run`）；systemd `deploy/systemd/fleet-graph-arbiter.service` | 生产：`src/fleet_graph/cli.py`；测试/脚本：`tests/test_arbiter.py`、`tests/test_arbiter_managed_path.py`、`tests/test_reconcile_a2_live_bus.py`、`scripts/a2_managed_path_acceptance.py`、`scripts/check_reconcile_a2_live_bus.py` | 无对应物，非 minimal 范围，保留（A2 三诊面属于旧 agent-bus 世界，minimal 不重建该问题域；其下线属旧世界整体退役，需用户另行拍板） | 保留 | §8 项 arbiter。虽满足判定标准 1–3 且仅 cli 引用，但 minimal 无对应模块承担其职能，按判定标准不进批次；留待旧 agent-bus 世界（bus/、state/、line_state_mcp 等）整体退役时一并处置。 |
| `src/fleet_graph/supervise/decision_publisher.py` | 132（另 `preauth.py` 295） | 无独立入口（全仓唯一 `work.decision.v2` 发布点，仅被 supervisor 图的 act 节点 import） | 生产：`src/fleet_graph/graphs/supervisor.py`（唯一，Guard C 钉死）；测试/脚本：`tests/test_decision_publisher.py`、`tests/test_preauth.py`、`tests/test_credential_separation.py`、`scripts/check_supervisor_conformance.py` | 看板投票不存在：人裁决走 Goal Agent 审单 approve/reject（`src/fleet_graph/minimal/ddgraph.py` 审单节点）+ approve 后先看平台 mergeable（`src/fleet_graph/minimal/mergegate.py`；design §3.5、§7.22） | batch-1 可删 | §8 项 看板投票（`decision_publisher.py` + `preauth.py` 的预授权代投通路）。批次 2：与 supervisor 图同簇（互为唯一生产引用），同批删；`supervise/` 其余模块不在 §8 清单、保留。 |
| `src/fleet_graph/supervise/harvest.py` | 1480（另 `harvest_ops.py` 1331、`harvest_allowlist.py` 201） | 无独立入口（E5 `approved_unharvested` 事件触发的收割反应器，supervisor 进程内） | 生产：`src/fleet_graph/graphs/supervisor.py`（唯一）；测试：`tests/test_harvest.py`、`tests/test_harvest_allowlist.py`、`tests/test_supervisor_graph.py` | 过 gate 合入默认分支：`src/fleet_graph/minimal/mergegate.py`（线尾 release→target 合并计划 / platform merge）+ `src/fleet_graph/minimal/prlifecycle.py`；部署动作无对应物（minimal 无部署环节，备注待拍板） | batch-1 可删 | §8 项 harvest（`harvest.py` + `harvest_ops.py` + `harvest_allowlist.py` 整个收割反应器）。批次 2：与 supervisor 图同簇同批删；部署尾巴若用户要保留，属新增能力、不在本清单。 |
| `src/fleet_graph/graphs/supervisor.py` | 1053 | console script `fleet-graph`（子命令 `supervisor run` / `supervisor reset`）；无 systemd 单元 | 生产：`src/fleet_graph/cli.py`；测试/脚本：`tests/test_supervisor_graph.py`、`tests/test_supervisor_conformance.py`、`tests/test_supervise_audit.py`、`scripts/check_supervisor_conformance.py` | 审计回合由 DD 循环承担：程序化验收命令 + CR → FR → Goal 审单链（`src/fleet_graph/minimal/ddgraph.py`，design §3） | batch-1 可删 | §8 项 supervisor 图。批次 2：它是看板投票与收割反应器的唯一生产引用者，三件同批删（含 cli `supervisor` 子命令、簇内测试与 conformance 脚本）；`supervise/events.py`、e6/e7 等不在 §8 清单、保留成孤儿待另行拍板。 |
| `src/fleet_graph/goal_interrupt/` | 1441（6 文件） | console script `fleet-graph`（子命令 `goal-interrupt run`，常驻桥）；无 systemd 单元 | 生产：`src/fleet_graph/graphs/goal_line.py`、`src/fleet_graph/graphs/runner.py`（旧 goal 线图，保留）、`src/fleet_graph/decision_bridge/resolver.py`（候选）、`src/fleet_graph/cli.py`；测试/脚本：`tests/test_goal_interrupt.py`、`tests/test_goal_line_card.py`、`scripts/e2_goal_interrupt_acceptance.py` 等 | 无 in-graph interrupt：引擎在步骤边界读 control.jsonl（`src/fleet_graph/minimal/control.py` + `stagerunner.py`；design §7.6），人经 MCP goal_message / goal_steer 介入 | 需先解依赖 | §8 项 goal_interrupt。批次 3：被保留侧旧图 `graphs/goal_line.py` / `graphs/runner.py` import，先来解依赖 DD 拆引用（`test_goal_line_card.py` 等保留侧测试同步改造），再与 decision_bridge 同批删。 |

## 批次汇总与范围外说明

- 批次 1（已执行，dd-38）：`src/fleet_graph/decision_mcp.py`（面最小，先行验证删除流程）。实际删除清单：`src/fleet_graph/decision_mcp.py`、`deploy/systemd/fleet-graph-decision-mcp.service`、`config/decision-mcp-reserved-ports.json`、`tests/test_decision_mcp.py`、`tests/test_m2_dd_gate_delivery.py`；`src/fleet_graph/cli.py` 删 `decision` 子命令（handler + parser + `--state-dir` 帮助），`tests/test_deploy_unit.py` 删 decision-mcp 单元断言与 DEFAULT_PORT / 保留端口断言。
- 批次 2（supervisor 簇，同批互为唯一引用）：`src/fleet_graph/graphs/supervisor.py` + `src/fleet_graph/supervise/decision_publisher.py` + `preauth.py` + `harvest.py` + `harvest_ops.py` + `harvest_allowlist.py`。
- 批次 3（先解依赖张，再删除张）：拆 `graphs/goal_line.py` / `graphs/runner.py` 对 goal_interrupt 的引用 → 删 `src/fleet_graph/goal_interrupt/` + `src/fleet_graph/decision_bridge/`。
- 批次 4：拆 `dd/control_plane.py` / `supervise/events.py` 对 scheduler 的引用 → 删 `src/fleet_graph/scheduler/`。
- 保留：`src/fleet_graph/arbiter/`（无对应物，非 minimal 范围）。
- 范围外（不在 §8 第二条清单，本清单不判定、一律保留待另行拍板）：`graphs/` 其余图（goal_line、dd_pipeline、research_* 等）、`dd/`、`goal/`、`goal_enroll/`、`executors/`、`bus/`、`state/`、`cost_obs/`、`supervise/` 其余模块（events / e6_* / e7_* / inbox / audit / wiki_report）、`line_state_mcp.py`、`research_*.py`、`cli.py` 等。
- 每批删除都需同步处理：pyproject console script 接线（现全走 `fleet-graph` 子命令）、对应 systemd 单元（`deploy/systemd/`）、自身测试与 `scripts/` 脚本；本 DD 一概不动。
