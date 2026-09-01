# H5 收割建 harvest 分支 / cherry-pick 时按路径排除 .dev-dispatch/ 与 .dd-evidence/ 两棵子树（勿全局 gitignore）

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：监督面 10:5x 追加立案（goal.md 顶部 🔴块 H5「同族，10:5x 追加」）。不是判据变更，是缺陷修复。
- 类别：缺陷修复（dd 协议产物泄漏进 main），不改 deny-all、不改 allowlist 配置文件、不改判据。

## 根因（已实读，非推断）

收割 R8 单（PR #205）把工单身份目录一并合进 main：`.dev-dispatch/`（development.json /
spec / gate / merge / reviews / handoffs / run-config）与 `.dd-evidence/acceptance.json`
共 15 个文件。main checkout 的 `development.json` 因此仍 bound 到那张单，本仓派新单
必判 `REPO_BOUND_TO_OTHER_DEVELOPMENT`。同族先例 #133 / #145 / #181——每次 durable-branch
收割复发一次、每次人工清一次，只治标。

代码路径（`src/fleet_graph/supervise/`）：

- `harvest_ops.pr_squash_merge(repo, development_id, head_commit, default_branch)` 直接
  `run_git(repo, "push", "origin", f"{head_commit}:refs/heads/harvest/{development_id}")`，
  再 `gh pr create` + `gh pr merge --squash`——而 `head_commit`（dd 开发链 tip）的树里带
  `.dev-dispatch/` 与 `.dd-evidence/`（dd 协议要求在工单分支提交它们），随之 squash 进 main。
- `harvest_ops.worktree_cherry_pick` 把同一个 `head_commit` cherry-pick 进一次性 worktree，
  同样带入这两棵子树（此处只是 verify 用、会被清理，但「建 harvest 分支」才是泄漏主口径）。

## 交付 A：ops 层机械洗树（机械层，Guard D 豁免）

`src/fleet_graph/supervise/harvest_ops.py`（`HarvestOps` 协议 + `DefaultHarvestOps`）：

新增机械写口，从产品 commit 派生一棵「去 dd 协议子树」的干净产品树并返回干净 tip
commit（建议 `build_harvest_tip(self, repo: Path, head_commit: str, default_branch: str,
worktree_root: Path) -> dict[str, Any]`，返回 `{ok, harvest_tip, detail}` 或等价）。实现要点：

1. 精确按**顶层路径**绑定并排除 `.dev-dispatch/` 与 `.dd-evidence/` 两棵子树（pathspec
   `:(exclude).dev-dispatch` / `:(exclude).dd-evidence`，或等价机械原语：洗树后重提交），
   **绝不**靠全局 `.gitignore`（dd 协议要求工单分支继续提交这些文件，全局忽略会打断协议）。
2. 只剔除这两棵**顶层**子树，不动任何产品文件、不动其它点前缀目录。
3. 模块只做机械事：读 `head_commit`、洗树、产出 tip，不做 allowlist 判定（判定在编排层 gate）。
4. `worktree_cherry_pick` 在 cherry-pick 成功后**在同一 worktree** 上做同样的剔除并提交
   （保证随后的 `run_verify` 也跑在干净产品树；保留 worktree 供 verify 的 rc-702098ab 语义不变）。

## 交付 B：编排层用干净 tip 建分支 / 合并

`src/fleet_graph/supervise/harvest.py`：

1. `worktree` / `cleanup_worktree` 之后、`pr_merge` 之前，用 `build_harvest_tip`（或
   `worktree_cherry_pick` 返回的干净 tip）拿到 `harvest_tip`，写入 `HarvestState`
   新增字段（如 `harvest_tip: str`）。
2. `pr_merge` 节点：`pr_squash_merge` 改推 `harvest_tip`（而非裸 `head_commit`）建
   `refs/heads/harvest/<development_id>`、再 squash merge。
3. Guard D 纪律不变：编排层不新增裸 git/subprocess 调用，全部落在 ops 层；conformance
   （`scripts/check_supervisor_conformance.py`）零回归。

## 交付 C：测试（tests/test_harvest.py；真 git 洗树用例用合成仓，禁触真网/生产 checkout）

1. **阴性测试（关键，不可省略）**：真实 git 合成仓构造一张「工单 commit」——在产品改动
   之外，同时纳入 `.dev-dispatch/development.json`、`.dev-dispatch/spec/approved.md` 与
   `.dd-evidence/acceptance.json`；跑 `build_harvest_tip` / `worktree_cherry_pick`；
   断言产出的 tip / harvest 分支 `git ls-tree -r <tip> --name-only` **不含**任何
   `.dev-dispatch/` 或 `.dd-evidence/` 路径，且产品改动仍保留。
2. **编排层负断言**（fake ops 注入）：断言 `pr_squash_merge` 收到的是干净 tip，而不是裸
   `head_commit`（fake 记录收到的实参）。
3. **正向回归**：不含这两棵子树的普通工单 commit 走完整链 outcome == harvested，既有
   `test_full_sop_runs_all_steps_and_harvests` 语义不变。
4. 既有用例零回归（`make verify` 全绿；`tests/test_harvest_allowlist.py` 不变）。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只改 `src/fleet_graph/supervise/harvest.py` + `harvest_ops.py` + `tests/test_harvest.py`；
  不触 `/data/fleet-graph/supervisor/harvest-allowlist.json`、不改 deny-all、不改判据。
- **绝不全局 gitignore** `.dev-dispatch/` / `.dd-evidence/`——dd 协议要求工单分支提交这些
  文件，全局忽略会打断协议；排除动作只发生在收割侧、按顶层路径精确作用。
- 一切改动走本 development worktree + PR，不直改 main；生产主 checkout 仅读、
  禁 checkout/switch/reset/切分支。
- 本单只交付洗树；`bb026e3` 等历史残骸的清理由人做、不进自动化。