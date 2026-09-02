# H8 case 2：`_binding_matches` 的 linked-worktree→canonical 归属判定把护栏变成全局收割锁

## Goal

收割反应器 `detect_inflight_binding` 依赖的 `HarvestOps._binding_matches` 含一条 case 2：
record 的 `repo_path` 是 linked worktree 时，用 `git rev-parse --git-common-dir` 解析出
其 canonical，判 canonical 是否等于被探测的 `tree`。这条「worktree → canonical」归属
判定让收割命中 canonical 树时，把**同仓任何一张在飞 linked worktree** 都算作「绑定本
树」，于是只要 fleet 某仓还有一张在飞单，该仓 canonical 就永远收割不了——护栏退化成
全局收割锁，且逼出「等在飞单跑完再收」的错误规避。本单据机械事实收敛 case 2。

## 机械答案：收割对 canonical 仓每个动作动了什么（已实读 `harvest_ops.py`/`harvest.py`）

| 动作（SOP 步） | 实际 git/命令 | 触及的树 | 是否触及其它 linked worktree 的 HEAD 或工作区 |
|---|---|---|---|
| `fetch_dd_ref`（fetch） | `git fetch <remote_url> <dd_ref>`（或本地 ref 解析） | FETCH_HEAD / 目标 ref | 否：fetch 只更新远端跟踪/目标 ref，不 checkout、不动任何 worktree |
| 建 harvest 分支（pr_squash_merge） | `git push origin <tip>:refs/heads/harvest/<id>` | **远端** refs/heads/harvest/<id> | 否：纯远端 push，本地其它 worktree 无感 |
| `ff_only_pull`（pull） | `git pull --ff-only`（canonical） | canonical 检出：移动其 branch/HEAD + canonical 工作区文件 | 否：链接 worktree 各持自己 HEAD；git 禁止两 worktree 检出同一分支，故其它 linked worktree 必在别的 ref，主树 ff 不影响它们 |
| `push`（pr_squash_merge） | `git push origin …` | 远端 | 否 |
| `PR merge`（pr_squash_merge） | `gh pr merge --squash --delete-branch` | 远端 forge（含删**远端** harvest 分支） | 否：删远端分支不触本地任何 worktree |
| `deploy`（deploy） | 运行 allowlist 命令，cwd=canonical | canonical 工作区（部署产物副作用） | 否 |
| `verify_real`（verify_real） | 运行 argv，cwd=canonical | canonical（读/跑套件） | 否 |
| `worktree_cherry_pick`（worktree） | `_preclean_worktree` + `git worktree add --detach <temp> <default_branch>` + cherry-pick + 洗树提交 | **仅收割自建临时树** `<state_root>/threads/<key>/worktree`（+登记它） | 否：`worktree add` 新建一棵临时树，不 checkout 已有 linked worktree 的分支 |
| `remove_worktree` / `reclaim`（cleanup_worktree） | `git worktree remove --force <temp>` + 兜底 `rmtree(<temp>)` + `git worktree prune` | **仅那棵临时树**；prune 只清「目录已不存在」的**死注册** | 否：prune 不删活 worktree 目录、不动活树 HEAD/工作区 |
| `_branch_occupied_by_worktree` | `git worktree list --porcelain`（只读） | 只读 | 否 |

结论：收割的写/删动作只落在 **(a) canonical 主 checkout**（ff_only_pull/deploy/verify_real）
与 **(b) 收割自建的临时 worktree**（worktree add/remove/prune）。**没有任何一个动作会
触及另一张在飞单的 linked worktree 的 HEAD 或工作区**。因此 case 2 把「同仓另一棵 linked
worktree」判成「绑定 canonical」是假绑定，必须删除；保留它只会错误锁死整仓收割。

## 决策

**删除 `_binding_matches` 的 case 2**（linked-worktree→canonical 归属判定），只保留
case 1（`tree == record_tree` 直接路径相等）。`detect_inflight_binding` 及
`_detect_occupied_tree` 不再把「同仓别的 linked worktree 在飞」判为占用。

- case 1 保留语义（不可弱）：record 的 `repo_path` 规范化后**直接等于**被探测的
  `tree`（canonical 路径 / record_worktree 路径 / 本次临时 worktree 路径）→ 才构成绑定。
- `_binding_matches` 若只剩 case 1，可整体收敛为等价 `tree == record_tree`（纯路径比较，
  `git rev-parse --git-common-dir` 读口不再需要）；文档同步更新，删掉「两种情况」描述。

## Bidirectional Acceptance Criteria（双向不可弱）

### 阳性判据（不可弱，护栏仍在）

主树 canonical 被另一张在飞单**直接**绑定（record.repo_path == canonical 路径，或 ==
本次要消费的那棵树的路径）→ `detect_inflight_binding(tree, …)` 返回 `in_flight=True`、
`bound_development_id=<该单 id>`、`repo_path` 非空、`detail` 同时含单 id 与树路径；
编排层 `run_harvest` outcome=escalated、`escalate==HARVEST_TREE_OCCUPIED_BY_INFLIGHT`、
写步骤一个没跑、目标树一字未动。

### 阴性判据（不可弱，解锁全局锁）

同仓另一棵 linked worktree 在飞（record.repo_path = canonical 的**另一棵** linked
worktree，与本次要消费的树路径不同），而收割只触 canonical 主 checkout + 自建临时树
→ `detect_inflight_binding(canonical, …)` 与 `detect_inflight_binding(本次树, …)` 都
返回 `in_flight=False`，`run_harvest` **照常收割**（harvested），不被那张在飞单锁死。
（显式禁止「等在飞单跑完再收」：并发不冲突的树必须可收。）

## Minimal Implementation Scope

1. 只改 `src/fleet_graph/supervise/harvest_ops.py`（`_binding_matches` 删 case 2，等价
   收敛为直接路径相等；`detect_inflight_binding` docstring 同步删掉 case 2 描述）与
   `tests/test_harvest.py`。
2. 测试（真实 git 合成仓：canonical + 两棵 linked worktree）：
   - 阴性（修复前必红）：dev-fg-OTHER 的 record.repo_path = canonical 的**另一棵** linked
     worktree（在飞），收割树 = 另一棵路径 → 修复前 `detect_inflight_binding(canonical)`
     恒 in_flight=True（case 2 误锁）；修复后 in_flight=False 且 `run_harvest` harvested。
   - 阳性（不可弱）：dev-fg-OTHER 的 record.repo_path == 本次要消费的树路径（直接相等，
     在飞）→ in_flight=True + refuse+escalate + detail 含单 id 与树路径，目标树一字未动。
   - 自身在飞仍不阻断（rc-3d12fbbe）、H-A/H-B/H-C 既有回归不回归。
3. 不碰 `harvest-allowlist.json`、不改 E5/E6/E7 词表、不改 H7/H8/M3 分支占用语义、不造
   真实收割单、不重派已 complete 的单。

## 可复现验收

```dd-acceptance
uv sync --frozen
make verify
```

Acceptance 完成仅当 `make verify` 全绿且上述正/负双向判据都在、既有 harvest 测试零回归。
本开发不部署、不重启、不触碰生产 checkout。

## 铁律

- 只改 `src/fleet_graph/supervise/harvest_ops.py` 与 `tests/test_harvest.py`。
- 一切改动走 PR（本 development worktree），生产主 checkout 仅 ff-only pull，禁
  checkout/switch/reset/detach/建分支/验证。
- 不触 `harvest-allowlist.json`、不自造真实收割单、不重派已 complete 的单
  （dev-fg-3369ceda52d5 / dev-fg-29ba21ec70cf / dev-fg-49bcbc00b4df）。