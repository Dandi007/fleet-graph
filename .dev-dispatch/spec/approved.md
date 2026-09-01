# M3 收割反应器 harvest 分支清理返工（主树判别 + rmtree 护栏 + 真实状态）—— dd-admissible rework spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：`supervise/harvest*`（M3 收割反应器）。返工对象 = 上一轮 `dev-fg-c1bf9113fcee`（`state=refused` / `GATE_REJECTED` by `agent:cc-supervisor（用户 2026-08-20 全权授权，full-auto）` / exit=rework / retryable）。
- 监督面 REJECT 原始理由（真机复现）：原实现会把生产主 checkout 连同 git 历史一起删掉仍报成功——事后仓目录/生产文件/`.git` 全 False。gate seq 1757。
- 类别：收割链缺陷修复（返工），不改 allowlist / decide() / 判据。

## 根因（实读 origin/main 5d454f8，非推断）

`supervise/harvest_ops.py` 有三处无护栏 `shutil.rmtree(worktree_root, ignore_errors=True)`：
1. `worktree_cherry_pick` L358-360 前置清树（`if worktree_root.exists(): rmtree`）；
2. `build_harvest_tip` L444-446 前置清树（同款）；
3. `remove_worktree` L473-482：`git worktree remove --force` 失败后兜底 `shutil.rmtree(worktree_root, ignore_errors=True)` + `git worktree prune`，且**恒 `return {"ok": True}`**（从不报真实状态，也不做后置 git 有效性校验）。

关键缺陷：无「主 worktree vs linked worktree」判别——`worktree_root` 若指向主 checkout（或其 common-dir 就是自身），`rmtree` 会删掉整个生产仓 + `.git`。判别式（监督面判据①）：主 worktree **根下 `.git` 是「目录」**；linked worktree 的 `.git` 是「文件」（gitfile 指向 common-dir）。

## 交付 A：主树/linked 判别式 + 主树持有 harvest 分支 → refuse+escalate

1. 机械判别：`(worktree_root/".git")` 为 `is_dir()`（主 worktree）或 `is_file()`（linked gitfile）；或读 `git rev-parse --git-common-dir` 是否等于 `worktree_root/.git`（等于 = 主树）。linked = 可回收；主 worktree = **绝不清理**。
2. 目标为主 worktree（`.git` 是目录）→ `refuse` + 机器可读 detail（`primary checkout is not reclaimable`）→ 收割 `escalated`，绝不对其 `rmtree` / `worktree remove` / `prune` / 切分支。
3. 主树当前持有 `harvest/<development_id>` 分支 → 同样 `refuse+escalate`（绝不在主 checkout 上删分支）。

## 交付 B：rmtree 兜底加护栏 + 返回真实状态

1. `remove_worktree` 的 `shutil.rmtree` 兜底前置护栏：仅当目标经判别确认为 linked worktree（`.git` 为 gitfile 且 common-dir 不等自身、不落在主 checkout 路径内）才允许 rmtree；否则拒绝删除 + 返回 `{"ok": False, "detail": ...}`。
2. `worktree_cherry_pick` / `build_harvest_tip` 的 L358/L444 前置 `rmtree` 一律过同一护栏函数（存在性 + 非主树 + 在允许 worktree 根下），不达标即不删、如实报。
3. `remove_worktree` **不再恒返 ok:true**：真实反映 `worktree remove` / `prune` 结果，失败返回 `ok:false` + detail。

## 交付 C：后置校验（先断言仓仍有效 git，杜绝「目标不存在=通过」）

1. 任何回收/清理动作后：`git -C <repo> rev-parse --is-inside-work-tree`（=true）与 `--show-toplevel`（=仓目录）、`.git` 仍在、`HEAD` 可解析；任一不满足 → `ok:false` + 机器可读 detail。
2. 绝不把「目标目录已不存在」当成功（那正是被删主树的假绿）。

## 交付 D：阴性测试（必须能红；主树夹具）

1. **阴性（本单判据，不可省）**：合成 fixture「主 worktree 检出 `harvest/<id>`（`.git` 为目录）」→ 回收/清理步 `ok:false` 且仓完好（生产 checkout 文件与 `.git` 一字未动、`is-inside-work-tree=true`）。未修复时（无判别、rmtree 无护栏）主树被清空 = 失败。
2. 反向不抖动：合法 linked worktree 持有 `harvest/<id>` → 正常 remove+prune+删分支、`ok:true`，主树不受影响。
3. 既有 6 个 linked-worktree 回收用例 + H 系列零回归；`make verify` 全绿。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据

1. `make verify` 通过。
2. 主树阴性能红：未修复必红（主树被清=失败），修复后 `ok:false` 且仓完好、`.git` 一字未动。
3. 不恒返 ok:true；触发后置断言的清理失败如实报 false。

## 铁律

- 一切改动走 PR（本 development worktree），生产主 checkout 仅 ff-only pull，禁 checkout/switch/reset/detach/建分支/验证。
- 只改 `supervise/harvest_ops.py`（+`supervise/harvest.py` 编排若需）+ `tests/test_harvest*.py`；不触 `decide()`、E3、harvest/allowlist 语义、判据。
- 无论失败姿态，绝不 rmtree 生产主 checkout 或 `.git`；不触 `harvest-allowlist.json`、不代造收割单、不重新收割 dev-fg-2e44f0e61516；不混入 dev-fg-c028a8839e5a（verify 按仓解析）与 dev-fg-eac9840bdd52（已收割入 #220）之外的无关改动。