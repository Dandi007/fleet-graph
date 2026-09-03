# 案A改写 ②：只授合并权——allowed_deploy=[] 时部署命令为空走 no-op，postconditions 不判红

来源：监督面直写 goal.md 2026-09-02 15:0x「🔴 案 A 改写」第八节任务 2。
监督面已逐条真机证实现状（取证锚点），实现据此，不重证。

## 取证锚点（机械事实）
1. `HarvestAllowlistEntry` 已带逐仓字段 `allowed_deploy`（`src/fleet_graph/supervise/harvest_allowlist.py`
   `entry.allowed_deploy: tuple[tuple[str, ...], ...]`）——「`allowed_deploy: []`」（merge-only）在条目上已可表达。
2. `deploy` 节点已有 `if not command: return {"deploy_exit_code": 0}` 的合法 no-op 路径
   （`src/fleet_graph/supervise/harvest_ops.py`）。
3. 但编排层 `harvest.py` 的 `deploy` 节点用全局 `deps.deploy_command` 当生效值，条目字段只拿去做
   `authorize` 校验、不当生效值；且 `authorize` 现在把「部署命令不在 `allowed_deploy` 白名单内 → 拒」
   （`harvest_allowlist.py:158-160`）——`allowed_deploy: []` 的 merge-only 条目会被这条误伤成 deny。

## 要什么
- 命中条目后，该单的生效 deploy 命令取自条目 `allowed_deploy` 而非全局 `deps.deploy_command`；
  **条目 `allowed_deploy: []`（merge-only）时生效 deploy 命令恒为空（空 argv）**，走 `deploy`
  节点既有 no-op 路径，`deploy_exit_code == 0`，**postconditions 不得因此判红**——收割能走到
  `harvested`（合并权照常行使：只合并、不部署）。
- **反向**：条目（生效）声明了部署命令而该命令**确实**不在 `allowed_deploy` 白名单内
  （「声明与白名单不符」）→ `gate` 必须拒（`granted=false` + 机器可读 reasons），整单不得往前走。

## 判据（两方向可红）
- **阳性**：造一个 `allowed_deploy: []`（merge-only）的仓 → 收割 `outcome == harvested` 且
  `deploy_exit_code == 0`；deploy 是合法 no-op，postconditions 不判红、不当作未执行写步误报。
- **反向**：条目声明（生效）部署命令而该命令不在 `allowed_deploy` 白名单内 → `granted=false`
  + reasons 内含指名 offending 命令与缺失授权，整单不走。
- **变异**：把「空命令走 no-op 且 `deploy_exit_code=0`」改成「空命令被 postconditions 判红 / 被
  当作未执行写步 / 被判部署失败」→ 必须有用例转红（断言 no-op 不判红这一契约定死）。

## 交付约束
- 只改 `src/fleet_graph/supervise/harvest.py`（deploy 生效值 + postconditions）、
  `src/fleet_graph/supervise/harvest_ops.py`（deploy no-op）、
  `src/fleet_graph/supervise/harvest_allowlist.py`（authorize：merge-only 不误拒、声明与白名单不符才拒）、
  与 `tests/test_harvest.py` / `tests/test_harvest_allowlist.py` 及必要 fixture；
- **不得改动、不得签发** `/data/fleet-graph/supervisor/harvest-allowlist.json`（发证权归监督面；
  本 spec 不扩围、不新增条目、不续签）；
- 不改 E5/E6/E7 事件词表；不部署、不重启；写动作仍全部被 allowlist gate 包住（Guard D 不变）。

```dd-acceptance
uv sync --frozen
make verify
```