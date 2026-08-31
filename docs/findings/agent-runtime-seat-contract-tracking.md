# agent-runtime 座位层契约违约——上游立案跟踪（在案）

> R7（spec dev-fg-119252fbfe8c）「上游跟踪」：agent-runtime 座位层把契约违约报成
> succeeded/exit 0 不本图修，判据只求立案号 `dev-fg-67feadc91821` 在案。本文件即
> 该立案号的**在案记录**（`scripts/check_research_preflight.py` 判据③核对它）。

## 立案

- **立案号**：`dev-fg-67feadc91821`
- **层面**：agent-runtime（上游，本仓 fleet-graph 只跟踪、不做容忍式补丁）
- **违约形态**：座位层把「契约违约」报成 `succeeded` / `exit 0`（本卷不在图里修——
  不得把「succeeded/exit0」当合法，判据只核对立案号在案）
- **本仓处置**：只跟踪不修。fleet-graph 侧以自己的失败语义哨兵（
  `src/fleet_graph/research_sentinel.py`）保证「该响必响」，但 agent-runtime 座位
  层的修复归上游立案单，与本卷解耦。
