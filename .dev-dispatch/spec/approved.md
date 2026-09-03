# M1 验收脚手架补全 —— verify-mcp-only.sh 加 :5615 线状态面探针

## 背景
wf-525fd4 goal.md M1 的线状态只读 MCP 面已在 main：`src/fleet_graph/line_state_mcp.py`，服务名 `fleet-graph-line-state`，serve loopback `:5615`，工具 `list_line_states` 与 `get_line_state(folder_id)`（字段面同 `:7494 /v1/lines`：folder_id/generation/round/phase/heartbeat_age_s/terminal/parked/wake_facts/release_id/run_id/wake_facts_stale）。

但 `scripts/verify-mcp-only.sh` 的 M1 探针只 loop 5608/5609/5610/5611/5612/5614 六口，漏了线状态面所在口 `:5615`。即便线状态面部署后，验收脚本也无法机械判 M1 红绿（监督面 2026-09-03 goal 级验收据此把 DoD1 判红）。本单把验收入口补诚实。

## 交付物
1. 改 `scripts/verify-mcp-only.sh` 的 M1 段（内嵌 python heredoc），把线状态面 `:5615` 纳入探针：
   - 经 `tools/list` 确认 `list_line_states`、`get_line_state` 两个工具注册存在；
   - 经 `tools/call` 实机调用 `list_line_states`（列全部）与 `get_line_state`（取一条正在跑的线的 folder_id），把返回的 `generation`/`round`/`phase` 与同刻 `http://127.0.0.1:7494/v1/lines` 里该线逐字段相等比对（同源，禁另造读法）；任一字段不等即该判据红并给出字段级证据。
   - `:5615` 不可达（connection refused，尚未部署/未 live）时，诚实报「不可判定 + connection refused 证据」，计红；不得伪造为绿、也不得伪造为「不存在 line-state 工具」。
2. M1 阴性保持：`:5615` 的 `tools/list` 与每个工具 `inputSchema` 不得含写原语（set/update/clear/patch/deliver/wake/park）；给线状态面加一个写原语须有一条能红的用例（`tests/test_m1_line_state_mcp.py` 已覆盖同义约束，复用/扩即可）。
3. 扩 `tests/test_mcp_only_scaffold.py`，把「M1 探针覆盖 5615」钉进回归：断言脚本输出在 `:5615` 不可达时 M1 判据给出「connection refused」证据、或静态断言脚本文本含 5615 与逐字段比对（generation/round/phase 对 :7494）逻辑。
4. 既有「探测纪律」不变：只使用本机通用命令（bash/python3/urllib），不可判定计红，不硬编码红绿；扩改不得破坏 M0/M2/M3/M4 段与既有红色计数语义。

## 双向判据（对齐 goal.md M1，不可弱）
- 阳性：一条正在跑的线，MCP 工具返回的 generation/round/phase 与同刻 :7494 逐字段相等（同源）。
- 阴性：线状态面只读，不得暴露写能力；加写原语须有用例变红。

## 红线
- 不越界部署线状态面（部署归监督面）；不退役任何现役面；不写本 deliverables 之外的东西。
- prod 主 checkout 只 ff-only；改动只进 worktree。

## 验收
```dd-acceptance
uv sync --frozen
uv run pytest -q tests/test_mcp_only_scaffold.py
```