# 端到端 smoke：一条假 agent 的 goal 从 enroll 跑到 done（真 git、真 events.jsonl）

# 背景（自含）
`release/loopx-minimal` 上 23 个 `src/fleet_graph/minimal/*.py` 各有单测，`engine.py` 也把 goalgraph/ddgraph/mergegate/prlifecycle/agentrun 接成了一根线，但**没有任何测试证明接线后的整体能跑**：`engine._run_dd_seam` / `_merge_fn` / `_final_merge_seam`、`ddgraph` 的 pr_open→baseline→impl→cr→fr→goal_review→merge→cleanup、`stagerunner` 的前后置 git 闸、`gitgate.check_dd_ready` 的五条核对，都只在各自的 fake 里验证过。这张 DD 只加端到端 smoke 测试。

# 要做什么
新文件 `tests/test_minimal_e2e_smoke.py`（langgraph 缺席按 dd-18 先例 per-test skip）：
1. **真 git 场景**（tmp_path，全程 `subprocess` 真跑 git，不碰网络）：造 bare `origin.git` + 一个 clone；main 上一个文件；切出 release 分支（如 `release/smoke`）push；再造 dd 分支（如 `dd/smoke-1`），写 `docs/specs/1-smoke.md` 并 commit+push——即 GO-36 里 Goal Agent 应当先做完的前置工作。显式设 `user.name` / `user.email` / 默认分支名。
2. **enroll**：构造 `goal.enroll/2` 对象过 `enroll.validate_enroll`，用 `runroot` 现成 API 建引擎根、写 `goal.enroll.json` 与首条 `goal.enrolled` event（读源码用现成函数，别自己拼路径）。
3. **假 agent invoker**（`stagerunner.AgentInvoker` 形状）：按 stage 顺序返回单行 Stop JSON —— goal turn 1 → `dispatch`（repos 指向上面真造的 dd 分支 / worktree 路径 / spec 相对路径）、impl → `done`、cr → `pass`、fr → `pass`、goal review → `approve`、goal turn 2 → `done`。PR 相关的 gh 调用与 `mergegate` 的合并走注入的假 `gh_runner` / 假 runner（返回 mergeable + 合并成功）；验收命令用真 `acceptance.BashRunner` 跑 `true`。
4. **跑**：直接调 `engine.run_engine(goal_id, engine_root=…, agent_invoker=…, gh_runner=…)`（git 用真 runner），断言：退出码 0；`events.jsonl` 的 kind 序列包含且顺序正确 `engine.started → goal.turn.started → dd.dispatched → dd.pr_opened → dd.acceptance(baseline) → dd.stage.finished(impl/cr/fr/goal_review) → dd.merged → goal.turn.started(2) → goal.merged_to_target → goal.done → engine.exiting(done)`；`events.fold` 出 `state=done`、dd_history 恰一张 `merged`；`control.goal_status_view` / `goal_list_row` 能生成视图不抛。
5. **第二个 case**：把 fr 换成 `fail`（带 blocker）→ 回 impl 再跑一轮 → 断言 approve 清零语义体现在事件流（round 递增、`goal_review` 只在第二轮之后出现、最终仍收敛到 merged）。

# 不改什么
不改 `src/` 下任何语义——**唯一例外**是 smoke 暴露的真 bug，改动必须最小且在 PR 描述里逐条列出（哪个断言红、改了哪几行、为什么）；不 spawn 子进程跑 engine（直接调 `run_engine`）；不碰网络、不调真 `gh`；不动 `src/fleet_graph/minimal/__init__.py`；不改 `docs/specs/minimal/`、Makefile、pyproject。

# 怎么验收
`make verify` 全绿 + 下面的额外命令。测试必须真起 git 进程（允许），单个用例 30s 内跑完；不得用 sleep 等待。
