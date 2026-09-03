# 案A改写 ①：逐仓生效值（命中 allowlist 条目即供 default_branch / deploy_command，全局降级缺省）

来源：监督面直写 goal.md 2026-09-02 15:0x「🔴 案 A 改写」第八节任务 1。
监督面已逐条真机证实现状（取证锚点），实现据此，不重证。

## 取证锚点（机械事实）
1. `HarvestAllowlistEntry` 已带逐仓 `allowed_branches` / `allowed_deploy`，但编排层 `harvest.py` 的
   `gate` / `deploy` / `ff_only_pull` 用的是全局 `deps.default_branch` / `deps.deploy_command`，
   只把 entry 拿去 `authorize` 校验——逐仓字段只做校验、不当生效值。
2. `harvest_default_branch` 全局估值是 `master`，而生产候选仓（fleet-graph / agent-session-mcp /
   lexicon / wiki-v3 / goal-agent / agent-runtime）全是 `main`；`harvest_deploy` 全局值是
   `["bash","scripts/deploy.sh"]`，候选仓多数没有该脚本 → gate 拒整单或跑不存在的脚本。

## 要什么
- `HarvestAllowlistEntry` 增加**可选逐仓字段** `default_branch` 与 `deploy_command`（缺省 `None`；
  **向后兼容**：现有 JSON 条目无这两个字段时按旧全局行为解析——不新增配置面、不改签 allowlist）。
- intake/gate 命中条目后，该单的生效 `default_branch` / `deploy_command` **取自条目**；条目未指定时
  才退回全局 `deps.default_branch` / `deps.deploy_command`（即缺省）。

## 判据（两方向可红）
- **① 正向**：一个 `main` 仓的单，其条目 `default_branch="main"` → 生效 branch 必须是
  `refs/heads/main`（gate 拿它比 `allowed_branches` 能过）；**变异：把它改回读全局（如全局
  `master`）必须变红**（`refs/heads/master` 不在 `allowed_branches` 内 → 拒）。
- **② 回归**：`master` 仓（如 fleet-sentinel）行为不得改变：条目未指定 `default_branch` 时仍取
  全局 `master`，指定 `master` 时亦 `master`，既有 master 仓回归零红。

## 交付约束
- 只改 `src/fleet_graph/supervise/harvest_allowlist.py`（entry 增可选字段 + 向后兼容解析）、
  `src/fleet_graph/supervise/harvest.py`（intake/gate 命中条目后取条目生效值、未指定退回全局）、
  与 `tests/test_harvest_allowlist.py` / `tests/test_harvest.py` 及必要 fixture；
- **不得改动、不得签发** `/data/fleet-graph/supervisor/harvest-allowlist.json`（发证权归监督面；
  本 spec 不扩围、不新增条目、不续签）；
- 不改 E5/E6/E7 事件词表；不部署、不重启；写动作仍全部被 allowlist gate 包住（Guard D 不变）。

```dd-acceptance
uv sync --frozen
make verify
```