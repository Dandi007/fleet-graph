# engine.py：每 goal 一个常驻引擎进程，把 goalgraph/ddgraph/mergegate/prlifecycle/agentrun 接成一根线

## 背景
DD-01..20 已把最小系统的全部零件落在 `src/fleet_graph/minimal/`（protocol, events, enroll, gitgate, runroot, dispatch, prompts, agentrun, acceptance, ddflow, control, steer, scribe, stagerunner, prlifecycle, mcptools, harness, goalgraph, ddgraph, mergegate）。但**没有任何入口把它们接起来**：`goalgraph.GoalDeps.run_dd` / `final_merge` 是空 seam，`ddgraph.DDDeps` 的 `pr_open` / `pr_cleanup` / `pr_mergeable` / `merge_fn` / `bash_runner` 也是 seam。design §1 / §7.2 + GO-7 / GO-9 / GO-10：引擎是常驻进程、一个 goal 一个进程、由 enroll MCP spawn。这张 DD 只做接线。

## 要改什么
新增 `src/fleet_graph/minimal/engine.py` 与 `tests/test_minimal_engine.py`。

1. **CLI**：`python -m fleet_graph.minimal.engine --goal-id g-xxxxxx --engine-root /data/fleet/goals`（`--engine-root` 缺省 `runroot.DEFAULT_ENGINE_ROOT`）。argparse；导出 `main(argv: list[str] | None = None) -> int` 便于测试；加 `__main__` 保护。
2. **读 enroll**：从 `runroot.GoalRunRoot` 给出的 `goal.enroll.json` 路径读 enroll 对象。这里**不重跑 enroll 校验**（那是 MCP 侧的事），只核文件存在 + `schema` 键正确；缺失/损坏 → 非零退出并打印一行错误，不抛裸栈。
3. **build_deps**：构造 `events.EventLog`、`control.ControlLog`、`gitgate.SubprocessGitRunner`、acceptance 的 bash runner、`agentrun` 的 AgentInvoker（`--session-root` 指向 GoalRunRoot 的 `sessions/`，harness 用 `harness.profile_for_role`），拼成 `goalgraph.GoalDeps`。**一律使用各模块已有的真实 API，不发明新签名**；确需的小适配函数写在 engine.py 内。
4. **run_dd seam**：用 `ddgraph` 跑一张 DD——把 Goal Agent 的 dispatch 对象 + `DDDeps`（`pr_open`/`pr_cleanup`/`pr_mergeable` 取 `prlifecycle` 同名函数；`merge_fn` 取 `mergegate` 的决策+执行；`goal` / `release_branch` / `release_head` 从 enroll 与 event fold 派生）拼好，返回 protocol §7 的 DD 结果对象交回 goalgraph。
5. **final_merge seam**：调 `mergegate` 的 release→target 收尾路径，每个 repo 一次，返回 `(stop, payload)`，stop ∈ merged/rebased/failed。
6. **进程生命周期 event**：起来写 `engine.started`（含 `pid`），退出写 `engine.exiting`（`stop` = done/blocked/stopped）。若 `events.py` 未注册这些 kind，按 DD-13/DD-18 先例在 `events.py` 注册（只加 kind，不改其它行为）。退出码：done→0、blocked→1、stopped→0、启动期错误→2。
7. **恢复 = 回放**（design §7.1 / GO-16）：进程起来只从 `events.jsonl` fold 出状态，绝不从 LangGraph checkpointer 续跑；崩溃不自动 resume（由 MCP `goal_resume` 再 spawn）。

## 不要改什么
- 不改 `goalgraph.py` / `ddgraph.py` / `mergegate.py` / `prlifecycle.py` / `stagerunner.py` 的任何一行（只从外面注入）。
- 不写 MCP transport（另一张 DD）、不接 scribe（另一张 DD）。
- 不新增第三方依赖；不改 `docs/specs/**`；不动 `pyproject.toml` 除非要加 console script（不需要就别加）。

## 怎么验收
`tests/test_minimal_engine.py` 至少覆盖：
1. `main()` 的参数解析与 `--engine-root` 默认值；
2. 缺 `goal.enroll.json` 时返回非零且只打印一行错误；
3. **端到端（全假 seam）**：tmp_path 当引擎根，假 AgentInvoker 依次回放 dispatch → impl → review pass → review pass → approve → merged、第二 turn 回 done，假 git runner + 假 bash runner，跑完后 `events.jsonl` 里按序含 `engine.started` / `goal.turn.started` / `dd.*` / `goal.done` / `engine.exiting`，且退出码 0；
4. 回放幂等：对同一份 `events.jsonl` 再 fold 一次得到同一状态。

langgraph 缺席时按 dd-18 先例做 per-test skip（不要模块级 importorskip，会 exit 5）。`make verify` 必须全绿。
