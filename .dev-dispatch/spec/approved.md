# goal-driven MCP 独立面切分（`fleet-graph goal serve`，127.0.0.1:5611）spec

> 用户拍板（2026-08-31 15:1x）：接口要做干净——goal-driven 的 MCP 就是 goal-driven，
> 不寄居 dd MCP。dd 面回归纯 dev-dispatch。
> （随卷副本，源 wf-216dc3 同名文件，内容一致）

- 目标仓：`/data/code/self/fleet-graph`（Dandi007/fleet-graph；本 development 在 `/data/worktrees/` 下独立 worktree）。
- 背景：PR #155 把 `goal_enroll` 工具、`goal-open` prompt、`fleet-graph://goal-open/briefing` resource 落在了 `dd serve`（:5610）里；且生产 unit 未传 `--work-folder-root`、env 无 `FLEET_GRAPH_WORK_FOLDER_ROOT`，线上调用必返回 `GOAL_ENROLL_SOURCE_UNBOUND`（registered-but-unbound 族 bug 复发，前例见 `docs/findings/work-folder-residue-reconciliation.md`）。
- 交付面：新增独立 MCP 服务 `fleet-graph goal serve`（FastMCP streamable-http，bind `127.0.0.1:5611`，路径 `/mcp`，MCP 注册名 `fleet-graph-goal`），goal-driven 相关件全部迁入；dd 面（:5610）移除这些注册，只留 dev-dispatch。

## 交付

### A. 新服务本体
1. `cli.py` 增 `goal` 子命令 + `serve`：`fleet-graph goal serve --host 127.0.0.1 --port 5611 --work-folder-root <root>`。
2. 端口纪律沿用 dd 面：启动前 `port_is_available` 检查，占用即拒启（可见失败，不 crash loop）。5611 已于 2026-08-31 在 fleet host bind-test 空闲；保留端口清单（dd 面 unit 注释里那份）同步加 5611。
3. **绑定 fail-fast**：`--work-folder-root` 与 `FLEET_GRAPH_WORK_FOLDER_ROOT` 均缺失时**拒绝启动**并打印明确错误——不允许起一个运行时才报 `*_SOURCE_UNBOUND` 的半残服务。这是对 unbound 族 bug 的结构性根治：把静默半残改成启动期可见失败。
4. 迁入注册：`goal_enroll` tool、`goal-open` prompt、`fleet-graph://goal-open/briefing` resource。`goal_enroll/` 模块本体不动（校验闸、拒绝码、registry、briefing 版本化全部沿用）。

### B. dd 面瘦身（干净做绝）
1. `dd serve` 移除 `goal_enroll` / `goal-open` / briefing resource 的注册——直接移除，不留 NOT_SUPPORTED stub（该工具 unbound 至今零生产调用者，无兼容负担；dd 面既有 5 个 NOT_SUPPORTED 是「预期存在但不支持」语义，不适用于此）。
2. `wf_reconcile` 本单不动、留在 dd 面（归属待用户/监督面另行拍板；若裁定迁移另立小单）。
3. 顺带修同主题常量错误：`supervise/wiki_report.py:24` `DEFAULT_WIKI_MCP_URL` 现指 `127.0.0.1:5610/mcp/`（= dd 自己），改为真实 katana-wiki-mcp `http://127.0.0.1:8113/mcp`。此值当前无消费者（`graphs/supervisor.py` wiki 默认 None）故无行为变化，属 5610 端口职责厘清的一部分。

### C. 部署 unit + 文档
1. `deploy/systemd/fleet-graph-goal-mcp.service`（systemd user unit，模板随 repo、不自动 enable——部署是监督面决策，沿用 dd-mcp unit 的约定与注释纪律）；ExecStart 显式传 `--work-folder-root <work-folder-root>`。
2. `docs/operating.md`：「闸一：名册」前补「闸零：入册申请（goal_enroll @ :5611）」一节——现状 operating.md 对该工具零覆盖。
3. 测试：`tests/test_deploy_unit.py` 同法覆盖新 unit（真子命令核验，防 crash loop）；新增/迁移测试断言 (a) goal 面 tools 含 `goal_enroll`，(b) dd 面 tools 不含 `goal_enroll`，(c) 缺 root 启动 fail-fast。
4. `scripts/e2_goal_enroll_acceptance.py` 改指 goal serve（真起服务走 loopback 的演练路径保留）。

## 可复现验收

```dd-acceptance
make verify
uv run python scripts/e2_goal_enroll_acceptance.py
```

## 量化判据（部署后真机；部署与放行属监督面，本单交付到「可部署」）

1. `cd /data/code/self/fleet-graph && make verify` 通过。
2. `e2_goal_enroll_acceptance.py` 演练通过：goal 面 loopback 调 `goal_enroll` 走完校验闸（非 UNBOUND 路径）。
3. dd 面 tools/list 不含 `goal_enroll`；goal 面 tools/list 含之。
4. `fleet-graph goal serve`（不带 root、清 env）以非零退出并打印绑定缺失错误。

## 铁律

- 代码与评审全委 dev-dispatch；worker 不写业务代码；一切改动走 PR 进 fleet-graph，不直改 main；生产 checkout ff-only。
- 本单纯重构+部署件：不改 `goal_enroll` 校验语义、不新增工具、不触 roster/scheduler——流水线补全见 goal-enroll-pipeline-spec.md（依赖本单）。
- 与 dd 面既有 17 工具的行为兼容：dev-dispatch 10 实工具 + 5 NOT_SUPPORTED + `wf_reconcile` 全部不动。
