# 案A改写 ③：dry-run 解析归属但零授权（先观测后授权）

来源：监督面直写 goal.md 2026-09-02 15:0x「🔴 案 A 改写」第八节任务 3。
监督面已逐条真机证实现状（取证锚点），实现据此，不重证。

## 取证锚点（机械事实）
1. `DefaultHarvestOps.resolve_canonical_repo(record_repo_path, remote_url, allowlist_repo_paths)` 的
   四条解析路径——直接命中 / linked-worktree 归属 / origin 本地路径 / origin URL 映射——
   **每一条的收口都是「命中 allowlist」**，解析不到就返回 `(None, 理由)`。
2. ⇒ 一个仓不在 allowlist 里，反应器连「这单该收进哪个仓」都算不出来，自然无法留下
   「它本来会怎么做」的记录；而 allowlist `_provenance` 里上一任定的扩围前提是「生产仓扩围待
   M3/M4 对账窗（≥3 天分歧清零）后重签」——**要分歧数据先授权、要授权先有分歧数据，死锁**。

## 要什么
- 让「归属解析」与「allowlist 授权判定」**解耦**：新增纯读解析路径
  （如 `resolve_canonical_repo_unfiltered`，只读、不做 allowlist 授权收口），使反应器能在
  **不授予写权限**的前提下解析出「这个 record 本会归属哪个 canonical 仓」。
- 一个**不在 allowlist** 的仓，其 e5 报告必须能记下：本会解析到的 canonical 仓 + 本会执行的写步骤
  （`pr_squash_merge` / `ff_only_pull` / `deploy`），同时 `writes_skipped` 覆盖全部写步骤、
  **真机零写**（不产生任何 git/部署写原语）——使「先观测后授权」在产品里成立，而非靠监督面
  手工 import 函数跑脚本。

## 判据（可红）
- **正向**：不在 allowlist 的仓 → 报告如实记录 `would-resolve canonical` + `would-do` 写步骤清单，
  且 `writes_skipped` 覆盖全部写步、真机零写（断言无任何写原语被调用）。
- **变异**：dry-run 路径真实执行任一写原语（pr_squash_merge / ff_only_pull / deploy / git push）或
  漏记 `writes_skipped` → 必须有用例变红。

## 交付约束
- 只改 `src/fleet_graph/supervise/harvest_ops.py`（纯读解析口）、`src/fleet_graph/supervise/harvest.py`
  （intake dry-run 留痕 + writes_skipped）、与 `tests/test_harvest.py` 及必要 fixture；
- **不得改动、不得签发** `/data/fleet-graph/supervisor/harvest-allowlist.json`；
- 不改 E5/E6/E7 事件词表；不部署、不重启。

```dd-acceptance
uv sync --frozen
make verify
```