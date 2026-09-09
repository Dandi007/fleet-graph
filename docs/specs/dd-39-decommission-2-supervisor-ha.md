# decommission 批次 2：删 supervisor 簇（图+看板投票+harvest+调度侧发起面）

## 背景
同批次 1 的依据（design §9 / GO-14 B6 / dd-36 的 `docs/specs/minimal/decommission.md`）。本张执行**批次 2**：supervisor 簇，四件互为唯一生产引用——`src/fleet_graph/graphs/supervisor.py`(1053)、`src/fleet_graph/supervise/decision_publisher.py`(132) + `preauth.py`(295)、`src/fleet_graph/supervise/harvest.py`(1480) + `harvest_ops.py`(1331) + `harvest_allowlist.py`(201)。minimal 取代物：审计回合由 DD 循环承担（`minimal/ddgraph.py` 的 验收→CR→FR→Goal 审单），人裁决由 Goal 审单 approve/reject + `minimal/mergegate.py` 的 approve 后先看平台 mergeable 承担。

**盘点表的一个补正（本张必须处理）**：`src/fleet_graph/scheduler/supervisor_events.py` 通过 argv 拉起 `fleet-graph supervisor run` 短命单元（不是 import，所以 dd-36 的 import 扫描没抓到）。它只为 supervisor 面存在，因此**先删这个发起面，再删图**，中间态不会留下指向不存在子命令的 argv。已核实 `scheduler/daemon.py` 不 import supervisor_events，只有 cli.py 引它（约 line 890-891 的 `scheduler run` observer 旗标、line 1093 的 `supervisor reset`）。

## 要改什么（按顺序）
1. 发起面：`git rm src/fleet_graph/scheduler/supervisor_events.py tests/test_supervisor_events.py`；cli.py 删掉 `scheduler run` 的 observer 旗标接线与 `supervisor reset` 子命令。`scheduler/daemon.py` 里只服务该 observer 的 harvest/e7/wiki 旗标字段可留（批次 4 连同 scheduler 一起删），仅在 lint 或测试报出未使用时做最小清理。
2. 图与簇：`git rm src/fleet_graph/graphs/supervisor.py src/fleet_graph/supervise/decision_publisher.py src/fleet_graph/supervise/preauth.py src/fleet_graph/supervise/harvest.py src/fleet_graph/supervise/harvest_ops.py src/fleet_graph/supervise/harvest_allowlist.py`；cli.py 删掉 `supervisor run` 子命令与其 import。
3. 一致性守卫：`git rm scripts/check_supervisor_conformance.py tests/test_supervisor_conformance.py`，并把 `Makefile` 的 `conformance` 目标里那一行去掉（保留 check_work_report_conformance.py 与 check_research_role_contracts.py 两条）。
4. 簇内测试：`git rm tests/test_supervisor_graph.py tests/test_harvest.py tests/test_harvest_allowlist.py tests/test_decision_publisher.py tests/test_preauth.py tests/test_supervise_audit.py`。
5. 保留侧的残留引用逐个清干净（`rg` 驱动）：`tests/test_credential_separation.py`、`tests/test_arbiter.py`（如 test_no_arbiter_module_imports_the_decision_publisher 这类守卫已无对象，整个用例删）、`tests/test_bus.py` 里提及 decision_publisher 的注释/用例——只删与被删模块绑定的部分，其余保留，不要留 skip。
6. 文档：`docs/operating.md` 的 supervisor run / supervisor reset / harvest / 看板投票 运维段落改成「已下线（decommission 批次 2）」；`design-e1.md` 只加一行下线注记，不重写正文。`docs/specs/minimal/decommission.md` 批次 2 标已执行 + 补记 supervisor_events 这条补正；`context.md` 同步一句。

## 不改什么
- 不动 `src/fleet_graph/minimal/**` 与 `tests/test_minimal_*.py`。
- 不动 `supervise/` 其余模块（events.py、e6_*、e7_*、inbox、audit、wiki_report）、`bus/`、`state/`、`arbiter/`、`goal_interrupt/`、`decision_bridge/`、`scheduler/` 其余模块的功能——它们不在 §8 第二条清单或属别的批次。
- 不为被删能力找替代实现，不把 harvest 的部署尾巴迁到 minimal。
- 不动 deploy/systemd 现存单元（supervisor 面本来没有自己的单元）。

## 怎么验收
`make verify` 全绿（conformance 目标已只剩两条脚本）；`uv run fleet-graph --help` 不含 supervisor 子命令；全树无被删模块的 import。
