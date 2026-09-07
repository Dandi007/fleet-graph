# stagerunner.py：把一个 agent stage 端到端跑通（前置闸→prompt→agent→校验→后置闸）

## 背景

release 上已合 12 个 minimal 模块，全部是叶子：`src/fleet_graph/minimal/__init__.py` 的 docstring 明确写着 modules from different DDs are intentionally not imported by one another。于是今天**没有任何代码能把一个 agent stage 跑完**——gitgate 会核 git、prompts 会渲染 prompt、agentrun 会拼 argv 和解 Stop、protocol 会校验 schema、events 会写日志，但没人把它们串起来。这张 DD 就补这条缝，它是后面 LangGraph 图的必经前置。

规则出处：protocol.md §0.10 的节点前后核对表（Impl / CR / FR / Merge / Goal 各自调用前核对什么、Stop 后核对什么）、§0.2（runtime 非零退出 = 该 agent 失败，**引擎不重跑**）、§0.1（缺 schema/stop 或 stop 不在枚举内判无效输出）、GO-28（交接核对是「已 push + HEAD == remote tip + 工作树干净」，agent 输入不带 sha）、GO-25（能机械化的都机械化）。

## 要改什么

只新增 `src/fleet_graph/minimal/stagerunner.py`，纯编排，**所有 IO 注入**：

1. `StageRequest` dataclass：`stage`（`impl|cr|fr|goal_turn|goal_review|merge|scribe`）、`run_id`、`in_obj`（已由 prompts 的 `build_*_in` 造好，本模块不造）、`repos: list[gitgate.RepoRef]`、`expected_schema`、`policy`（agentrun 的 session 策略）、`cwd`、`is_first_call`。
2. `StageOutcome` dataclass：`ok`、`stop`、`obj`、`invalid_reason`、`gate_failures`、`events: list[tuple[str, dict]]`。`events` 是**待写**的 (kind, payload) 列表，本模块**绝不自己写盘**——由调用方交给 `events.EventLog`，这样 stagerunner 保持零 IO、可纯测。
3. `run_stage(req, *, git_runner, agent_invoker) -> StageOutcome`，顺序固定：
   - **前置闸**：`gitgate.check_handoff(req.repos, runner=git_runner)`。不过 → `ok=False`，events 记一条 `agent.invalid_output`（payload 带 gate 失败项与 `phase: pre`），并且**不调 agent**（这条要有测试守住：invoker 的调用次数为 0）。
   - **渲染 prompt**：按 `prompts.needs_system_prompt(req.policy, is_first_call=req.is_first_call)` 决定要不要 `render_system_prompt`；`render_user_prompt(req.in_obj)` 每轮都要。
   - **调 agent**：注入的 `agent_invoker(argv_or_spec) -> (exit_code, stdout)`；argv 用 `agentrun` 现成的构造函数，不要在这里另拼一套。
   - **非零退出** → `ok=False`，events 记 `agent.failed`（detail 取 stdout 里的 `runtime.error/1` 或 stderr 摘要）。**不重跑**（§0.2）。
   - **解 + 校验**：`protocol.extract_protocol_object(stdout, prefix)` 再 `protocol.validate(obj, req.expected_schema)`。不过 → `ok=False` + `agent.invalid_output`。
   - **后置闸**：再 `check_handoff` 一次；`stage in (cr, fr)` 额外要求工作树与 tip 与前置闸取到的值**一致**（review 不许改代码，改了判无效，§0.10 表）。
   - 全过 → `ok=True`，events 依次 `agent.exited`、再按 stage 记 `dd.stage.finished`（DD 内）或 `goal.turn.finished`（goal_turn）。

## 不要改什么

- 不写 events（只返回待写列表）、不直接调 `subprocess`/git（一律走注入的 runner）、不 `import langgraph`。
- 不决定下一个 stage（那是 `ddflow.next_stage` 的事）、不做 merge、不开关 PR、不跑验收命令。
- 不改任何已合模块的公开签名。若 `prompts` / `agentrun` 的现有签名不够用，**只加关键字参数带默认值**，不改已有调用点。
- `__init__.py` 只在 docstring 的模块清单里加 `stagerunner`，不 import 它（沿用现有纪律）。

## 注意

前面 6 张 DD 都在 rebase 时撞过 `__init__.py` docstring 模块清单和 `pyproject.toml` 的 `pythonpath`；冲突就两边都保留。
