# fleet-graph decision-mcp 测试/验收隔离：不落 DEFAULT_STATE_DIR（卫生加考）spec

- 目标仓：`/data/code/self/fleet-graph`（https://github.com/Dandi007/fleet-graph）
- 分类：A 类生产污染缺陷（decision-mcp 的测试/验收把假数据写进生产账本）

## 1. 现象与真因

- 现象：decision-mcp 的测试/验收把 fixture 假数据写进生产账本 `/data/fleet-graph/decision-mcp/deliveries.jsonl`（`delivered_total` 由 22 涨到 26；`wf-1` 夹具在 03:43/03:45/03:54/03:56 仍在注入）。fresh 实测：`deliveries.jsonl` 29 行，其中 26 条 `line=wf-1`（action_key `mcp:wf-1:g2:APPROVE`、card `card-1`、question `q-1` 的合成夹具）+ 3 条 test 负例 refused（wf-6475fd `LINE_NOT_PARKED` / wf-does-not-exist `NO_WAITING_PARTY`）。
- 真因（读源码坐实）：`decision_mcp.py` `DEFAULT_STATE_DIR = Path("/data/fleet-graph/decision-mcp")`；`DeliveryLedger(state_dir=DEFAULT_STATE_DIR)` 且 `build/mcp` 层仅当显式给 `--state-dir`/`FLEET_GRAPH_DECISION_MCP_STATE_DIR` 才覆盖，测试/验收未覆盖 → 一律落生产 DEFAULT_STATE_DIR，fake 投递被 append 进真账本。

## 2. 修复方向（契约）

1. decision-mcp 的测试与 dd-acceptance **一律用临时 state-dir**（`tmp_path` 夹具 / `FLEET_GRAPH_DECISION_MCP_STATE_DIR` / `--state-dir`），**绝不落 `DEFAULT_STATE_DIR`**；ledger/metrics 文件名仍由 `state_dir` 派生。
2. 只读纪律副作用：任何测试都不得对 `/data/fleet-graph/decision-mcp/*` 做写；必要时在测试里加「生产账本不可写」的守卫断言。
3. 判据（能红）：**跑一遍测试套件前后，`/data/fleet-graph/decision-mcp/deliveries.jsonl` 行数不变**；若某用例硬编码 `DEFAULT_STATE_DIR` 写入 → 该用例必须红（或套件前后生产账本行数差 > 0 视为红）。

## 3. 真机判据

1. 正向：套件全绿后，`wc -l /data/fleet-graph/decision-mcp/deliveries.jsonl` 与跑前一致（零新增污染行）。
2. 反向（不破坏功能）：临时 state-dir 下投递→消费、四失败模式拒绝、吞掉率 metrics 仍正确（只在 temp dir 内发生）。

## 4. 验收（dd-acceptance）

```dd-acceptance
uv run pytest -q tests/test_decision_mcp.py
make verify
```

## 5. 铁律

- 代码/review 一律交 dev-dispatch；git worktree；生产主 checkout 只读、仅 ff-only。