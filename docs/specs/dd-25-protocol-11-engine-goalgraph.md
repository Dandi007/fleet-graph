# 引擎恢复=回放：把 protocol §11 的续跑点接进 engine/goalgraph

# 背景（自含）
`release/loopx-minimal` 上 `events.resume_point()`（src/fleet_graph/minimal/events.py:329）与 `engine.resumed` kind（events.py:62）都已存在，但**无人调用**。`engine.run_engine`（src/fleet_graph/minimal/engine.py:537）只在 `events.fold` 出终态时直接退出，否则以 `turn_no=0 / last_seq=0 / dd_summary=""` 从头调 `goalgraph.run_goal`（goalgraph.py:632）。后果：崩溃或 `goal_resume` 之后引擎会重发 turn 1、把 control.jsonl 的历史行整批重放（重复写 `control.received`）、in-flight DD 静默丢失。设计正本 `docs/specs/minimal/protocol.md` §11 已定「events.jsonl 是唯一状态来源，恢复=回放」并给了续跑表；`mcptools.goal_resume` / `mcpserver` 的 resume spawn 已就位，只差引擎侧。

# 要改什么
1. **`goalgraph.run_goal` 增参** `initial_state: dict[str, Any] | None = None`：None 时行为与今天**逐字一致**（既有 tests/test_minimal_goalgraph*.py 一个字节不许改），非 None 时用它覆盖 initial dict 的键。
2. **`engine.py` 内新增纯函数**（不新建模块）`resume_initial_state(events) -> dict | None`：用 `events.fold` + `events.resume_point` 折出 `turn_no`、`goal_version`、`last_seq`（**control.jsonl 的游标 = 已落 `control.received` 事件 payload 里最大的 seq**，注意这是 control seq 空间，不是 event seq）、`dd_summary`（由 `fold.dd_history` 生成一行，语义与 goalgraph 现有 `_one_line_dd_summary` 一致）、`warnings`（把 lost_on_restart 之类作为下一轮交接内容注入）。docstring 必须写清 turn_no 与 goalgraph 里「run_dd 节点 +1」这一语义如何对齐。
3. **`run_engine` 分派**：日志非空且非终态时先写 `engine.resumed {"from_seq": fold.last_seq}`（终态仍按今天直接退出，且**不写** resumed），再按 `resume_point.action`：
   - `restart_step` 且 stage=="goal_turn"：写 `agent.failed {"stage":"goal_turn","detail":"lost_on_restart"}`，重跑同一个 turn（初始 turn_no 回退一格，使重跑的 turn 号等于丢失的那个）。
   - `fold.current_dd.dd_id` 非空（DD 在途，含 `rerun_acceptance` / `next_step`）：写 `agent.failed{stage, detail:lost_on_restart}` + `dd.failed {"stage": <open stage>, "detail":"lost_on_restart"}`（`dd_id` 走 EventLog 的 dd_id 参数），把这张 DD 的结论（`ddflow.build_dd_result`）作为下一轮 Goal Agent 的 `last_dd` 与一条 warning，然后正常进下一 turn。
   - 其余：直接进下一 turn。
4. **§11 的 git 一致性核对**：续跑前，若 events 里最近记过的 `release_head` / `head_commit`（`dd.pr_opened` / `dd.merged` / `goal.merged_to_target` payload）在对应远端分支上已不存在（用 `gitgate` 现成能力 + 注入 runner），写 `goal.blocked {"kind":"state_mismatch", ...}` 并按 blocked 退出（exit 1），**不猜**。events 里没有可核对的 sha 时跳过这一步。
5. **偏离必须写明**：protocol §11 表里「in-flight DD 逐 stage 续跑」不在本 DD 范围（要动 ddgraph 的初始状态，另一张 DD）。本 DD 采用「该 DD 以 lost_on_restart 结束、交回 Goal Agent 决定」的收敛方式，必须在 `run_engine` docstring 与 `dd.failed` 的 detail 里显式说明这是范围外的暂行收敛，不许假装 §11 已完整实现。

# 不改什么
不动 `events.py` / `ddflow.py` / `control.py` / `mcptools.py` / `mcpserver.py` / `stagerunner.py` / `prompts.py` / `ddgraph.py`；不动 `src/fleet_graph/minimal/__init__.py`（连续多张 DD 的 rebase 冲突源）；**不碰 `engine.build_deps` 的签名与 goalgraph 里 scribe 相关代码**（同批另一张 DD 在改那里）；不加依赖；不改 `docs/specs/minimal/` 下任何文件；不改 Makefile。

# 怎么验收
`make verify` 全绿；新文件 `tests/test_minimal_engine_resume.py`（langgraph 缺席时按 dd-18 先例做 per-test skip，别用模块级 importorskip）至少覆盖：① 空日志 → 不写 `engine.resumed`、行为与今天一致；② 终态日志 → 立即退出、不写 resumed；③ 最后一条 `goal.turn.started` → 落 `agent.failed(lost_on_restart)` 且重跑的 turn 号等于丢失的那个（用假 invoker 录 in_obj 断言 turn_no）；④ 最后一条 `dd.stage.started` → 落 `dd.failed(lost_on_restart)` 且下一 turn 输入里带该 DD 结论；⑤ 已记 `control.received` seq=3 时，用真 `ControlLog` 写 4 行，resume 后只有第 4 行落 `control.received`；⑥ release_head 在远端不存在 → `goal.blocked(kind=state_mismatch)` 且退出码 1；⑦ `engine.resumed.from_seq == fold.last_seq`。
