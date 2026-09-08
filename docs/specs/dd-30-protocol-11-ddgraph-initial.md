# protocol §11 补完：ddgraph initial-state seam，DD 内逐 stage 续跑（不再把在飞的 DD 直接判 failed）

## 背景

`engine.py:729-734` 的 docstring 自己写着：

> Scope note (deliberate deviation, protocol §11): per-stage in-flight DD resume — re-entering the ddgraph at the lost stage — is *not* implemented here (it needs a ddgraph initial-state seam, tracked as a separate DD). The in-flight DD therefore ends immediately as `failed` with `detail=lost_on_restart` … this is a temporary convergence, not a claim that §11 is fully implemented.

这张 DD 就是那个 "tracked separately"。今天引擎一重启，任何在飞的 DD 都被写成 `dd.failed(lost_on_restart)` 扔回 Goal Agent——已经跑过的 impl commit、CR / FR 的结论全部作废，与 protocol §11 的续跑表直接冲突：

| 最后一条 event | §11 要求 |
|---|---|
| `dd.stage.started` / `agent.spawned` 无对应 finished | 该 run 视为丢失，**重新起同一个步骤** |
| `dd.acceptance` 中途 | **重跑该轮全部验收命令** |
| `*.finished` 边界 | 从下一个步骤继续 |

注意 `events.py` 已经有 `resume_point()`（`events.py:350` 附近）能算出 action / stage，`engine.resume_initial_state` 也已经在用它做 goal 级的续跑——缺的只是 DD 级的对应物。

## 要改什么

1. `src/fleet_graph/minimal/ddgraph.py`：给 `run_dd(...)` 增加 `initial_state: dict[str, Any] | None = None` 参数，语义与 `goalgraph.run_goal` 的同名参数**逐字对齐**（`goalgraph.py:638` / `:673-674` 是现成先例，照抄那个形状）：`None` = 全新一张 DD，走今天的路径，字节级不变；非 None 则 update 进初始 state，并从其中的续跑点进入图，而不是从 `dd_ready` 从头跑。
2. 续跑点怎么进图：在 `ddgraph` 里加一个纯函数 `resume_entry(events_list, dd_id) -> tuple[str, dict]`，从该 DD 的 event 折出 `(入口节点名, state 覆盖)`。规则严格照 §11 的表：
   - 最后是 `dd.stage.started` / `agent.spawned` 且无对应 finished → 入口 = 那个 stage 本身（重起同一步骤）；
   - 最后是 `dd.acceptance` 中途（该轮验收命令没跑完） → 入口 = 验收节点，**整轮重跑**，不做断点续跑；
   - 最后是 `dd.stage.finished` 等边界 → 入口 = 下一个节点；
   - `dd.merged` / `dd.failed` → 该 DD 已终态，返回 sentinel 让调用方直接取结果对象、不重进图。
   - `round` / `approve 是否有效` 从 event 折（`approve 清零` 的既有语义不变：impl 每跑一次清零，GO-14）。
3. `src/fleet_graph/minimal/engine.py`：把 `run_engine` 里 `elif derived.current_dd.dd_id is not None:` 那一段（`engine.py:793-821`）**换掉**——不再写 `dd.failed(lost_on_restart)`，改为：写一条 `agent.failed(stage=..., detail=lost_on_restart)`（这条 §11 明文要求，保留），然后用 `resume_entry` 算出续跑点，经 `_run_dd_seam` 用 `initial_state` 重进 `ddgraph.run_dd`，把结果对象照常交回 goalgraph 的下一个 turn。同时把 docstring 里那段 "Scope note / deliberate deviation / temporary convergence" 删掉，换成实现说明。
4. `docs/specs/minimal/context.md`「已实现」段补一行：§11 的 DD 级逐 stage 续跑已落地。

## 不改什么

- 不改 `events.py` 的 `fold` / `resume_point` / kind 全集——够用，不要顺手扩。
- 不改 goal 级续跑（`resume_initial_state`、`engine.started` vs `engine.resumed` 的分派、`_engine_has_run`）：dd-25 刚修过 `_engine_has_run`，别回退它。
- 不改 `_state_mismatch` 的 git 一致性核对：代码状态以 git 为准、不回放、对不上就 `goal.blocked(state_mismatch)` 不猜——这条保持原样。
- 不引入 LangGraph checkpointer 做续跑。`events.jsonl` 是唯一真相（GO-14 / GO-16），checkpointer 只是可删缓存。这是硬约束。
- 不改 `ddgraph` 全新 DD 的行为：`initial_state=None` 时行为必须与本 DD 之前逐字一致。

## 怎么验收

- `make verify` 全绿。
- 新增测试（用假 agent + 假 git runner，照 `tests/test_minimal_engine_resume.py` 的既有手法）覆盖：① impl 起跑后崩 → 重启后 impl 重跑、DD 不再被判 failed；② CR 跑完、FR 起跑前崩 → 从 FR 继续，CR 结论不丢；③ 验收命令跑到一半崩 → 整轮验收重跑；④ `dd.merged` 之后崩 → 不重进图，直接拿结果对象；⑤ `initial_state=None` 的全新 DD 行为不变（回归）。
