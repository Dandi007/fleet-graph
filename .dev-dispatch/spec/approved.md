# H6 冲突重试路径缺陷：cherry-pick 首败后未清场即 -X theirs 重试，恒报 unmerged files

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：监督面 M3 e2e 第三轮/第四轮收口点立案。是缺陷修复，不是判据变更。
- 类别：缺陷修复（冲突重试协议缺陷），不改 deny-all、不改 allowlist 配置文件、不改判据、不动 SOP_STEPS 枚举。

## 根因（已实读，非推断）

M3 e2e 第三轮真实受管单 dev-fg-4d0e7047d1ca 的 E5 回执 `reports/e5-dev-fg-4d0e7047d1ca.json`
里 `worktree_cherry_pick ok:false conflicts=true`，detail=`Cherry-picking is not possible
because you have unmerged files`；`allowlist_auth.granted=true`（canonical 仓解析 #203 已生效），
但 `worktree_cherry_pick` 失败 → `run_verify` 缺树 → `pr_squash_merge` PR#2 判 CONFLICTING →
`pr_merged=false`，终局 escalated（H2 终局语义正确拦截，未吞成 harvested）。

代码 `src/fleet_graph/supervise/harvest_ops.py::worktree_cherry_pick`（release b8a45c07，L289-361）：

1. L323 首次 `git cherry-pick <head_commit>` 因冲突返回非零，把 worktree 索引留在
   MERGING/conflicted 态（残留 unmerged files）。
2. L344 直接 `git cherry-pick -X theirs <head_commit>` 重试，但**没有先
   `git cherry-pick --abort` 或 `git reset --merge` 清场**。
3. git 拒绝在已有 unmerged files 的树上开启新 cherry-pick → 重试恒报
   `Cherry-picking is not possible because you have unmerged files`，与「首败是否真冲突可解」无关。

即：这是**冲突重试路径的协议缺陷**，不是真正不可解的产品冲突。首败残留的未合并状态把重试路径
永久锁死，`-X theirs` 从未被真正执行过。

## 交付 1：重试前清场（abort / reset --merge），成功收口并洗 dd 子树

`src/fleet_graph/supervise/harvest_ops.py::worktree_cherry_pick`：

1. 首次 `cherry-pick` 失败且判为冲突（现有 L335-343 分支）后，在 `-X theirs` 重试之前，先
   执行 `git cherry-pick --abort`；若 abort 返回非零则兜底 `git reset --merge`（或等价清场原语）
   把索引从 MERGING 态恢复干净。清场失败则如实返回 `ok:false` + 机器可读 detail，绝不带病继续。
2. 清场成功后 `-X theirs` 重试：成功则照旧 `_strip_dd_subtrees` 洗 dd 协议子树
   （`.dev-dispatch` / `.dd-evidence`）产出干净 `harvest_tip` 并返回 `ok:true`。
3. 重试仍失败：返回 `ok:false conflicts:true` + `detail`（机器可读 escalate），
   **绝不强行覆盖、绝不 `git reset --hard`、绝不自动 reset 生产主 checkout**。
4. 失败/冲突路径照旧就地 `remove_worktree` 清理一次性 worktree，不留给编排层。

## 交付 2：阴性测试（必须，不可省略）

`tests/test_harvest.py`（真实 git 本地合成 fixture，禁触真网/生产 checkout）：

1. 构造 cherry-pick 冲突 fixture（沿用 `test_conflict_path_still_reports_conflicts` 双分支同文件
   改法）：断言重试路径**能收口**（`-X theirs` 以默认分支为主解得开 → `ok:true` 且 `harvest_tip`
   非空、成功路径 worktree 保留供 run_verify）或**产出 escalate**（`ok:false` / `conflicts:true`），
   二者必居其一。
2. 关键负例：断言**不得自报 harvested**——`worktree_cherry_pick` 要么诚实返回 ok:false
   （conflicts/escalate），要么返回 ok:true 且其 `harvest_tip` 为真实可解析 commit；绝不出现
   「失败却报 ok:true」或「凭空 harvest_tip」。终局语义对齐既有 H2 判据：任一 step ok:false →
   postconditions escalated，不吞成 harvested。
3. 回归：不破坏既有 `test_conflict_path_still_reports_conflicts`、正向
   `test_worktree_cherry_pick_returns_clean_tip`、
   `test_worktree_survives_cherry_pick_through_verify_then_is_removed`。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只改 `src/fleet_graph/supervise/harvest_ops.py::worktree_cherry_pick` + `tests/test_harvest.py`；
  不触 allowlist 配置文件、不改 deny-all、不改判据、不动 SOP_STEPS 枚举。
- 禁止对生产主 checkout 做 checkout/switch/reset/detach；禁止 `git reset --hard` 任何非一次性 worktree；
  沙箱本地残骸清理仍由监督面人工执行，绝不写进自动化。
- 一切改动走本 development worktree + PR，不直改 main；生产主 checkout 仅读。