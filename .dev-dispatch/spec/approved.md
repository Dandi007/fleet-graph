# M3 收割反应器 pr_squash_merge 分支占用 refuse+escalate 修复——delete-branch 面对残留 worktree 占用时不自相矛盾

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：`supervise/harvest_ops.py::pr_squash_merge`（M3 收割反应器写步）。真机触因：首个真实 fleet-sentinel 单 `dev-fg-2e44f0e61516` 收割时 `pr_squash_merge` 报 `failed to delete local branch harvest/dev-fg-2e44f0e61516: cannot delete branch ... used by worktree at '/data/worktrees/fs-harvest-glm53-20260901'`，且该残留 worktree 至今仍在（占用 `harvest/dev-fg-2e44f0e61516`）。
- 类别：收割链缺陷修复（merge 与本地分支删除的原子性/占用前置不一致）。**不做**「merge 前回收清树」路线（那是另一张单 dev-fg-d775ef9fff26 的方向）；本单做「占用前置检测 → refuse+escalate」路线，杜绝自相矛盾。

## 根因（已实读 release 14938bf 源，非推断）

`pr_squash_merge`（`harvest_ops.py` L875-942）用 `gh pr merge <number> --squash --delete-branch`（L942）一步完成「远端 squash 合并 + 删远端分支 + 删本地分支」。当本地 `harvest/<development_id>` 分支被任一残留 worktree 检出时：

1. 远端 squash 合并**已成功**（GitHub PR MERGED）；
2. `--delete-branch` 的**本地**删分支失败（`cannot delete branch ... used by worktree`）→ gh 返回非零；
3. `pr_squash_merge` 据非零返回 `merged=False` → 编排层 escalate —— 但**远端其实已 merged**，形成「合并已发生却按未合并 escalate」的自相矛盾，且无任何机器可读的占用诊断（把 git 原始错误原样吞进 detail）。

当前 main 的 `pr_squash_merge` 无「分支是否被 worktree 占用」的前置检测，也无把「占用」显式升级为 refuse+escalate 的路径（L942 仍 `--delete-branch`）。#226（闸18 branch-cleanup 返工）只加固了 `remove_worktree`/rmtree 护栏、未触 `pr_squash_merge`。

## 交付 A：分支占用前置检测（只读） + refuse+escalate

1. `pr_squash_merge` 在 `gh pr merge` **之前**做只读占用检测：`git worktree list --porcelain`（或等价只读口）判断是否有任一 worktree 检出 `harvest/<development_id>` 本地分支。
2. 占用命中 → 返回机器可读 `{"merged": False, "refused": True, "escalate": "HARVEST_BRANCH_OCCUPIED", "detail": "...占用者 <worktree_path>..."}`，**绝不执行 gh pr merge / gh pr create**，绝不落「远端已合并却报未合并」的半态。编排层据此 `outcome=escalated`（走既有 escalate 收尾，不触碰任何写步）。
3. 未占用 → 走既有 `gh pr merge --squash --delete-branch` 原路径，行为零回归。
4. 禁止用 `git branch -D`/`worktree remove --force` 硬删残留 worktree 的占用来「顺手清掉」——占用只 report，绝不替人删（残留回收是退管/回收链的职责，不混进 pr_merge）。

## 交付 B：阴性/正向测试（`tests/test_harvest.py` 扩展，合成本地仓，禁触真网）

1. **阴性（本单判据，必须能红）**：合成本地仓，残留一棵 worktree 检出 `harvest/<id>` → `pr_squash_merge` 返回 `refused=true/escalate=HARVEST_BRANCH_OCCUPIED`、`merged=false`，且 `gh` 未被调用（fake/命令计数 0）——未修复时必然走进 `gh pr merge --delete-branch` 报原始 `used by worktree` 或 `merged=false` 而无 `refused/escalate` 码。
2. **反向不抖动**：无占用 → 走原路径、`merged=true`、产出 `pr_url`。
3. `make verify` 全绿；`test_harvest*` / dev-fg-eac9840bdd52 / dev-fg-c028a8839e5a / 闸18 返工 等既有用例零语义回归；H 系列零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据

1. `make verify` 通过。
2. 占用时未修复必红（`merged=false` 且无 `escalate=HARVEST_BRANCH_OCCUPIED`）；修复后 `refused=true` + `escalate=HARVEST_BRANCH_OCCUPIED` + `gh` 零调用、绝不落半态。

## 铁律

- 一切改动走 PR（本 development worktree），生产主 checkout 仅 ff-only pull，禁 checkout/switch/reset/detach/建分支/验证。
- 只改 `src/fleet_graph/supervise/harvest_ops.py`（+`harvest.py` 编排若需）+ `tests/test_harvest*.py`；不触 `decide()`、E3、harvest/allowlist 语义、判据、SOP 步枚举。
- 不触 `harvest-allowlist.json`、不代造收割单/真实单、不重新收割 `dev-fg-2e44f0e61516`；占用只 report 绝不替人删残留 worktree/分支。