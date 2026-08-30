# 名册测试断言同步修复——wf-216dc3 入编补测 spec

- 目标仓：`/data/code/self/fleet-graph`（Dandi007/fleet-graph；本 development 在 `/data/worktrees/` 下独立 worktree）。
- 类别：环境修复（改配置没同步改测试），非判据变更。goal.md M1 前三条验收断言一字不改。
- 归属：M1 传感层推进的前置清障——不修此条，M1 第一条验收 `make verify` 会因 pre-existing 两红被拦。

## 根因（已真机复现，非推断）

PR #158（commit `095292201f48`，squash 合入 main）把 `wf-216dc3`（监督面图化自举线，alias `ronin-sup-graph`）以 `enabled: true` 加进 `config/ronin-lines.json`，但 `tests/test_ronin_lines_config.py` 两条守卫断言未同步：

1. `test_the_real_loader_accepts_it`：断言 `{line.folder_id} == MIGRATED | OPENED | ENROLLED | REVIVED`；实际左集多出 `wf-216dc3`。
2. `test_exactly_the_current_batch_is_switched_on`：断言 `enabled == (BATCH_TWO | OPENED | ENROLLED | REVIVED) - CONVERGED`；实际 `enabled` 多出 `wf-216dc3`。

## 交付（代码与评审全委 dev-dispatch；worker 不写业务代码）

唯一改动（最小、确定性）：

1. 在 `tests/test_ronin_lines_config.py` 的 `ENROLLED` 集合中加入 `"wf-216dc3"`，并附一条与同文件既有批次注释同风格的 provenance 注（第六波 2026-08-30：监督面图化自举线，新 goal 线入编，seat `opencode-dsv4pro`，alias `ronin-sup-graph`）。
   - 语义依据：`ENROLLED` = 新线经监督面入编（非 MIGRATED 的 babysitter 存量迁移、非 OPENED 的 08-27 新开批、非 REVIVED 的复活线）。wf-216dc3 恰好落入此桶。

## 铁律 / Non-goals

- 不改 `config/ronin-lines.json`（配置正确，wf-216dc3 本就该 `enabled:true`）。
- 不改 `goal.md` 任何验收断言（判据只有用户能改）。
- 不改 `tests/test_ronin_lines_config.py` 其余任何断言，不改任何其他文件。
- 一切改动走 PR 进 fleet-graph（本 development worktree），不直改 main；生产主 checkout 仅 ff-only pull。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据

1. `make verify` 通过——相对基线 0952922 无新增失败（基线仅 test_ronin_lines_config.py 两红，修复后清零）。
2. `git diff` 仅限 `tests/test_ronin_lines_config.py`；`config/ronin-lines.json` 未被改动。
3. goal.md M1 前三条验收断言原样保留（判据不变）。