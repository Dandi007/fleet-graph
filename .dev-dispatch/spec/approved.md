# H3 本地 canonical checkout 与 origin 分叉时 pull 前检测并机器可读 escalate（不得自动 reset）

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：监督面 10:1x 立案（goal.md 顶部 🔴块 H3「可恢复性」）。不是判据变更，是缺陷修复。
- 类别：缺陷修复（分叉恢复路径缺失），不改 deny-all、不改 allowlist 配置文件、不改判据。

## 根因（已实读，非推断）

M3 e2e 第三轮本地 checkout HEAD 停在 `bb026e3`（#201 之前一次「本地假合并」遗留残骸，
从未 push），与 `origin/master` 1:1 分叉；`git pull --ff-only` 报
`Diverging branches can't be fast-forwarded`（`ff_only_pull ok:false`），但 pull 节点只记
ok:false 就继续走 deploy/verify_real，链没有恢复路径，分叉错误最终还可能滑进 harvested。

关键约束：**绝不自动 reset / checkout -f / 强制覆盖**——那会吃掉别人在 canonical 主 checkout
上的本地工作。残骸 `bb026e3` 的清理动作也**由人做、不进自动化**（#201 修了行为但没清残骸）。

## 交付 A：ops 层分叉检测读口（机械层，Guard D 豁免）

`src/fleet_graph/supervise/harvest_ops.py`（`HarvestOps` 协议 + `DefaultHarvestOps`）：

新增 `detect_divergence(self, repo: Path, default_branch: str) -> dict[str, Any]`：
- 读本地 HEAD：`run_git(repo, "rev-parse", "HEAD")`；读远端 tip：`run_git(repo,
  "rev-parse", f"origin/{default_branch}")`。
- 用 `run_git(repo, "merge-base", "--is-ancestor", local, origin_tip)` 与反向判定是否分叉；
  任一侧读取失败?无 origin → 保守按「无法判定」返回，不留分叉漏检。
- 返回机器可读 `{"diverged": bool, "local_head": str|None, "origin_head": str|None, "detail": str}`。
  本方法是纯读口，**不含任何 reset/checkout/强制写**。

## 交付 B：编排层 pull 前检测分叉并 escalate

`src/fleet_graph/supervise/harvest.py::pull` 节点：

1. 在执衈 `ff_only_pull` **之前**调用 `deps.ops.detect_divergence(repo, default_branch)`。
2. 若 `diverged` 为真：记录 `ff_only_pull` step `ok:false` + 机器可读 escalate 字段
   （如 `escalate: "HARVEST_DIVERGED_LOCAL_VS_ORIGIN"`，附带 `local_head`/`origin_head`/
   `detail`），设 `outcome = OUTCOME_ESCALATED`，**立即走 receipt**（经既有 conditional
   edge 或新增「分叉即跳 receipt」路由），不再跑 deploy/verify_real。
3. 未分叉 → 走既有 `ff_only_pull`；pull 失败（如非分叉的其它原因）仍如实 ok:false，
   交由 H2 修正的 postconditions 计入。
4. **绝不调用**任何 reset / checkout -f / clean -f 之类强制原语；分叉时写动作只字不动。

## 交付 C：测试（tests/test_harvest.py，fake ops 注入）

1. **分叉 fixture**：fake ops `detect_divergence` 返回 `{"diverged": True, ...}` → 断言
   `outcome == OUTCOME_ESCALATED`、`ff_only_pull` step 带 `escalate` 字段且 `ok:false`、
   且 `calls` 里**没有任何** reset/checkout 类写动作（deploy/verify_real 也不跑）。
2. **未分叉 fixture**：`detect_divergence` 返回 `{"diverged": False}` + pull ok → 走正常
   链、outcome==harvested（正向回归）。
3. 既有用例零回归（`make verify` 全绿）。
4. 显式断言：实现方不得引入任何自动 reset 路径（测试天然约束，因 fake 无 reset 方法）。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只改 `src/fleet_graph/supervise/harvest.py::pull` + `harvest_ops.py`（新增分叉读口）+
  `tests/test_harvest.py`；不触 allowlist 配置文件、不改 deny-all、不改判据。
- **分叉只 escalate、不自动 reset**：清理残骸（如 `bb026e3`）由人做，写清理动作不在本单。
- 一切改动走本 development worktree + PR，不直改 main；生产主 checkout 仅读。