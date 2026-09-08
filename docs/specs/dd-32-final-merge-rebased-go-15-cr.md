# final_merge rebased 后按 GO-15 完整再审（CR→FR→Goal 审单）再合

## 背景

design.md §2.4 / §7.7 与 protocol.md §6「rebased」条明确要求：release→target 的收尾合并若 Merge Agent 返回 `rebased`（rebase 动了代码），引擎必须对 rebased 的 release 分支**完整再走 CR → FR → Goal Agent 审单**，Goal approve 后再调一次 Merge Agent 才合。

现状：`src/fleet_graph/minimal/goalgraph.py` 的 `final_merge_node`（约 636-666 行）在 `rebased` / `failed` 时只落一条 `goal.warning` 并把文本塞进下一个 turn 的 `warnings`，让 Goal Agent 自己决定。该函数的 docstring 明写这是 dd-18 故意留的边界：「GO-15's rule that a rebase which touched code must re-run CR → FR → Goal review in full is a later DD's graph — nothing here re-reviews」。本 DD 就是关掉这条边界。

注意：**DD 内**的 merge 路径已经按 GO-15 实现（`ddflow._TRANSITIONS` 的 `merge: rebased → {next_stage: cr, approve_reset: True}`，`ddgraph` 已接），本 DD 只补 **goal 级收尾合并**这一处。

## 要改什么

1. `goalgraph.py` 的 `final_merge_node`：当 `deps.final_merge()` 返回 `("rebased", payload)` 时，不再只落 warning，而是对 rebased 的 release 分支跑一遍 CR → FR → Goal 审单：
   - 用 `prompts.build_review_in` 造 `review.in/1`，`role` 依次 `cr` / `fr`；按 protocol §6 明文：`base_commit` = 目标分支 head、`head_commit` = payload 的 `new_head`、`spec_text` = goal 的 `goal_text`。`acceptance_results` 用 goal 级验收命令的本轮结果，没有就空数组。
   - 经 `stagerunner.run_stage`（stage 分别为 `cr` / `fr`）执行，事件照常由 stagerunner 产出并 append。
   - CR 或 FR `fail` → 落 `goal.warning`（message 带 findings 摘要），清 `stop`，把结果作为下一个 turn 的交接内容回到 `goal_turn`；**不自动重试、不猜**。
   - 两者都 `pass` → 用 `prompts.build_goal_review_in` 起 Goal 审单（stage `goal_review`）：`approve` → 再调一次 `deps.final_merge()`；`reject` → 落 event、清 `stop`、把 reject 的 `message` 作为交接内容回 `goal_turn`。
2. 第二次 `final_merge()` 返回 `merged` 时走原有路径：`goal.merged_to_target` → `_append_progress(goal.done)` → `goal.done` event → `run_scribe(trigger="goal.done")`（顺序不变，§12 要求先落终态 event 再起书记员）。
3. **防死循环**：第二次仍返回 `rebased` 时不再进第三轮再审，直接落 `goal.warning` 并回 `goal_turn`——一次收尾合并最多再审一轮。
4. `failed` 分支维持现状（落 warning 回 `goal_turn`），不改。

## 不改什么

- 不改 DD 内的 merge 路径：`ddgraph.py`、`ddflow.py` 的转移表一个字节都不动。
- 不改 `mergegate.py`，也不改 `engine.py` 的 `_final_merge_seam` 签名——seam 仍是 `() -> tuple[str, dict]`。
- 不改 `docs/specs/minimal/` 下任何文件（正文已写明该规则，本 DD 只补实现）。
- 不新增 event kind：复用已在 `events.py` 注册的 `goal.warning` / `goal.merged_to_target` / `goal.done` 与 stagerunner 既有的 `dd.stage.*`。
- 不动 §12 书记员的既有五个触发点，也不新增触发点。

## 怎么验收

`make verify` 全绿。另在 `tests/test_minimal_goalgraph.py` 补测试覆盖四条路径：① rebased → CR pass → FR pass → Goal approve → 第二次 merged → `goal.done`（`final_merge` 被调用 2 次）；② rebased → CR fail → 回 `goal_turn`；③ rebased → CR/FR pass → Goal reject → 回 `goal_turn`；④ 连续两次 rebased → 回 `goal_turn`，不进第三轮。
