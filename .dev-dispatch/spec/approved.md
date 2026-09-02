# M4 wiki 人话账节点接线缺陷修复（v2：+阴性守卫用例）——生产晋级 HARVESTED 不追加分节 + wiki 客户端未构造

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：`supervise/wiki_report.py`（M4 交付 C）接线。真机触因：闸18 产品已发布（生产晋级 HARVESTED 事实已发生，PR #226），但 wiki 页「舰队开发阶段性成果报告」自动分节=0 次。
- 类别：接线缺陷修复 + **补一条阴性守卫用例**（v2 相对 v1/上一闸的 delta）。
- 与上一闸（dev-fg-4d06db4493ea，spec sha256:80053096）关系：同 spec 内容 + 新增阴性用例；rebase 到新 main（本轮 fa73ab5362b8，H-A/H-B/H-C #231 与 roster RETIRED #232 已上线，151507a 基线已过期）。

## 根因（已实读 release 14938bf 源，非推断）

两处机械缺口，缺一即 0 分节：

1. **`harvest.py` 全文无 wiki 接线**：`wiki_report.py` 的 `record_production_promotion`（生产晋级 HARVESTED）、`record_line_done`（line-done）、`record_stage_authorized`（新阶段授权）三个函数只定义、从不被任何模块调用。`run_harvest` 终局（`outcome==OUTCOME_HARVESTED`）不追加「生产晋级」分节。
2. **生产进程从未构造 wiki 客户端**：`DefaultWikiClient` 全源码树零构造点；`SupervisorRunConfig.wiki` 默认 None；`scheduler/supervisor_events.py::SupervisorLaunchSpec.argv()` 不发任何 wiki 旗标；故 `run_supervisor` 三路 `wiki=config.wiki` 恒 None，连 `e6_stop.py`/`e7_write.py` 已接的 `record_defect_closed` 也因 `deps.wiki is None` 跳过。

## 交付 A：harvest 生产晋级分节接线（`supervise/harvest.py`）

1. `run_harvest` 在终局 `outcome==OUTCOME_HARVESTED` 时调 `record_production_promotion`（标题「生产晋级：<development_id>」，背景=该单是什么，交付与现状=已 HARVESTED，证据指针=PR 链接/commit/看板 seq）。
2. `HarvestRunConfig`/`HarvestState` 注入 `wiki` 客户端（可注入 fake）。
3. **best-effort**：wiki 追加失败只记 `wiki_report` step `ok:false` + detail，绝不翻转 harvest outcome、绝不 escalate、绝不重跑收割。

## 交付 B：wiki 客户端构造 + 调度透传（`supervisor` 配置与 `scheduler/supervisor_events.py`）

1. `run_supervisor` 提供 `--wiki`（可选 enable）开关；off（默认）时 `deps.wiki=None` 字节不变（零回归）；on 时把 `DefaultWikiClient()`（`DEFAULT_WIKI_MCP_URL=http://127.0.0.1:8113/mcp`）注入 E5/E6/E7 三路 `config.wiki`。
2. `SupervisorLaunchSpec.argv()` 在本 config 指定启用时发 `--wiki` 旗标（默认不发、词法稳定）。
3. 不新增第二调度、不 import scheduler/ignition/launcher（Guard A）；wiki 失败绝不咬 E6 stop/E7 write。

## 交付 C：E6/E7 缺陷闭环分节激活（已接线、只待客户端非 None）

`e6_stop.py`（OUTCOME_STOPPED）/`e7_write.py`（OUTCOME_DELIVERED）现有 `deps.wiki is not None` 分支保留；本单只使 `deps.wiki` 可为非 None。

## 交付 D：阴性/正向测试（含 v2 新增阴性守卫，本单判据）

1. **【v2 新增·阴性守卫，必须能红】**fake wiki 注入，跑 harvest 至 `outcome != OUTCOME_HARVESTED`（如 `escalated` 或 no-op 终态）→ 断言 `record_production_promotion` **0 次调用**。验收标准=去掉 `and state.get("outcome") == OUTCOME_HARVESTED` 守卫后该用例必须变红（当前缺失该守卫时全绿=阴性面没钉住）。理由（监督面）：`record_production_promotion` 写的是「已 HARVESTED」正文，守卫一旦失效，未收割成功的单也会被写成已上线——telemetry 可以失败、不可以撒谎。
2. **harvest 生产晋级触发**：fake wiki + `outcome==OUTCOME_HARVESTED` → 断言 `record_production_promotion` 调用 1 次、入参带 non-empty 证据指针；未修复时 0 次（阴性能红）。
3. **harvest wiki 失败不咬主链**：fake wiki 抛 `WikiReportError` → harvest outcome 仍 HARVESTED、`wiki_report` step ok=false、绝不 escalate。
4. **wiki off 零回归**：`--wiki` 缺省不发 → argv 不含、deps.wiki=None；启用 → argv 含、deps.wiki=DefaultWikiClient。
5. `make verify` 全绿；M3/E6/E7 既有测试零语义回归。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据

1. `make verify` 通过。
2. 阴性守卫红：去掉 `outcome==OUTCOME_HARVESTED` 守卫 → 用例 1 变红；补上守卫 → 绿。
3. harvest HARVESTED 修复后 1 次调用、wiki 抛错不翻转 outcome；`--wiki` 缺省不发/启用才发。

## 铁律

- 只改 `src/fleet_graph/supervise/harvest.py`、`wiki_report.py`(如需)、`graphs/supervisor.py`、`cli.py`、`scheduler/supervisor_events.py` + `tests/`；不触 `decide()`、E1–E7 词表、harvest 14 步管线、harvest-allowlist 语义、判据。
- 不触 `harvest-allowlist.json`、不代造 wiki 分节/收割单/真实单、不改 §6.5；wiki 失败绝不咬主反应器。
- 一切改动走 PR（本 development worktree），生产主 checkout 仅 ff-only pull，禁 checkout/switch/reset/detach；不 import scheduler/ignition/launcher。