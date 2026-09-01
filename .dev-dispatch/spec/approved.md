# H7：收割链在写前步骤判红时禁止执行任何写动作；fallback PR 必须洗树且 verify 绿

## 背景（生产实录，不是推测）

2026-09-01 M3 收割 e2e 第四轮，回执 `/data/fleet-graph/supervisor/reports/e5-dev-fg-88999a951ac9.json`：

| 步骤 | 结果 |
|---|---|
| `intake` / `gate` / `fetch_dd_ref` / `cherry_check` | ok:true |
| `worktree_cherry_pick` | **ok:false** —— `Cherry-picking is not possible because you have unmerged files` |
| `run_verify` | **ok:false** —— exit 127 |
| `cleanup_worktree` | ok:true |
| `pr_squash_merge` | **ok:true —— PR #4 真的合进了 master（3062c6f）** |
| `ff_only_pull` | ok:true |
| `outcome` | `escalated` |

两个后果，都已真机核实：

1. **在验证失败之后执行了不可逆写入**。`worktree_cherry_pick` 与 `run_verify` 都判红，链没有停，
   继续走到 `pr_squash_merge` 并真的合了。`outcome=escalated`（H2 语义）只是把账记诚实，
   **写已经发生，且不可回退**。
2. **协议目录整棵重新入树**。监督面 18:0x 用 PR #3（only-delete strip）把
   `fleet-harvest-sandbox` 的 master 清到 `1517e6e`（只剩 Makefile / README.md /
   scripts/deploy.sh 三个产品文件）；18:57 这条 fallback 路径把 durable 分支整支合了回去，
   master `3062c6f` 的树里又出现 `.dev-dispatch/` 10 个 + `.dd-evidence/` 1 个文件。
   H5 的 `_strip_dd_subtrees` **只在 cherry-pick 成功路径上跑**，兜不住这条。

这就是本卷记了三次的 #133 / #145 / #181「每次 durable-branch 收割复发一次、每次人工清一次」
家族的根：**不是有人手滑，是失败路径把 durable 分支直接并进了默认分支**。
fleet-supervisor SKILL §7 反模式第一条写的就是这件事。

H6（PR #213 已合入 main `1cdae40`）修的是「冲突重试前清场」，让 cherry-pick 有机会成功；
**H7 修的是另一件事——cherry-pick 或 verify 失败时，链绝不许再写。** 两者不可互相替代。

## 目标

把「写前步骤判红 → 仍执行写动作」这条路彻底封死，并让任何保留下来的 PR 路径不再把协议目录带进默认分支。

## 交付物

### D1 —— 写前闸：判红即停，绝不进入写步骤

- 在 `src/fleet_graph/supervise/harvest.py` 的收割链里明确区分**只读/准备步骤**与**写步骤**：
  - 写步骤至少包括 `pr_squash_merge`、`ff_only_pull`、`deploy`，以及任何 push / merge / 部署类动作；
  - 只要 `worktree_cherry_pick` 或 `run_verify` 任一 `ok:false`，**立即停止链、直接进入 escalate 收尾**，
    不得执行上述任何写步骤。
- 停止时回执必须机器可读地说明：在哪一步停的、为什么停、**明确记录「未执行任何写动作」**
  （例如 `writes_skipped: [...]`），让观测面能一眼分清「拒绝写」与「写了但失败」。
- `outcome` 维持 `escalated`（H2 语义不变，不得改成新终态）。

### D2 —— 若保留 fallback PR 路径，它必须洗树且 verify 绿

- 如果实现上仍保留「cherry-pick 失败后改走 PR」这条 fallback，则该路径**必须**：
  1. 先经 `_strip_dd_subtrees`（**复用 H5 的既有函数，不许另写一套**）把
     `.dev-dispatch/` 与 `.dd-evidence/` 两棵子树剔除；
  2. 有**绿的** `run_verify` 才允许 merge——verify 未跑或非零一律不许 merge。
- 若实现判断这条 fallback 本就不该存在，**直接删掉它也是合格交付**，
  但必须在测试里钉死「cherry-pick 失败 → 无任何 PR 被 merge」。

## 阴性测试（必须能红，缺一不算达标）

1. 构造 `run_verify` 返回非零的 fixture → 断言**没有任何 PR 被 merge**、
   默认分支 HEAD 与运行前逐字节相同、回执里 `outcome=escalated` 且写步骤被显式记为跳过；
2. 构造 `worktree_cherry_pick` 返回 `ok:false` 的 fixture → 同上断言；
3. 若保留 fallback PR 路径：构造走该路径的 fixture → 断言产出分支的树里
   **没有** `.dev-dispatch/` 与 `.dd-evidence/` 任何文件；
4. 正向回归：全链每步 ok:true 时仍正常 `harvested`，PR 正常 merge——
   证明本单没有把收割链改成「永不写」。

**变异枪要求**：把 D1 的写前闸去掉后，测试 1 与 2 必须转红。请在交付说明里写清你实际跑过的变异结果。

## 边界（硬线）

- 不得改 `outcome` 词表、不得新增终态；H2 的「任一 step ok:false → escalated」语义一字不动。
- 不得改 allowlist 语义与 `/data/fleet-graph/supervisor/harvest-allowlist.json`（该文件禁止触碰）。
- 不 `reset --hard`、不碰任何生产主 checkout。
- 既有测试一行不许删。
- 本单只改收割链的写序与 fallback，不动 H6 的清场逻辑（已在 main）。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run pytest -q tests/test_harvest.py
```
