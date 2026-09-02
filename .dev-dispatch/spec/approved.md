# 案A改写：收割逐仓生效值 + 只授合并权入围 + dry-run 解析留痕 + verify 写前闸契约

来源：监督面直写 goal.md 2026-09-02 15:0x「🔴 案 A 改写」第八节四任务。上一节把「案 A」理解为
allowlist 扩容判据（卷内 a1-harvest-allowlist-expansion-security-spec.md，即 14:2x 旧框架），
监督面明确收回该提法，改判：**扩围在今天的代码下做不成安全的事，瓶颈是三个全局旋钮假设了单仓世界**。
本 spec 取代旧「扩容判据」，落监督面四任务；不为旧框架徒耗。

## 取证锚点（监督面真机结论，实现据此，不重证）
1. `HarvestAllowlistEntry` 已带逐仓 `allowed_branches` / `allowed_deploy`，但 `harvest.py` 的
   `gate` / `deploy` / `ff_only_pull` 用的是全局 `deps.default_branch` / `deps.deploy_command`，
   只把 entry 拿去 `authorize` 校验——逐仓字段只做校验、不当生效值。
2. `harvest_default_branch` 全局估值是 `master`，而生产候选仓（fleet-graph / agent-session-mcp /
   lexicon / wiki-v3 / goal-agent / agent-runtime）全是 `main`；`harvest_deploy` 全局值是
   `["bash", "scripts/deploy.sh"]`，而上述候选仓没有该脚本 → gate 直接拒整单，或跑不存在的脚本
   exit 127 → deploy ok:false → escalated，且 merge 已发生。
3. `deploy` 节点已有 `if not command: return {"deploy_exit_code": 0}` 这条合法 no-op 路径，
   所以「只授合并权、不授部署权」在现有代码里可表达。
4. H9 之后 `resolve_verify_argv` 按目标仓解析，解析不到 -> `(None, "no resolvable verify command")`；
   `verify` 节点据此 ok:false + escalated，且发生在写步骤之前。此性质现为实现副作用，未被断言契约保护。

## 任务（排序 2 → 1 → 4 → 3；判据两方向可红，实现形态自决）

### 任务 2：只授合并权的入围形态（allowed_deploy=[]）
把「条目 `allowed_deploy: []` -> 该仓生效 deploy 命令为空」接成生效值：
- 命中条目后，该单的生效 `deploy_command` 取自条目而非全局；条目 `allowed_deploy` 为空（`[]`）时
  生效 deploy 命令必须为空（空 argv），走 `deploy` 节点既有 no-op 路径，`deploy_exit_code == 0`，
  `postconditions` 不得因此判红（收割能走到 `harvested`）。
- **判据（两方向可红）**：
  - 阳性：造一个 `allowed_deploy: []`（merge-only）的仓 -> 收割能走到 `harvested`，且
    `deploy_exit_code == 0`；deploy 是合法 no-op，不当作未执行写步骤误报。
  - 反向：条目声明（生效）部署命令而该命令不在 `allowed_deploy` 白名单内（「声明与白名单不符」）
    -> gate 必须拒（`granted=false` + 机器可读 reasons），整单不得往前走。

### 任务 1：逐仓生效值（default_branch / deploy_command 取自条目，全局降级为缺省）
- `HarvestAllowlistEntry` 增加可选逐仓字段 `default_branch` 与 `deploy_command`（缺省 None；
  向后兼容：现有 JSON 条目无这两个字段时按旧全局行为解析——不新增配置面、不改签 allowlist）。
- intake/gate 命中条目后，该单的生效 `default_branch` / `deploy_command` 取自条目；条目未指定时
  才退回全局 `deps.default_branch` / `deps.deploy_command`（即缺省）。
- **判据（两方向可红）**：
  - ① 一个 `main` 仓的单，其条目 `default_branch="main"` -> 生效 branch 必须是 `refs/heads/main`
    （gate 拿它比 `allowed_branches` 能过）；**变异：把它改回读全局（如全局 `master`）必须变红**
    （`refs/heads/master` 不在 `allowed_branches` 内 -> 拒）。
  - ② `master` 仓（如 fleet-sentinel）行为不得改变：条目未指定 `default_branch` 时仍取全局，
    指定 `master` 时亦 `master`，既有 master 仓回归零红。

### 任务 4：可解析 verify 是写前闸（把取证锚点 4 的副作用钉成契约）
- 无可解析 verify 的仓，收割必须在**任何写步骤（pr_squash_merge / ff_only_pull / deploy）之前**
  escalate：`verify` / `verify_real` 解析不到可执行指令时，ok:false + 机器可读 detail +
  `outcome=escalated` + `writes_skipped` 覆盖全部写步骤，绝不继续往下走产生任何写。
- **判据（可红）**：变异——让 verify 解析失败时继续往下走（越过 escalate 放行后续写节点）
  -> 必须有用例变红（断言 escalate 早退 + `writes_skipped` 全量 + 无写发生）。

### 任务 3：dry-run（解析但不授权）
- 让「归属解析」与「allowlist 授权判定」解耦，使反应器能在**不授予写权限**的前提下解析出
  「这个 record 本会归属哪个 canonical 仓」并留痕，使「先观测后授权」在产品里成立
  （不再依赖监督面手工 import 函数跑脚本）。
- 一个不在 allowlist 的仓，其 e5 报告必须能记下：本会解析到的 canonical 仓 + 本会执行的写步骤
  （pr_squash_merge / ff_only_pull / deploy），同时 `writes_skipped` 覆盖全部写步骤、真机零写
  （不产生任何 git/部署写原语）。
- **判据（可红）**：不在 allowlist 的仓 -> 报告如实记录「would-resolve canonical / would-do
  write steps」且 `writes_skipped` 覆盖全部写步、真机零写；变异——dry-run 路径真实执行任一写原语
  或漏记 `writes_skipped` -> 必须有用例变红。

## 交付约束
- 只改 `src/fleet_graph/supervise/harvest_allowlist.py`、`src/fleet_graph/supervise/harvest.py`、
  `src/fleet_graph/supervise/harvest_ops.py` 与 `tests/test_harvest_allowlist.py` /
  `tests/test_harvest.py` 及必要测试 fixture；
- **不得改动、不得签发** `/data/fleet-graph/supervisor/harvest-allowlist.json`（发证权归监督面；
  本 spec 不扩围、不新增条目、不续签）；
- 不改 E5/E6/E7 事件词表；不部署、不重启；写动作仍全部被 allowlist gate 包住（Guard D 不变）。

```dd-acceptance
uv sync --frozen
make verify
```