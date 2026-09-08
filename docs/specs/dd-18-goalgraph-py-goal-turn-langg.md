# goalgraph.py：goal 级 turn 循环的 LangGraph 图（turn→dispatch→DD→下一轮；done→收尾合并）

## 背景

design.md §2 的线循环：Goal Agent 跑一个 turn，Stop 三选一（dispatch / done / blocked）；dispatch 就是 DD 启动协议（GO-36）；done 触发 release→target 收尾合并（GO-6.3 / GO-15）；blocked 写 event 终结。GO-20 要求 goal_steer 改的版本号与 diff 在下一个 turn 被 Goal Agent 看到。GO-16 定 MCP→引擎只经 `control.jsonl`，引擎在**步骤边界**读。

仓里 `src/fleet_graph/minimal/` 已有件（读源码确认签名）：

- `prompts.py`：`build_goal_turn_in` / `render_system_prompt` / `render_user_prompt` / `needs_system_prompt` / `history_handle`
- `stagerunner.py`：`run_stage(StageRequest) -> StageOutcome`（注意 `stagerunner.STAGES` 里 goal turn 那一档的实际名字，照它写，别自造）
- `dispatch.py`：dispatch 启动协议的字段校验 + 转 `gitgate.DDRepoRef`（GO-36 多 repo）
- `steer.py`：`current_goal(enroll_obj, events) -> (goal, version)` / `steer_diff_since(events, since_seq)` / `apply_patch` / `steered_payload`
- `control.py`：`ControlLog`（读写 control.jsonl）/ `validate_control`
- `events.py`：`EventLog` / `fold` / `DerivedState`
- `ddflow.py`：`build_dd_result`
- `runroot.py`：`GoalRunRoot` / `goal_run_root` / `release_branch` / `read_enroll`
- `agentrun.py`：`resolve_session_policy` / `schema_for("goal", "turn")`

`langgraph==1.2.11` 已在依赖里。

## 要做

新增 `src/fleet_graph/minimal/goalgraph.py` 与 `tests/test_minimal_goalgraph.py`。

**1. `GoalDeps`（frozen dataclass）**：`event_log`、`control_log`、`agent_invoker`、`git_runner`、`run_dd`（`Callable[[dispatch_obj], dict]`，返回 protocol §7 的 DD 结果对象）、`final_merge`（`Callable[[], tuple[str, dict]]`，release→target，默认 None）、`warn_turns`、`session_root`、`session_overrides`、`model_by_role`、`timeout_s`。

**本 DD 禁止 import `ddgraph`**——DD 内部循环只是注入的 `run_dd` seam，两张 DD 因此可并行。

**2. `GoalGraphState`（TypedDict）**：`goal_id` / `enroll` / `turn_no` / `goal_version` / `last_stop` / `last_dd` / `dd_summary` / `pending_messages` / `warnings` / `stop` / `summary` / `blocked` / `last_seq`。节点返回 partial dict，禁止原地改。

**3. 节点**

- `read_control`：在步骤边界读 `control.jsonl` 自 `last_seq` 以来的新行。`goal_message` → 追加进 `pending_messages`；`goal_steer` → `steer.apply_patch` 后写 `goal.steered` event、`goal_version` +1；`goal_stop` → 置终态并终结。未知/非法 op 写 event 忽略，不崩。
- `goal_turn`：`steer.current_goal` 取当前 goal 与 version → `prompts.build_goal_turn_in` 组 `goal.turn.in/1`（含 `steer_diff_since` 的结果、`dd_summary` 一行摘要而非整表〔GO-17〕、`last_dd`、`pending_messages`（**读后即清**）、`warnings`、`history` 句柄）→ `stagerunner.run_stage` → 写 `goal.turn.finished`。`run_stage` 返回 `ok=False` 时按 protocol §0.2：Goal Agent 失败 → goal 置 `blocked`，写 event 终结。
- 条件边按 stop：`dispatch` → `validate_dispatch`；`done` → `final_merge_node`；`blocked` → 写 `goal.blocked` 终结。
- `validate_dispatch`：用 `dispatch.py` 校验并转 `DDRepoRef`。不过 → 写 `goal.dispatch_rejected`（带字段级错误），把错误作为下一轮的交接内容回 `goal_turn`，**不终结**（机械化原则：打回，不猜）。
- `run_dd_node`：调 `deps.run_dd(dispatch_obj)` 拿 DD 结果对象 → 更新 `last_dd` / `dd_summary`、`turn_no` +1 → 回 `read_control`。
- `final_merge_node`：调 `deps.final_merge`。`merged` → 写 `goal.done` 终结；`rebased` / `failed` → 写 event，把结果作为交接内容回 `goal_turn`（GO-15 的「rebase 动了代码要完整再走 CR→FR→Goal」由后续 DD 接，本 DD 只做回流并注入，**docstring 里写明这个边界**）。
- 计数与告警：`turn_no >= warn_turns` 只产 warning 文本进 `warnings` 并写 event，**不停**（GO-6.4，不设硬上限）。

**4. `build_goal_graph(deps)` / `run_goal(deps, *, goal_id, enroll, checkpointer=None) -> dict`**（返回最终 stop 与 summary）。图无硬轮数上限，`recursion_limit` 显式设一个很大的值并在 docstring 说明为什么（GO-6.4）。checkpointer 只是可删缓存，`events.jsonl` 是唯一真相——docstring 写清。

## 不改什么

- 不改 `prompts` / `stagerunner` / `dispatch` / `steer` / `control` / `events` / `ddflow` / `runroot` / `agentrun` 任何一个字节。
- **不要改 `src/fleet_graph/minimal/__init__.py`**（连续五张 DD 的 rebase 冲突源）。
- 不 import `ddgraph`（同批并行）、不实现 DD 内部循环、不实现合并逻辑、不写进程入口、不写 MCP 传输层。
- 不碰旧的 `src/fleet_graph/graphs/` 与 `supervise/`。
- 模块内不得有裸 `subprocess` / 文件读写：IO 全走注入件。
- 不改 `pyproject.toml` / `Makefile` / 不加依赖。

## 验收

`tests/test_minimal_goalgraph.py` 用假 `agent_invoker`（脚本化返回一串 Stop 对象）、假 `run_dd`、假 `final_merge`、`InMemorySaver` 覆盖：
1. 第一轮 `dispatch` → `run_dd` 被调一次 → 第二轮 `done` → `final_merge` merged → 写 `goal.done`；
2. 首轮 `blocked` 直接终结，`run_dd` 零次调用；
3. dispatch 字段非法 → 写 `goal.dispatch_rejected`、`run_dd` **零次**调用、回到 `goal_turn` 且错误进了下一轮 in 对象；
4. `control.jsonl` 里一条 `goal_steer` 使 `goal_version` +1，且下一轮 `goal.turn.in/1` 里 `steer_diff` 非空（GO-20）；
5. `goal_message` 只被注入一次（读后即清），第二轮 `messages` 为空；
6. `turn_no` 越 `warn_turns` 只产 warning、循环继续不终结；
7. Goal Agent `run_stage` 返回 `ok=False` → goal 置 blocked 且不重跑。

`make verify` 全绿，ruff 干净。
