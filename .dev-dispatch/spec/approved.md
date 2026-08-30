# E7 首跑基线修复——observer decision_swallowed 缺水位线 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：M2 修复。🔴P1 生产缺陷（监督面补立案，progress.md L94）。
- 类别：缺陷修复，非判据变更。goal.md 不改。
- 依赖：M1 read-model(:7494) + M2 E5/E6/E7 词表已合入（main@6ebe9cb）。与在飞 E5 修复单 dev-fg-9bb86bfee5a2 不同文件面（彼改 `state/fleet_state.py` + `test_fleet_state_readmodel.py`；本单改 `scheduler/supervisor_events.py` + `test_supervisor_events.py`），无冲突。

## 根因（已实读，非推断）

`src/fleet_graph/scheduler/supervisor_events.py::SupervisorObserver._read_model_events()`
（L406–418）对 `/v1/decisions` 每个 `state=="swallowed"` 的判决派生 E7 `decision_swallowed`。
`/v1/decisions` 是**全量快照**（非增量流），observer 无任何水位线：每次 tick 都重新扫出全部
历史 swallowed 判决。唯一护栏是 `max_attempts_per_key=3`（每 key 终身 3 次 attempt），故历史
已人工处置完毕的 swallowed（33 条 × 3 ≈ 99 次）被当「新事件」反复空审计；生产 journal
（19:38–20:06Z）16 次 supervisor launched 全为 `e7-msg_01M13x/14x` 历史 key，纯浪费。

## 目标语义（supervisor 给定，照此实现）

E7 首跑采当前 `/v1/decisions` 快照为**水位基线**，此后只对**新增** swallowed 判决审计。

- 照抄同文件 `_board_scan` L445–449 已有同款先例（E1 board_question）：
  > "First run adopts the current head as its baseline … questions from before
  > we were watching are the human's existing backlog, not events we observed."
  （动作注记 `cursor_adopted:head_seq=<head_seq>`）
- E7 水位 = `source_message_id` 集合（`/v1/decisions` 视图天然带 `source_message_id`，可做
  集合水位，与 E1 的序号游标一一对应）。

## 交付 A：E7 水位（只读，不新增任何写权限）

1. cursor 状态（`_load_state`/`_write_state`，落盘 `supervisor-cursor.json`）新增键
   `e7_baseline`：已观测（已审计）的 swallowed `source_message_id` 有序列表。
2. `_read_model_events` 的 decisions 分支改为水位语义：
   - 若 `e7_baseline` **键不存在**（首跑/重置后）：采当前 `/v1/decisions` 快照中
     `state=="swallowed"` 且 `source_message_id` 非空的全部 id 记为基线，落盘，
     返回动作注记 `{"source":"read_model","action":"cursor_adopted:e7_baseline=n=<n>"}`，
     **本 tick 零 E7 发射**。
   - 后续 tick：仅对 `source_message_id` **不在** `e7_baseline` 中且 `state=="swallowed"`
     的判决派生 E7；派生后推进水位（将该 id 加入 `e7_baseline`），使其本 tick 与后续
     tick 不再重发。
3. budget / attempt / receipt 语义不变：新增 E7 仍走 `_consider`
   （`max_launches_per_tick=2`、`max_attempts_per_key=3`、receipt 压制、unit 名 `e7-<id>`、
   thread `supervisor:e7-<id>:a{n}`）。E5/E6 不受影响。
4. **budget 延后纪律（与 E1 游标同）**：被 `deferred:tick_budget` 或 `skipped:audit_in_flight`
   而未真正处置的新 id 不得推进水位，下一 tick 重扫重派；已处置（`launched` /
   `skipped:receipt_exists` / `skipped:attempts_exhausted`）才推进。
5. `reset_supervisor_event` 文档串补一句：需重审某历史 E7 key 时，删除 cursor 中的
   `e7_baseline`（整体删 cursor 文件仍为文档化 reset），水位重建即重扫当前快照全部 swallowed。

## 交付 B（P3 顺手，同单）：E5 deferred / E7 skipped 逐 tick 日志去重/聚合

1. 现状：每 tick 逐条打日志（E5 `deferred:tick_budget`、E7 `skipped:receipt_exists` /
   `skipped:attempts_exhausted` 等），30 分钟约 2400 行。
2. 目标：同一 tick 内同类 action 聚合为一条计数（如 `skipped:receipt_exists:x<n>`），
   并与上一 tick 完全相同的「无进展」动作批降噪（去重打印，不重复刷屏）。

## 交付 C：测试（合成快照两轮扫描）

在 `tests/test_supervisor_events.py`（用 `read_model_for(...)` 合成 `/v1/decisions` 快照，
禁触 :7494 真网）：

1. **首轮零发射**：快照含历史 swallowed（如源 id `e7-msg_01M13x…`）→ 第一次 tick
   `launcher.events()==[]`，cursor 落盘 `e7_baseline` 含该 source_message_id，
   动作注记含 `cursor_adopted:e7_baseline`。
2. **新增一条后仅发射该条**：第二 tick 快照多一条新增 swallowed（`msg_new`）→ 仅发射
   `e7-msg_new`（type/key/payload 精确），历史 id 不重发；后续多 tick 重复扫描不重复发射
   （水位推进）。
3. **restart 后基线仍生效**：新 observer 对象 + 同 state root / cursor → 历史 id 不重发
   （水位持久化）。
4. 改造既有 `test_e7_swallowed_decision_fires` 为水位语义（首跑采基线 + 新增才发射，
   或显式两轮：tick1 基线 → tick2 增一条 → 仅该条发射）。M2 其余既有测试
   （E5/E6 发射与去重、`test_validate_event_still_refuses_unknown_names` unknown 负例、
   budget/attempt 语义）零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 落地后真机复检（监督面/worker，不并入本 dd acceptance）

- 修复合入 + 重新部署 observer 后，journal 复检（`journalctl --user -u fleet-graph*` /
  `/data/fleet-graph/logs/supervisor-*.log`）`launched` 不再出现 `e7-msg_01M13x`/`01M14x`
  历史 key；仅对部署后**新** swallowed 判决出现 `e7-` 发射。

## 铁律

- 只读：observer/read-model 不写被观察工件、不写 git、不获写权限；E7 水位只写 supervisor
  自己的 `supervisor-cursor.json`。
- 一切改动走 PR（本 development worktree），不直改 main；生产主 checkout 仅 ff-only pull。
- 判据（goal.md 验收断言）只有用户能改。