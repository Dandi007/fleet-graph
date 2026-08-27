# R5-1: deep-research 串行闭环图（fleet-graph 第三张 L2 业务图）

## Goal

新增 submit 驱动的 research 图（串行 W=1 版）：`graphs/research_pipeline.py`、`graphs/research_runner.py`、`cli.py` 的 `research run` 子命令，及配套测试。一个 research 工单跑通 seed → {dispatch → collect → harvest → converge} 循环 → synthesis → finalise，产出 run root 下的 report.md 与 result.json。

## Design constraints (binding)

1. **L1 零改动（硬验收）**：不得改动 `src/fleet_graph/executors/`、`src/fleet_graph/scheduler/`、`src/fleet_graph/state/`、`src/fleet_graph/bus/`、`src/fleet_graph/graphs/guards.py`、`src/fleet_graph/acceptance.py`、`src/fleet_graph/graphs/adapters.py` 的任何一行。只允许新增 `graphs/research_pipeline.py`、`graphs/research_runner.py`，修改 `cli.py`（新增 subparser 与 handler），新增测试文件。
2. **thread identity**：`research_id` 由问题文本内容寻址派生（sha256 前 12 hex，前缀 `r-`）；`thread_id = f"{research_id}:g{generation}"`。参照 `graphs/runner.py` LineConfig 的形状。
3. **checkpoint**：`SqliteSaver` 落 `run_root/checkpoint.sqlite3`；resume 语义参照 `graphs/runner.py` 的 `resume_start`（snapshot.next 非空 → `invoke(None)` 精确续跑）。
4. **run artifacts 走 dd 形状**：`events.jsonl` + `result.json` 由 runner 侧写（参照 `graphs/dd_runner.py`），**禁止使用 `state/run_artifacts.py` 的 RunArtifacts/heartbeat**（其 phase 封闭枚举不含 research 阶段）。
5. **节点纯度**：dispatch/collect/harvest/converge/finalise 全部是 script 节点（无 LLM 调用）；seed 与 synthesis 是 LLM 节点。seed 用 `executors/text_node.py` 的 TextNode（纯文本进出）；synthesis 用 `executors/agent_run.py` 的 AgentRunLauncher（一次性 structured run，`write=False`，语料经 `--prompt-file` 文件投递，run id 用 `derive_run_id(thread_id, "synthesis", attempt)`）。worker 同样经 AgentRunLauncher，run id `derive_run_id(thread_id, f"worker/{clue_id}", retry+1)`——kill-restart 后同 id 重派即 re-adopt（launcher 已保证幂等）。
6. **收敛判定是纯函数**：`converge(state) -> "continue" | "converged" | "capped" | "partial"`——纯计数：coverage 零增长连续 N 轮 → converged；clue 总数/深度触顶 → capped（**capped 不得报成 converged**）；存在 blocked clue（retry 耗尽）且其余收敛 → partial。bounds 为新的 frozen dataclass（max_clues、max_depth、zero_growth_rounds、max_rounds），纯计数无时间。
7. **state 只装 id 与计数**：findings/report 正文落 run root 文件（`evidence.jsonl` 逐条 append），state 里只留 clue 板（id/status/depth/retry）与计数。clue 状态机：open → dispatched → done | blocked（retry<2 失败回 open，=2 置 blocked）。单 clue 失败绝不 fault 整图。
8. **role 名**：worker 用 role `research_worker_local`、synthesis 用 `research_synth`（agent-runtime 侧 role 由另单交付；本单测试全部用 fake launcher/text node，不依赖真实 role 存在）。
9. CLI：`fleet-graph research run --question <text> [--run-root PATH] [--generation N] [--max-clues N] [--checkpoint PATH]`，终态写 result.json 并以 JSON 打印到 stdout；exit 0 当且仅当终态 ∈ {converged, capped, partial}（fault 非零）。

## Tests (required)

- 图级端到端（fake TextNode + fake launcher）：一题两轮收敛，evidence.jsonl 逐条落盘、report.md 生成、result.json 终态 converged。
- capped 负例：max_clues 触顶终态为 capped 而非 converged。
- partial 负例：一个 clue retry 耗尽 blocked，终态 partial。
- resume：checkpoint 中断后重跑进入精确续跑分支（参照 tests/test_line_restart.py 的验法）。
- worker run id 派生稳定：同 thread 同 clue 同 retry 派生同 id。

## Constraints

- 遵循仓内注释风格（每条规则带理由）；中文注释、术语英文。
- 不改动本 spec 未列出的既有文件（conftest/工具配置除非测试确需）。
- 不触碰部署、服务、生产路径。

## Acceptance

```dd-acceptance
make verify
```
