# M1 —— 线的运行状态上 MCP（只读 line-state MCP 面）

把 fleet-graph 的 `:7494` 读模型能力做成**只读** MCP 工具（wf-525fd4 goal.md 的 M1，缺的那条腿，优先做）。字段面已稳定（同 `:7494 /v1/lines`）：`folder_id / generation / round / phase / heartbeat_age_s / terminal / parked / wake_facts / release_id / run_id / wake_facts_stale`。

## 交付物

1. 一个新的只读 line-state MCP 面，参考 `src/fleet_graph/decision_mcp.py` 的 FastMCP 模式（`MCP_SERVER_NAME` + `build_*_mcp_server(...)` 可无传输层单测 + `serve(...)` + reserved-ports 纪律）。注册名/端口遵循既有 reserved-ports 单一来源纪律，不得撞已占用 loopback 端口。
2. 工具切分**窄且自解释**（golden-order 第 2 条）：至少「列出全部线状态」与「取一条线状态」两个窄工具，逐字段返回上述字段面；**禁止**做成「一个 call 工具 + 一个 path 参数」那种把 native 面原样包一层的假 MCP。
3. `tests/test_m1_line_state_mcp.py`（本单验收目标）。

## 双向判据（不可弱，逐字对齐 goal.md M1）

- **阳性**：一条正在跑的线，MCP 工具返回的 `generation / round / phase` 与**同刻** `:7494` 的回答**逐字段相等**（同源，不许另造一套读法）。
- **阴性**：MCP 工具**不得暴露任何写能力**——线状态面是只读的。变异：给它加一个写原语 → 必须有用例**变红**。

## 红线纪律

- 读取必须走既有 `:7494` 读模型（或与其同一个视图函数/同一份数据源），**禁止自创第二套读法**（否则阳性判据无从逐字段对齐）。
- `build_*` 函数必须可无传输层单测（参考 `decision_mcp.py`）；测试不得触碰生产账本/生产文件（health-isolation 规则）。
- 测试不得因「不可达」静默变绿：`:7494` 不可达时如实报「不可判定」并按**红**处理，同时给出不可判定的证据。
- 阴性判据要可回归：测试须断言 `tools/list` 结果与各工具 `inputSchema` 中不出现任何写原语/写动作（如 set/update/clear/patch/deliver/wake/park），一经加入写原语该测试即失败变红。
- 不删除/改写任何既有测试或 `scripts/` 下既有脚本；`make verify` 不得回归（新增文件过 ruff/format）。

## 验收

```dd-acceptance
uv sync --frozen
uv run pytest -q tests/test_m1_line_state_mcp.py
```