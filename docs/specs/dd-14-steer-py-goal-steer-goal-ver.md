# steer.py：goal_steer 打补丁 + goal_version + steer_diff 投射（GO-20）

## 背景

GO-20（golden-order.md:87-90）：`goal_steer` 可改或**新增**任意 goal 字段；每次 steer `goal_version` +1，落 `goal.steered` event 记 diff；Goal Agent 下一个 turn 的输入注入 `goal_version` 与自上次 turn 以来的 `steer_diff`（这是本次交接内容，按 GO-17 直接注入，不只给路径）。

现状缺口：`control.py` 已能校验 `op: steer`（含 patch 非空、不许碰 `goal_id`/`work_folder`/`repo.path`），`events.fold` 已从 `goal.steered` 派生出 `goal_version`，`prompts.build_goal_turn_in` 已经**接收** `goal_version` 与 `steer_diff` 两个参数——但**没有任何代码算出这两个值**，也没有代码把 patch 应用到 goal 对象上。这张 DD 补齐。

## 要改什么

只新增 `src/fleet_graph/minimal/steer.py`，纯函数，零 IO：

1. `apply_patch(goal_obj, patch) -> (new_goal_obj, diff)`：深拷贝，**绝不改入参**（要有测试守住）。`diff` 形如 `{"changed": {...}, "added": {...}}`，按 key 在原对象里是否已存在拆分 —— 这是 protocol §2 `steer_diff[]` 里 `changed`/`added` 两个字段的语义。只处理顶层 key；嵌套 dict 一律**整值替换**不做递归 merge，并把这个选择写进 docstring（机械化原则：不猜用户想合并还是想覆盖）。
2. `steered_payload(version, diff, note) -> dict`：`goal.steered` 的 event payload（protocol §8：version, diff, note）。
3. `steer_diff_since(events, since_seq) -> list[dict]`：从 `seq > since_seq` 的 `goal.steered` event 造 §2 的 `steer_diff` 条目 `{version, ts, changed, added, note}`，按 seq 升序。
4. `current_goal(enroll_obj, events) -> (goal_obj, version)`：按 seq 序把所有 `goal.steered` 的 patch 依次 fold 到 enroll 对象上，得到 §2 `goal` 字段该带的**当前值**；`version` = enroll 为 1，每条 steered +1。
5. `IMMUTABLE_FIELDS`：`apply_patch` 碰到不可变字段抛 `ValueError`。这与 `control.validate_control` 的规则**必须一致** —— 请直接读 control.py 现有的那份规则并在 docstring 里点明这是 defence-in-depth 的第二道，不要定出一套不同的清单。

## 不要改什么

- 不写 control.jsonl、不写 events、不改 `control.py` / `events.py` / `prompts.py` 的公开签名。
- 不做磁盘上的版本号自增（版本号只从 events fold 出来 —— events.jsonl 是唯一状态来源，protocol §11）。
- 不 `import langgraph`、不调 agent。
- `__init__.py` 只在 docstring 模块清单加 `steer`。

## 验收要点

`added` 与 `changed` 拆分正确；入参未被改动；不可变字段被拒；N 次 steer 重放后 version == N+1 且同 key 后写覆盖先写；`steer_diff_since` 按 seq 过滤且保序；空 patch 的行为与 control.py 的校验一致。
