# M4 wiki 人话账节点接线缺陷修复——生产晋级 HARVESTED 不追加分节 + wiki 客户端未构造

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：`supervise/wiki_report.py`（M4 交付 C）接线。真机触因：闸18 产品已发布（生产晋级 HARVESTED 事实已发生 1 次，PR #226=36ac767 已 deploy），但 wiki 页「舰队开发阶段性成果报告」自动分节=0 次（页 mtime 仍 2026-08-31 04:28:52、size 29095 未变）。
- 类别：接线缺陷修复，不改判据、不改 §6.5 写法、不改 allowlist 语义、不代造分节。

## 根因（已实读 release 14938bf 源，非推断）

两处机械缺口，缺一即 0 分节：

1. **`harvest.py` 全文无 wiki 接线**：`wiki_report.py` 的 `record_production_promotion`（生产晋级 HARVESTED）、`record_line_done`（line-done）、`record_stage_authorized`（新阶段授权）三个函数**只定义、从不被任何模块调用**（全 `src/fleet_graph/` grep 命中仅定义与 `__all__`）。`run_harvest` 终局（`outcome==OUTCOME_HARVESTED`）不追加「生产晋级」分节。
2. **生产进程从未构造 wiki 客户端**：`DefaultWikiClient` 全源码树**零构造点**；`SupervisorRunConfig.wiki` 默认 `Any|None=None`；`scheduler/supervisor_events.py::SupervisorLaunchSpec.argv()` 只按需发 `--harvest-allowlist/--harvest-default-branch/--harvest-deploy/--repo` 旗标、**不发任何 wiki 旗标**；故 `run_supervisor` 中 E5/E6/E7 三路 `wiki=config.wiki` 恒为 `None`，连 `e6_stop.py`/`e7_write.py` 已接好的「缺陷闭环→`record_defect_closed`」分支也因 `deps.wiki is None` 跳过（E6 停转回执 `reports/e6-*.json` 的 steps 无 `wiki_report` 步佐证）。

真机佐证（本轮已取证）：katana-wiki-mcp 服务 `wiki-v3.service` active（`HOST=100.64.0.6,127.0.0.1`、`PORT=8113`，`127.0.0.1:8113` 与 `100.64.0.6:8113` 均可达）；supervisor 各 `reports/`/`logs/` 无 `WikiReportError`/`page_append`/`8113` 写失败——即「服务可用、从未被调用」，是接线缺口、非服务不可达失败。

## 交付 A：harvest 生产晋级分节接线（`supervise/harvest.py`）

1. `run_harvest` 在终局 `outcome==OUTCOME_HARVESTED`（收割成功、产品已进默认分支）时调用 `wiki_report.record_production_promotion`（标题「生产晋级：<development_id>」或等价，正文背景=该单是什么，交付与现状=已 HARVESTED，证据指针=PR 链接/commit/看板 seq）。
2. `HarvestRunConfig`/`HarvestState` 注入 `wiki` 客户端（可注入 fake，默认同 supervisor 侧传递）。
3. **best-effort**：wiki 追加失败只记 `wiki_report` step `ok:false` + detail，**绝不翻转 harvest 的 `outcome`、绝不 escalate、绝不重跑收割**（wiki 是 telemetry）。

## 交付 B：wiki 客户端构造 + 调度透传（`supervisor` 配置与 `scheduler/supervisor_events.py`）

1. `run_supervisor` 提供 wiki 客户端注入点：`--wiki`（可选 enable）开关；off（默认，缺省不发）时 `deps.wiki=None` 行为字节不变（零回归）；on 时把 `DefaultWikiClient()`（`DEFAULT_WIKI_MCP_URL=http://127.0.0.1:8113/mcp`）注入 E5/E6/E7 三路 `config.wiki`。
2. `scheduler/supervisor_events.py::SupervisorLaunchSpec.argv()` 在本 config 指定启用时发 `--wiki` 旗标（默认不启用、不改变既有 argv 词法；词法顺序稳定，测试按 `in argv` 断言）。
3. 不新增第二调度、不 import scheduler/ignition/launcher（Guard A 不动）；`wiki` 失败绝不咬 E6 stop/E7 write 语义。

## 交付 C：E6/E7 缺陷闭环分节激活（已接线、只待客户端非 None）

`e6_stop.py`（`outcome==OUTCOME_STOPPED`）与 `e7_write.py`（`outcome==OUTCOME_DELIVERED`）现有 `deps.wiki is not None` 分支保留；本单不重写其语义，只确保交付 B 使 `deps.wiki` 可为非 None（测试用 fake wiki 断言 `record_defect_closed` 被调用）。

## 交付 D：阴性/正向测试（`tests/test_wiki_report*.py` + `tests/test_harvest.py` 扩展，禁触真网/真 wiki）

1. **harvest 生产晋级触发**：fake wiki 注入，跑收割到大成功 → 断言 `record_production_promotion` 被调用一次、入参带 non-empty 证据指针；未修复时 0 次（阴性能红）。
2. **harvest wiki 失败不咬主链**：fake wiki 抛 `WikiReportError` → harvest `outcome` 仍 HARVESTED、`wiki_report` step ok=false、绝不 escalate。
3. **wiki off 零回归**：`--wiki` 缺省不发 → `deps.wiki=None`、argv 不含 `--wiki`、E6/E7 不调 wiki；`--wiki` 启用 → argv 含 `--wiki`、deps.wiki 为 DefaultWikiClient。
4. `make verify` 全绿；M3/E6/E7 既有测试零语义回归。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据

1. `make verify` 通过。
2. 阴性能红：harvest HARVESTED 未修复时 wiki 0 次调用（红）、修复后 1 次（绿）；wiki 抛错不翻转 harvest outcome。
3. `--wiki` 缺省不发（argv 不含）、启用才发（argv 含）；`deps.wiki` off 时 None、on 时 DefaultWikiClient。

## 铁律

- 只改 `src/fleet_graph/supervise/harvest.py`、`src/fleet_graph/supervise/wiki_report.py`（如需）、`src/fleet_graph/graphs/supervisor.py`、`src/fleet_graph/cli.py`、`src/fleet_graph/scheduler/supervisor_events.py` + `tests/`；不触 `decide()`、E1–E7 词表、harvest 14 步管线、harvest-allowlist 语义、判据。
- 不触 `/data/fleet-graph/supervisor/harvest-allowlist.json`、不代造 wiki 分节/收割单/真实单、不改 §6.5；wiki 失败绝不咬主反应器（best-effort telemetry）。
- 一切改动走 PR（本 development worktree），生产主 checkout 仅 ff-only pull，禁 checkout/switch/reset/detach；不 import scheduler/ignition/launcher（Guard A）。