# ddgraph.py：DD 循环的 LangGraph 图（Impl→验收→CR→FR→Goal审单→Merge→cleanup）

## 背景

minimal 层零件已齐，缺的是编排。design.md §3 定死 DD 循环，GO-16 要求引擎基于 LangGraph 实现。仓里 `src/fleet_graph/minimal/` 已有全部可复用件（自己读源码确认签名，不要凭记忆）：

- `ddflow.py`：`STAGES` / `next_stage(stage, stop) -> Transition` / `advance(state, stage, stop) -> DDState` / `DDState(round, approve_valid, stage, outcome, history)` / `warnings_for` / `build_dd_result(events, dd_id)`
- `stagerunner.py`：`run_stage(StageRequest, ...) -> StageOutcome`（前置闸→prompt→agent→校验→后置闸；零 IO，待写事件以 `events: list[tuple[kind, payload]]` 返回）、`AgentInvoker` Protocol、`STAGES`、`REVIEW_CHANGED_CODE`
- `acceptance.py`：`run_acceptance` / `acceptance_event_payload` / `combine_acceptance` / `Runner` / `BashRunner`
- `prlifecycle.py`：`open_pr` / `pr_mergeable` / `close_pr` / `remove_worktree` / `delete_remote_branch` / `cleanup_dd` / `PrRef` / `CleanupRepo`
- `gitgate.py`：`check_dd_ready(list[DDRepoRef])` / `check_handoff` / `GitRunner` / `SubprocessGitRunner` / `GateResult` / `GateFailure`
- `prompts.py`：`build_impl_in` / `build_review_in` / `build_goal_review_in` / `build_merge_in` / `render_system_prompt` / `render_user_prompt` / `needs_system_prompt` / `history_handle`
- `events.py`：`EventLog.append(kind, payload, dd_id=None)` / `EventLog.read(since_seq)` / `fold`
- `agentrun.py`：`resolve_session_policy(role, overrides)` / `schema_for(role, call_kind)` / `SessionPolicy` / `FailureCode`

`langgraph==1.2.11` 已在 pyproject 依赖里。

## 要做

新增 `src/fleet_graph/minimal/ddgraph.py` 与 `tests/test_minimal_ddgraph.py`。

**1. `DDDeps`（frozen dataclass）**：注入全部 IO 与策略，模块内不得有任何全局 IO。字段至少含 `event_log`（带 `.append(kind, payload, dd_id=)`）、`agent_invoker`（`stagerunner.AgentInvoker`）、`git_runner`（`gitgate.GitRunner`）、`bash_runner`（`acceptance.Runner`）、`pr_open` / `pr_cleanup` / `pr_mergeable`（可调用，签名照 `prlifecycle` 同名函数）、`merge_fn`（`Callable[[state], tuple[str, dict]]`，返回 `(stop, payload)`，stop ∈ merged/rebased/failed）、`session_overrides`、`session_root`、`model_by_role`、`timeout_s`、`warn_dd_rounds`。

**本 DD 不实现任何合并逻辑**——`merge_fn` 只是 seam，具体实现是另一张 DD 的 `mergegate.py`。

**2. `DDGraphState`（TypedDict，LangGraph state）**：`goal_id` / `dd_id` / `repos` / `spec_text` / `acceptance_cmds` / `stage` / `round` / `approve_valid` / `history` / `last_stop` / `last_obj` / `feedback` / `prs` / `dd_result` / `terminal`。注意 LangGraph 按 key 整体替换，节点一律返回 partial dict，禁止原地改 state。

**3. 节点**（全是程序节点；任何 agent 调用一律经 `stagerunner.run_stage`，不得自己拼 argv）：

- `dd_ready`：`gitgate.check_dd_ready` 跑 GO-36 的五条机械核（分支在 remote / worktree 在该分支 / HEAD == remote tip / 工作树干净 / spec 文件存在）。不过 → 写 `dd.failed`（`stage="dd_ready"`，payload 带 gate failures）并终结，**且一次 agent 都不调**。
- `open_pr`：每个 repo 开 PR 到 release 分支，写 `dd.pr_opened`。
- `baseline`：在 base 上跑 goal 的 acceptance（**不含** `acceptance_extra`），红 → 写 `dd.failed`（`stage="baseline"`）终结（GO-34 解读：基线验收挪到开好 worktree 之后、Impl 起跑之前）。
- `impl` / `cr` / `fr` / `goal_review`：各自组 `stagerunner.StageRequest`（`in_obj` 用 `prompts.build_*_in`，`policy` 用 `agentrun.resolve_session_policy`，`expected_schema` 用 `agentrun.schema_for`）调 `run_stage`，把 `outcome.events` 逐条写进 `event_log`，再用 `outcome.stop` 查 `ddflow.next_stage` 决定下一步。`outcome.ok is False`（闸失败或无效输出）时按 protocol §0.2：DD 内 agent 失败 → 该 DD 以 `failed` 结束交回 Goal Agent，不重跑。
- `acceptance`：`run_acceptance(goal.acceptance + dispatch.acceptance_extra)` fail-fast，写 `dd.acceptance`，pass/fail 走转移表。
- `merge`：调 `deps.merge_fn`，写 `dd.stage.finished(stage="merge")`。
- `cleanup`：`prlifecycle.cleanup_dd`（merged 即 PR merge/close，failed 则 close 不合并；删 worktree、删远端 dd 分支），写终态 `dd.merged` 或 `dd.failed`。

**4. `build_dd_graph(deps) -> CompiledStateGraph`**：用 `langgraph.graph.StateGraph` 接线。**所有条件边一律查 `ddflow.next_stage`，禁止在图里重写或复制转移规则**（approve 清零、rebased 回 cr 这些语义只有 ddflow 一个来源）。checkpointer 由调用方传入；docstring 必须写清「checkpointer 只是可删缓存，`events.jsonl` 是唯一真相，重启按回放恢复而非靠 checkpointer 续跑」（GO-16 / design §7.1）。

**5. `run_dd(deps, *, goal_id, dd_id, repos, spec_text, acceptance_cmds, checkpointer=None) -> dict`**：跑完整张 DD，返回 `ddflow.build_dd_result(deps.event_log.read(), dd_id)`。

## 不改什么

- 不改 `ddflow` / `stagerunner` / `acceptance` / `prlifecycle` / `gitgate` / `prompts` / `events` / `agentrun` 任何一个字节。
- **不要改 `src/fleet_graph/minimal/__init__.py`**（dd-12~dd-16 连续五张都栽在这个 docstring 模块清单的 rebase 冲突上）。
- 不实现合并逻辑、不写 goal 级 turn 循环、不写进程入口、不写 MCP 传输层。
- 不碰旧的 `src/fleet_graph/graphs/`、`src/fleet_graph/dd/`、`src/fleet_graph/supervise/`。
- 模块里不得出现裸 `subprocess` / `open()` / `Path.write_*`：IO 全走注入件。
- 不改 `pyproject.toml` / `Makefile` / 不加第三方依赖。

## 验收

`tests/test_minimal_ddgraph.py` 用假 `agent_invoker` / 假 `git_runner` / 假 `bash_runner` / `langgraph.checkpoint.memory.InMemorySaver` 覆盖：
1. happy path `impl→acceptance→cr→fr→goal_review→merge→cleanup` 得 `merged`，断言事件序列与 `build_dd_result` 的 outcome/rounds 一致；
2. cr `fail` 回 impl，`approve_valid` 清零、`round` +1；
3. merge `rebased` 回 cr 且 approve 清零（GO-15）；
4. `dd_ready` 不过 → `dd.failed` 且 agent_invoker 调用次数为 **0**；
5. baseline 红 → `dd.failed(stage=baseline)`，不进 impl；
6. `run_stage` 返回 `ok=False` → 该 DD `failed`，且引擎**不**重跑该 agent；
7. 断言图不含旁路转移：对未知 (stage, stop) 组合 `ddflow.next_stage` 抛 ValueError 且图把它作为错误暴露，不吞。

`make verify` 全绿，ruff check / ruff format 干净。
