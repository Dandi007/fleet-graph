# dd-replay: gate refuse→resume 重放不得重物化已封存的 review attempt

## 事实（真机复现，dev-fg-31b963659d16，2026-08-28）

该单 g1 完整走过：configure → implement（1 次 PROVIDER_UNAVAILABLE 后 success）→
continuous_review APPROVE → final_review REJECT → implement rework →
continuous_review APPROVE → final_review APPROVE → acceptance success →
human_gate。gate 首个 board decision 的 decision 字段为散文，按设计被
`GATE_VERDICT_UNRECOGNIZED` refuse（fail-closed 正确）。补发规范 verdict
（APPROVE）后经 `development_gate(resume=True)` 恢复，重放却在
continuous_review 的物化处 fault：

```
materialize failed on continuous_review: PLUGIN_CONTRACT_MISMATCH:
materialize-handoff.sh returned non-JSON output (exit 1):
[attempt-context] ERROR ORDER_VIOLATION: entry 4
(rc-20096c0b-5929-5d20-8dc8-604b0e7e7aef): a new attempt requires a prior REJECT
```

即 resume 沿 checkpoint 重放到已完成的 continuous_review 节点时，试图**新开
一个 review attempt**去物化，而 attempt-context 守卫正确地拒绝了「无 REJECT
前置的新 attempt」。同族先例：main@4072a4f（#119）已为 final_review 的
RECEIPT_CONFLICT 修过重放路径（重放 receipt 物化 materialization intent）。
本缺陷是同一问题在 continuous_review（以及任何 attempt-context 阶段）的残留。

## 要求

1. gate 阶段 refuse 后的 `development_gate(resume=True)`（以及任何等价的
   同 generation resume 路径）重放经过**已封存**的 attempt-context 阶段
   （continuous_review / final_review / implement 等）时，必须识别该阶段已有
   终态 receipt 并做**幂等物化**（复用 #119 的 materialization-intent 机制或
   等价手段），不得新开 attempt；恢复应推进到真正待决的阶段
   （human_gate/merger）。
2. **不得削弱** attempt-context 的 ORDER_VIOLATION 守卫本身——对真正的新
   attempt（有 REJECT 前置）它是对的；只修重放侧的调用方。
3. 回归测试：构造「全链走到 human_gate → gate refused（unrecognized verdict）
   → board 补规范 APPROVE → resume」的场景，断言 resume 不新开任何 review
   attempt、不触发 ORDER_VIOLATION，最终推进到 merger 并 complete。同时保留
   一条负例断言：真正的新 attempt 无 REJECT 前置时守卫仍拒绝。
4. 既有测试（tests/test_dd_replay*、tests/test_dd_auto_resume.py 等）不回退。

## Non-goals

- 不改 gate verdict 解析（宽进严出属 E4b，另单处理）。
- 不动 agent-bus。
- 不在本单内恢复 dev-fg-31b963659d16——修复合入部署后由监督面另行 resume 验证。

## 边界

一切实现与 review 由 dev-dispatch actor 完成；只在本 worktree 及其 dd 子
worktree 内操作；不触碰生产 main checkout 与任何生产服务。

```dd-acceptance
uv sync --frozen
make verify
```
