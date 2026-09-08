# 让书记员在生产接线里真的跑起来 + 补齐 §12 的 goal 级触发点

# 背景（自含）
dd-23 把 Scribe 接进了 `goalgraph`，但 `GoalDeps.scribe_enabled` 默认 False（src/fleet_graph/minimal/goalgraph.py:110），而 `engine.build_deps` 构造 `GoalDeps` 时（engine.py:503）**没有传这个参数**——所以生产里第六个 agent 一次都不会跑，goal 语句里的「+ Scribe」目前是死代码。另外 `docs/specs/minimal/protocol.md` §12 的触发点是 `goal.turn.finished`、`dd.merged`/`dd.failed`、`goal.done`/`goal.blocked`、`goal.warning`，而 goalgraph 现在只在前两类触发（goalgraph.py:502 / 555）。

# 要改什么
1. `engine.py`：`build_deps` 与 `run_engine` 增 `scribe: bool = True`，CLI 加 `--no-scribe`（默认开），传进 `goalgraph.GoalDeps(scribe_enabled=…)`。**只改这两处签名与 GoalDeps 构造**，不动 `run_engine` 的恢复/退出逻辑（同批另一张 DD 在改那里）。
2. `goalgraph.py`：在 `final_merge_node` 写完 `goal.done` 之后、以及 `finish_blocked` 写完 `goal.blocked` 之后，各调一次 `run_scribe`（trigger 分别 `goal.done` / `goal.blocked`）；`goal.warning` 的触发只在「本轮新写过 goal.warning」时跑一次以免噪声（判据自定，写进 docstring）。硬约束不变：只读、失败只落 `scribe.failed`、**绝不**改流程与退出码、绝不写 control.jsonl、绝不碰 git。**只改 `run_scribe` 与这两个节点**，不动 `read_control` / `goal_turn` / `validate_dispatch` / `run_dd_node` / `run_goal`。
3. 核对 harness 与路径：scribe stage 实际用的 profile 是 `profiles/harness/minimal-scribe.json`（`engine._apply_harness_profile` 按 role 解析），session 落 `<goal_run_root>/sessions/`；`scribe.ObservationLog` 落的 observations 路径要与 `runroot` 的约定一致。三者若对不上就修到对上（以 `runroot` 为准），并加断言测试。

# 不改什么
不改 `scribe.py` 的证据闸语义、不改 `protocol.py` / `prompts.py` / `events.py`；不动 `src/fleet_graph/minimal/__init__.py`；**既有 tests/test_minimal_goalgraph.py 与 tests/test_minimal_goalgraph_scribe.py 一个字节都不许改**；不改 `docs/specs/minimal/`；不加依赖。

# 怎么验收
`make verify` 全绿；新文件 `tests/test_minimal_engine_scribe.py`（langgraph 缺席 per-test skip）至少覆盖：① 默认 `run_engine` 下 scribe 被调用（假 invoker 记录到 `scribe` stage）且 `observations.jsonl` 有行；② `--no-scribe` / `scribe=False` 时一次都不调、事件流里没有任何 `scribe.*`；③ scribe 的假 invoker 抛异常 / 返回非法输出时，goal 仍走到 done、退出码 0，只多一条 `scribe.failed`；④ `goal.done` 与 `goal.blocked` 各触发一次（断言 event payload 的 trigger 值）；⑤ scribe 用的 harness profile 名与 session_root 逐 token 断言。
