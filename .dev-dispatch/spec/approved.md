# R3：dispatch 串行（W=1）→ 并发 fan-out（LangGraph Send API）

> 前置：R1（clue/evidence/doc 落 bus append-only，#169）与 R2（多源 worker 矩阵，#171，
> 契约修正 #174）已收割上线。本单只做并发化，不新增角色、不改 bus 协议、不改 converge 语义。

## 根因 / 现状

`fleet_graph/graphs/research_pipeline.py` 的 `dispatch → collect → harvest → converge` 是
W=1 串行闭环：`dispatch` 每轮只取 `open_clues[0]`（写 `pending_clue_id`）、`collect` 对单
clue launch+wait、`harvest` 处理完该 clue 才回 converge。worker 是 agent-runtime 的
detached 进程、`wait()` 是轮询——N 个 worker 的延迟被线性叠加进 wall-clock。

## 目标（宪法条1 / 规模）

把 dispatch 从串行改为 **LangGraph fan-out（Send API）**，并发度可配、缺省 4，同时保
checkpoint 可重放、幂等不重复派发、且不破坏 `converge()` 纯度与「State 只装 id 与计数」约定。

## 设计要求

1. 可配并发：`ResearchBounds` 增 `concurrency: int = 4`；`ResearchConfig` 增 `concurrency`
   （缺省 4）；`cli research run` 增 `--concurrency`（default 4）。
2. fan-out 结构：`dispatch` 把本 wave 至多 `concurrency` 个 open clue 标 `dispatched`，
   返回 `list[Send("collect", {"clue_id": id})]`；`collect` 改为单 clue 粒度（由 Send
   命令携带 clue_id，取代全局 `pending_clue_id`）。dispatch 先 `launch` 全部再 `wait`
   全部（真实时间重叠），wall-clock 因并发降、而非仅结构并列。
3. State 仍只装 id 与计数：clue board 逐项仍 `{id,status,depth,retry,source}`；本轮
   dispatched id 集合仍只存 id；findings/report 正文仍只落 run root 文件，绝不进 state。
4. `converge()` 纯度不动：仍是纯计数多出口（触顶 / 线索耗尽 / 零增长 / 轮次预算），
   不读时钟、不 IO、不因并发度改语义；capped 绝不报 converged。
5. 可重放与幂等：worker run id 仍 `derive_run_id(thread, "worker/{clue_id}", retry+1)`
   内容寻址派生、与并发度无关；kill-restart 后同 id 重派 = re-adopt 在途 run（launcher
   幂等），绝不二次派发。concurrency 只影响「同 wave 派几个」，不影响 clue id / input /
   run id 的派生。
6. 产物等价：clue id 按 `text|source` 内容寻址去重、evidence 逐 clue 落盘，故同一题目
   W=1 与 W=4 的证据集合（各 done clue 的 evidences 并集）逐字相等；children
   （proposed_clues）派生确定、仅发现顺序可异。等价测试用确定性 fake seed 产出有界小线索
   树（< max_clues）并用确定性 fake launcher 逐 clue 回放，断言两边证据集合逐字相等。

## 判据（机器可判）

① 同一题目 W=4 与 W=1 产物等价（证据集合相同）；
② wall-clock 显著下降（W=4 makespan < W=1 makespan）；
③ kill-restart 不重复派发（同 clue 同 retry 的 run_id 只派一次）。

## 验收

`scripts/check_research_fanout.py`（本单新建）用确定性 fake seed/launcher 跑 W=1 与 W=4
各一次 + 一次 kill-restart 续跑，打印三行可解析输出，exit 0 当且仅当三条判据全过：

```text
equivalence=ok
wallclock=ok
no_dup_dispatch=ok
```

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_fanout.py
```

## 硬边界

- 不新增/改名/重注册 agent-runtime role；不新增 bus kind（R1 已定）。
- 不碰 `converge()` / `research_bus` 协议生成器 / agent-run launcher 的幂等语义（只消费其保证）。
- 不 hand-roll 并发原语绕过 LangGraph checkpoint；并发度不得使产物随 W 变化。