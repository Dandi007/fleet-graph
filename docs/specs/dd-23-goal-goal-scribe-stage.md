# 把书记员接进 goal 循环：goal 级边界触发 scribe stage，只读且永不阻塞流程

## 背景
GO-21：书记员是第六个 agent，**只读、不参与流程、不改任何状态**，持续读 L0（events.jsonl + 各 agent session 文件 + 各 Stop 输出），产出带 L0 证据指针的 L1 observation。DD-15 已落 `scribe.py`（L1 observation 存储 + protocol §12 证据闸），DD-10 已落 `prompts.build_scribe_in` 一类输入构造，DD-13 已落 `stagerunner.run_stage`，DD-20 已落 `profiles/harness/minimal-scribe.json`。但 `goalgraph.py` 的 import 里根本没有 scribe——书记员从没被调起来过。

## 要改什么
改 `src/fleet_graph/minimal/goalgraph.py`（尽量小改动），并补 `tests/test_minimal_goalgraph_scribe.py`。

1. 在 `GoalDeps` 上加一个**可选** seam（例如 `scribe_enabled: bool = False` 或 `scribe_fn: Callable | None = None`，二选一，选你觉得与既有风格一致的那个）。缺省不接时，goal 图的行为与今天**逐字一致**（既有 goalgraph 测试一个都不许改）。
2. 触发时机：**goal 级边界**——每个 turn 结束后、每张 DD 结束后各跑一次（design §1 节点表「书记员 / agent（只读）/ goal 级边界」）。用 `stagerunner.run_stage` 跑，role=`scribe`，harness profile 走 `harness.profile_for_role("scribe")`，session 走 §0.8 默认表的 resume + compact_at 0.6，session 目录在 `<goal_run_root>/sessions/` 下。
3. 输入用 `prompts` 已有的 scribe 输入构造器：`until_seq` 取上次观测后的游标，历史只给句柄（GO-17）。游标本身从 event 日志 fold 出来，**不新增状态文件**。
4. 输出按 `protocol.SCHEMA_SCRIBE` 校验，再过 `scribe.py` 的 §12 证据闸；通过的 observation 追加到 `scribe.py` 规定的存放位置，并写一条 event（`scribe.observed` 之类；kind 未注册就按 DD-13 先例在 `events.py` 注册）。
5. **永不阻塞流程**：scribe 无效输出、agent 失败、证据闸不过、超时——一律只写 event（如 `scribe.failed`）然后继续跑 goal 循环，绝不让 goal 变 blocked、绝不让 DD 变 failed。scribe 也绝不写 control.jsonl、绝不碰 git、绝不改 goal 状态。

## 不要改什么
- 不改 `scribe.py` / `stagerunner.py` / `prompts.py` / `ddgraph.py` / `harness.py` 的语义（只调用）。
- 不在 DD 内部循环里插 scribe（GO-21 说的是 goal 级边界）。
- 不改 `docs/specs/**`，不改 `engine.py`（并行 DD 在写；本 DD 只保证 seam 默认关时零行为变化）。

## 怎么验收
新测试至少覆盖：
1. seam 未接时，goal 图产生的 event 序列与接之前**完全一致**（无 scribe.* event）；
2. 接上假 invoker 后，一个「turn → DD → turn(done)」的跑法里 scribe 恰好被调用 N 次且都在 goal 级边界（断言 event 相对顺序）；
3. scribe 返回无效 JSON / 证据闸不过 / invoker 抛异常 三种情况下，goal 仍然正常跑到 `goal.done`，且各写了一条失败 event；
4. observation 的 `until_seq` 游标单调递增，不重复观测同一段 event。

langgraph 缺席时按 dd-18 先例 per-test skip。既有 `tests/test_minimal_goalgraph*.py` 必须一行不改地继续全绿；`make verify` 全绿。
