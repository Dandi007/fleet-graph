# R3-fix：research run 身份按 run 实例隔离，消除 agent.run.exited.v2 409 冲突

> development_id = <派单后回填>
> target_base = 13b91947f85a126fdc09afa3bf9c3ff13d5c955e（main HEAD，R4 已暂缓未合）
> spec_digest = <由 dd 冻结>

## 根因（真机探针逐字取证，非推断）

R3 判据 ① 要求「同一题目 W=4 与 W=1 产物等价」，须真机把同一题跑两遍。但 research
run 身份是内容寻址的：`research_id = r-{sha256(question)[:12]}`、
`thread_id = research_id:g{generation}`、worker/synthesis 的 run_id =
`uuid5(thread_id, node, attempt)`。同一题两次跑（同 research_id、同 generation=1）
派生**相同 run_id**，第二遍的 synthesis agent-run 发布 `agent.run.exited.v2` 时撞
bus 409 `IDEMPOTENCY_CONFLICT "Same idempotency_key with different intent"
(retryable=false)` → agent-run exit 91 → 图 terminal=fault。

真机实测（2026-08-31，探针 run r-56200bbfbc60，本轮亲取）：
- W=1（concurrency=1）：terminal=capped（"max_clues 6 触顶 total=7"），只跑了 1 条
  clue（wiki worker 违规展开 3 条 proposed_clues 把板顶到 7>6），synthesis 成功。
- W=4（concurrency=4）：terminal=fault（"synthesis run e20ade17-ea16-5f2c-9ea0-9cc630643aaa
  结束于 failed"），4 条 clue 一波并发派发、3 条 collect ok、1 条 wiki collect
  ok:false；synthesis 撞 409 exit 91（result.json 逐字含 contract_error=exited.v2
  publish failed ... bus returned 409 IDEMPOTENCY_CONFLICT）。
- 判据 ① 等价：不过（W=1 只产 1 条 clue 证据、W=4 产 3 条且 fault）。
- 判据 ② wall-clock：并发派发已可见（4 条 00:51:14 同波、3 条并行 collect），
  但 W=4 fault 使 makespan 对照不干净。
- 判据 ③ kill-restart：本轮探针未覆盖。

## 修复要点

1. **run 身份按 run 实例隔离**：给 research 的 thread 身份注入稳定、非随机的
   run-instance 分量（如 run_root 内容寻址后缀，或 CLI 显式 `--instance`），使同一题
   的两次独立跑（不同 run_root）派生**不同 thread_id/run_id**，不再撞 bus 409；同一
   次 run 的 kill-restart（同 run_root）仍得**相同**身份，re-adopt/幂等不回退（判据
   ③ 语义不破坏）。
2. 真机判据脚本用**可控 worker**（确定性 fake launcher 驱动真实图）或放宽 max_clues，
   使等价性判据可复现，不被真实 worker 的违规展开破坏。
3. 新增随单验收脚本 `scripts/check_research_instance_identity.py`：断言同一题不同
   run_root 派生不同 run_id、同 run_root 派生相同 run_id（幂等不变）。

## 边界（硬线）

- 不碰 agent-runtime：`agent.run.exited.v2` 的 409 容忍是 agent-runtime 座位层缺陷
  （sibling 于 started.v2 的 #480 B），本单只在本图侧做 run 实例隔离，不在图里吞 409
  （findings 硬约束：内容寻址键下吞 409 = 数据分岔静默化）。
- state 只装 id 与计数；converge 纯度不破坏；不新造角色。
- run 身份分量必须**稳定非随机**（kill-restart 不漂移），不得掺 uuid4/时间戳。

## 判据（机器可判）

① 同一题 W=4 与 W=1 在**不同 run_root** 下都能跑到合法终态（无 409 fault）；
② 两 run evidence 集合等价；
③ W=4 makespan < W=1 makespan；
④ kill-restart 同 clue 同 retry 的 run_id 只派一次（不重复派发）。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_fanout.py
uv run python scripts/check_research_instance_identity.py
```