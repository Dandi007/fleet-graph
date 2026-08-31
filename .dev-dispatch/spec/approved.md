# 修复 PR #195 引入的 make verify 回归（debate 子图迁移后测试/脚本残留引用）

## 目标

PR #195（dev-fg-97b5dbae70e2，R4 对抗·裁决子图收割）把 `src/fleet_graph/graphs/research_pipeline.py` 的合成节点换成 debate 子图（advocate/opponent/judge/arbiter），删除了 `synthesis_run_id` 与 `SYNTHESIS_ROLE`。但以下测试/脚本仍引用被删符号，导致 `make verify` 在 pytest collection 阶段 ImportError 失败：

- `tests/test_research.py`：L43 导入 `synthesis_run_id`、L281/L289 使用；
- `scripts/check_research_instance_identity.py`：L40 使用 `SYNTHESIS_ROLE`、L44 导入 `synthesis_run_id`、L147/L152 使用。

`make verify` 触及范围内任何其它对被删符号的残留引用一并迁移（由实现者读码确认，不列举穷尽）。

## 判据（唯一验收标准）

1. `make verify` 全绿（`lint` + `test` + `conformance` 三目标全部通过）。
2. 不改 `src/fleet_graph/graphs/research_pipeline.py` 的图形状与 R4 debate 语义：不重新引入 synthesis 节点/角色，不动 advocate/opponent/judge/arbiter 子图及其 run id 派生规则。
3. instance-identity 断言以现 API 等价表达：用 `debate_run_id` / `worker_run_id` / `derive_run_id` 与 `ADVOCATE_ROLE` / `OPPONENT_ROLE` / `JUDGE_ROLE` / `ARBITER_ROLE`（`DEBATE_ROLES`）替换被删的 `synthesis_run_id` / `SYNTHESIS_ROLE`；同线程恒等、跨线程互异的观察意图保持不变、不弱化。
4. 具体迁移取舍（例如原“合成单节点身份”断言改用哪个 debate role 的 `debate_run_id` 作等价表达）由实现者读码定夺，不得改变被迁移断言的观察语义，不借机重构或扩大改动面。

## 铁律

- 代码编写与 review 一律由 dev-dispatch 完成，独立 `/data/worktrees/` worktree；
- 生产主 checkout 严禁 checkout/switch/reset/detach；
- 只迁移对被删符号的残留引用与相应断言，最小改动。

## Executable Acceptance

```dd-acceptance
make verify
```