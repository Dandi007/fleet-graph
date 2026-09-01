# M3 收割反应器 pr_squash_merge 前未清理被残留 worktree 占用的 harvest 分支 —— dd-admissible spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：`supervise/harvest*`（M3 收割反应器）。真机触因同 dev-fg-eac9840bdd52：首个真实 fleet-sentinel 单 `dev-fg-2e44f0e61516` 收割 `escaped`。
- 类别：收割链缺陷修复（merge 前本地 harvest 分支/worktree 清理）。**不重复 (1) fetch_dd_ref remote_url / (2) cherry-pick identity 两条（已在 dev-fg-eac9840bdd52）**。
- 真机原始错误（e5-dev-fg-2e44f0e61516.json 回显）：`pr_squash_merge` ok:false `merged`=false detail=`failed to delete local branch harvest/dev-fg-2e44f0e61516: failed to run git: error: cannot delete branch 'harvest/dev-fg-2e44f0e61516' used by worktree at '/data/worktrees/fs-harvest-glm53-20260901'`。该残留 worktree 至今仍在（`git worktree list` 显示 `/data/worktrees/fs-harvest-glm53-20260901` 持有 `harvest/dev-fg-2e44f0e61516` @48ea03e）。

## 根因（实读源码，非推断）

- `harvest_ops.py` `pr_squash_merge`（L593-668）：推本地分支 `harvest/<development_id>`（L621 `git push origin <head>:refs/heads/harvest/<dev>`）→ `gh pr create` → `gh pr merge --squash --delete-branch`（L660）。`--delete-branch` 合并后要删**本地** `harvest/<dev>` 分支；但流程全程无「merge 前清理残留 worktree / `worktree prune` / 强制移除本地 harvest 分支」步骤——harvest 分支被任一残留 worktree 检出时，删本地分支必然 `cannot delete branch ... used by worktree` → `pr_squash_merge` ok:false → 收割 escalated。
- 现场残留 `/data/worktrees/fs-harvest-glm53-20260901` 即该缺陷的产物：合并前无人收回这棵占着 harvest 分支的 worktree。

## 交付 A：merge 前机械回收被残留 worktree 占用的 harvest 分支

1. `pr_squash_merge`（或新增紧邻其前的 cleanup 步）在删本地分支前：`git worktree prune` 清陈旧注册；找出持有 `harvest/<development_id>` 的 worktree 并 `remove_worktree`（复用现有 `remove_worktree`）；再显式 `git branch -D harvest/<development_id>`（或确保 `gh pr merge --delete-branch` 能顺利删本地分支）。
2. 清理失败 → 如实 `ok:false` + 机器可读 detail，绝不 `--force` 硬清生产 checkout、绝不在脏树上继续 merge。
3. 不改 `gh pr create/merge` 语义、不改 allowlist、不改 harvest 14 步管线骨。

## 交付 B：阴性测试（必须能红，合成本地仓，禁触真网）

1. 阴性：合成仓遗留一棵 worktree 检出 `harvest/<development_id>` → `pr_squash_merge`（或新 cleanup 步）先 `prune`+移除该 worktree+删本地分支，随后 merge 成功；未修复时必然 `cannot delete branch ... used by worktree`。
2. 反向不抖动：无残留 worktree 的正常路径行为不变（已合 PR 仍产出 `pr_url` / `merged=True`）。
3. `make verify` 全绿；`test_harvest*` / H 系列 / dev-fg-eac9840bdd52 两条零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据

1. `make verify` 通过。
2. 阴性能红：残留 worktree 占住 harvest 分支时未修复必红 `used by worktree`；修复后转绿，且残留 worktree 被回收、本地 harvest 分支被清。

## 铁律

- 一切改动走 PR（本 development worktree），生产主 checkout 仅 ff-only pull，禁 checkout/switch/reset/detach。
- 只改 `supervise/harvest_ops.py`（+`harvest.py` 编排若需）+ `tests/`；不触 `decide()`、E3、harvest/allowlist 语义、判据。
- 不触 `harvest-allowlist.json`、不自造收割单、不重新收割 dev-fg-2e44f0e61516。