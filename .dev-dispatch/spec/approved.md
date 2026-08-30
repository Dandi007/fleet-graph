# M4 破障/汇报节点——E6 stop→代谢重拉、E7 goal.md 直写+送达自验、wiki 人话账 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：goal.md M4「破障/汇报节点」。依赖 M1（:7494 read-model）+ M2（E5/E6/E7 事件）+ M3（harvest 子图 + allowlist 纪律）。
- 类别：纯增量（新增 E6/E7 处置反应器 + wiki 人话账节点 + conformance 扩展），不改 E1–E7 词表语义、不改判据、不改 harvest 14 步管线与 allowlist 语义。
- 交付：代码与评审全委 dev-dispatch；worker 只写 spec 与取证。
- 前置说明（本单之外，不走本 dd acceptance）：①「监督面显式 allowlist PR」（M3 激活前置）独立单另行落；②E5 语义修复单的 implement 超时阻塞属引擎围栏单，其 per-stage timeout 修复已合入 PR#167（45282f8）；本单不代产二者。

## 交付 A：E6 处置反应器（stop→代谢重拉）

1. 输入 E6 `heartbeat_stale` 事件（payload：`folder_id` / `heartbeat_age_s` / `round` / `phase`）。
2. SOP（script 节点，机械判定，不采信自述），在 supervisor 进程内，与 harvest 同构（`run_supervisor` 对 E6 分派到本反应器，不进入审计图）：
   - `intake` —— 解析 `folder_id`（非空且 `wf-` 前缀，否则 escalated）。
   - `resolve_unit` —— 机械解析目标线 unit：前缀 `fleet-graph-line-<folder_id>-*` 下唯一 active 单元（`systemctl --user list-units` 输出解析），或读 scheduler stall-state 的 generation 构造 `fleet-graph-line-<folder_id>-g<gen>`；解析不到/多解 → escalated，**绝不任意 stop**。
   - `gate` —— E6 停止权判定：目标 unit 必须是 event.folder_id 自己的 line unit（前缀精确匹配，越界 → refused + 留痕）。
   - `stop` —— `systemctl --user stop <unit>`；机械写动作走注入 ops 层，写函数必须先过 gate（与 Guard D 同纪律）。
   - `postconditions` —— 代码核验 stop 后 unit 不再 active（`is-active` 非 0 / :7494 `/v1/lines` 该线心跳龄回落或新代起火），不采信自述；未达成 → escalated。
   - `evidence` + `receipt` —— evidence note 挂卡；结果落 supervisor 自己 state root。
3. 「代谢重拉」= **scheduler 下一 tick 自然 re-launch**（stop 后 unit 不 active、无 terminal → ignition 重拉起）。本单**不实现第二调度**（Guard A 不动：反应器不 import `scheduler.ignition` / `scheduler.launcher`）。

## 交付 B：E7 处置反应器（goal.md 直写信道 + 送达自验）

1. 输入 E7 `decision_swallowed` 事件（payload：`source_message_id` / `reason`）。
2. 解析目标线：`source_message_id` → 决策链 → card head `work_folder_id`（机械链，复用 `supervisor` 的 `_folder_id` 同款读取，禁从 prose 解析）；解析不到 → escalated（不猜、不降级静默）。
3. goal.md 直写信道：经 work-folder MCP（`state/work_folder.py::WorkFolder` 的 `fs_edit`/`fs_write`/`fs_read`/`fs_stat`）向**该线自己的 goal.md** 追加固定块模板（E7 送达失败块：`source_message_id` / `reason` / `at` / 监督面直写署名）。`content_revision` 变化即命中 scheduler 停车唤醒源（goal_revision），停牌线自然被唤醒——这正是「送达」的机械闭环。
4. 送达自验（postconditions，不采信自述）：写后 `fs_stat` 取 `content_revision` 确认变化 + `fs_read` 回读确认块正文在场；两缺任一 → escalated。
5. 写权限纪律（与 M3 allowlist 同款、独立配置）：E7 goal.md 直写唯一正当来源 = 命中「E7 直写目标线」白名单（`folder_id` 圈点，默认 deny-all，未命中即拒绝 + 留痕）；块模板封闭（字段固定，不写任意 prose）。

## 交付 C：wiki 人话账节点

1. 独立组件（建议 `supervise/wiki_report.py`）：用 katana-wiki-mcp（`search` 按标题定位「舰队开发阶段性成果报告」页 → `page_append`；页不存在按该页「报告更新约定」骨架重建），wiki 客户端注入以便测试替换。
2. 触发（命中任一就追加带日期分节，不攒批）：line-done / 生产晋级（harvest HARVESTED）/ 缺陷闭环（E6/E7 处置成功收口）/ 新阶段授权。挂在反应器终止路径与 line-done 事实。
3. 写法铁律 §6.5：分节先背景（这条线是什么、为什么做）→ 交付与现状 → 证据指针（PR 号 / commit / 看板 seq / 真机回显）；裸 wf-id/订单号等抽象缩写不进正文。
4. 机械 postcondition：`page_append` 返回成功 + 回读页含刚写分节标题（送达自验，不采信自述）。

## 交付 D：conformance 扩展（Guard E）

- `scripts/check_supervisor_conformance.py` 扩展：
  - E6 处置模块只能 stop 自己 event.folder_id 的 line unit（前缀精确匹配，禁 arbitrary unit）。
  - E7 处置模块只能写自己解析出的 folder_id 的 goal.md（直写目标圈点）。
  - 两类写原语函数必须先调用对应 gate（`authorize`/`gate` 调度名），同 Guard D 纪律；破坏样本负例进 `tests/test_supervisor_conformance.py`（sabotage self-verification）。

## 交付 E：测试（合成快照/注入 fake，禁触真网）

1. E6：合成 E6 → stop 目标 unit + postcondition（is-active 非 0）；越界（非本 folder unit）→ refused 留痕不 stop；postcondition 缺 → escalated；`make verify` 下 E1–E7 词表负例零回归。
2. E7：合成 E7 → goal.md 直写 + content_revision/回读验证送达自验；解析不到 folder → escalated；传送失败 → escalated；`validate_event` unknown 事件名仍拒绝（负例保留）。
3. wiki：fake wiki client → 分节追加 + 分节标题在场；§6.5 铁律（证据指针字段）断言。
4. `make verify`（lint + test + conformance）通过；M1–M3 既有测试零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据（goal.md M4）

1. `make verify` 通过（含 E6/E7 处置路径单测 + wiki 节点单测 + Guard E 破坏负例）。
2. 并行对账窗与退役清单（findings 落账 + 旧件替代证据）属上线后监督面人肉/值守面执行，不并入本 dd acceptance。

## 铁律

- E6 停止权、E7 goal.md 直写权一律默认 deny-all，未命中目标线 → 拒绝 + 留痕，绝不静默放行（与 M3 allowlist 先行同款纪律；「监督面显式 allowlist PR」仍是 M3 激活的独立前置）。
- E6「代谢重拉」= scheduler 自然 re-launch，本单不实现第二调度；反应器不 import scheduler/ignition/launcher（Guard A 不动）。
- harvest 14 步管线、allowlist 语义、E1–E7 词表、判据（goal.md 验收断言）本单一律不改；判据只有用户能改。
- 一切改动走 PR（本 development worktree），不直改 main；生产主 checkout 仅 ff-only pull。