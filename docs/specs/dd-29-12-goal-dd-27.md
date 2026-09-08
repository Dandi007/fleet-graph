# 让书记员在生产接线里真的跑起来 + 补齐 §12 的 goal 级触发点（dd-27 重派，去掉自相矛盾）

## 背景

线的目标明确包含「五个协议 agent + Scribe」。书记员的**能力**已经齐了（`scribe.py` 的 ObservationLog / 证据闸、`goalgraph.run_scribe`、`harness` 的 minimal-scribe profile、`mcptools.goal_observations`），但**生产上它一次都没跑过**：

- `goalgraph.GoalDeps.scribe_enabled` 默认 `False`（`src/fleet_graph/minimal/goalgraph.py:110`）；
- `engine.build_deps` 构造 `GoalDeps` 时（`engine.py:505-517`）根本没传这个字段。

所以 `fleet-graph-minimal-engine` 起的每个 goal，书记员都是关着的。

另外 protocol §12「触发」段列了五个 goal 级边界：`goal.turn.finished`、`dd.merged` / `dd.failed`、`goal.done` / `goal.blocked`、`goal.warning`。今天 `goalgraph` 只接了前两组（`goalgraph.py:502` 与 `:555`），`goal.done` / `goal.blocked` / `goal.warning` 三处没有。

**这张 DD 是 dd-27 的重派。** dd-27 之所以 failed，是因为它的 spec 一边要求新增 `goal.done` / `goal.blocked` 触发，一边又禁止改 `tests/test_minimal_goalgraph_scribe.py` 一个字节——而该文件 6 个 `scribe_enabled` 用例都跑到 `goal.done`，新增触发必然让 scribe 多跑一次。本 spec **显式解除**那条禁令（见下）。implementer 当时的判断是对的，不要重复那个坑。

## 要改什么

1. `src/fleet_graph/minimal/engine.py`：`build_deps` 增加一个关键字参数 `scribe_enabled: bool = True`，透传进 `goalgraph.GoalDeps(...)`；`run_engine` 同样增加 `scribe_enabled: bool = True` 并向下透传。**生产默认开**（书记员只读、永不阻塞流程，这是 GO-21 的定位），测试要关就显式传 False。
2. `src/fleet_graph/minimal/goalgraph.py`：在 `final_merge_node` 写完 `goal.done` 之后、`finish_blocked` 写完 `goal.blocked` 之后，各加一次 `run_scribe(state, trigger="goal.done")` / `run_scribe(state, trigger="goal.blocked")`。**顺序要求**：先写终态 event，再起书记员，这样书记员的 seq 区间能覆盖到终态那条。
3. `goal.warning` 触发：`goalgraph` 里写 `goal.warning` 的地方有多处（`:362` `:379` `:388` `:406` `:410` `:430` `:586`），逐处都起书记员会很吵。**只在 turn 边界那一处**（`:430`，把 MCP message 转成 warning 的那条，以及 `:586` 的收尾 handoff warning）之后触发；control 解析失败类的内部 warning（`:362` `:388` `:406` `:410`）不触发。在代码注释里写明这个取舍，理由是「避免噪声」，protocol §12 原文就是这么要求的。
4. `docs/specs/minimal/context.md` 的「已实现」段补一行：书记员已在生产接线里默认启用，五个 §12 触发点全部接上。

## 不改什么

- 不改 `scribe.py`、`prompts.build_scribe_in`、`harness.py`、`mcptools.goal_observations`、`protocol.py` 的 `scribe/1` 校验——这些都已完成且已合。
- 不改 `run_scribe` 内部的失败语义：书记员 runtime 非零、输出不合 schema、observation 无证据，一律只落 `scribe.failed` / `agent.failed`，**永不**让 goal 循环失败或阻塞。这条是 GO-21 的红线，改动后必须仍然成立。
- 不动 `goalgraph` 里 `dd.stage` 级的任何东西：书记员只在 goal 级边界跑，不进 DD 内每个 stage。
- 不改 `docs/specs/minimal/golden-order.md`（只增不改，且本次无新用户原话）。
- 分支 diff 只允许含 `src/fleet_graph/minimal/{engine,goalgraph}.py`、`tests/` 下相关文件、`docs/specs/minimal/context.md`。

## 明确允许（dd-27 的坑）

**允许并且预期**你会改 `tests/test_minimal_goalgraph_scribe.py`：新增的 `goal.done` / `goal.blocked` 触发会让既有用例里书记员的调用次数从 3 变 4。请把断言按新的真实行为更新，并在测试里补上「`goal.done` 之后确实又跑了一次书记员、且其 seq 区间包含 `goal.done` 那条 event」的正向断言。不要为了不改测试而阉割功能。

## 怎么验收

- `make verify` 全绿。
- 新增测试覆盖：① `engine.build_deps` 默认产出的 `GoalDeps.scribe_enabled is True`；② 显式传 `scribe_enabled=False` 时为 False；③ `goal.done` 收尾后 `observations.jsonl` / `scribe.observed` 多了一次，且区间含终态 event；④ `goal.blocked` 同理；⑤ 书记员整个失败（invoker 抛错或返回非零）时 goal 仍以 `done` 正常收尾、只多一条 `scribe.failed`。
