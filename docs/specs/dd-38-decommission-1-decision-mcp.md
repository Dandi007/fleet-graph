# decommission 批次 1：删 decision MCP（模块+cli 子命令+systemd 单元+测试）

## 背景
goal 的设计正本 design.md §9 落地路径第三条与 GO-14 B6（golden-order 第 14 段：「新的写好了的话 重复的全都下线」）要求 minimal 建成后把重复的旧部件下线。dd-36 已产出 `docs/specs/minimal/decommission.md`（盘点表 + 四个批次），但明确不删任何生产代码。本张执行**批次 1**：`src/fleet_graph/decision_mcp.py`（1025 行，判定 batch-1 可删，生产侧只有 cli.py 接线）。minimal 侧取代物已在 release 上：`src/fleet_graph/minimal/mcpserver.py` + `mcptools.py`（goal_message / goal_steer 等经 control.jsonl，引擎在步骤边界读；design §7.6、protocol §10），同步裁决投递面不再独立存在。删除落在 release 分支，release→main 是人工闸，人会在合并时过目。

## 要改什么
1. `git rm src/fleet_graph/decision_mcp.py`。
2. `src/fleet_graph/cli.py`：删掉 `decision` 子命令——parser 注册、handler（内含 `from fleet_graph.decision_mcp import serve`，约 line 775）、以及 state-dir 帮助文案（约 line 1848）。删完 `fleet-graph --help` 不再出现 decision。
3. `git rm deploy/systemd/fleet-graph-decision-mcp.service`。
4. `config/decision-mcp-reserved-ports.json`：先 `rg -n decision-mcp-reserved-ports src tests scripts config` 确认剩余读者。若只剩注释（`src/fleet_graph/line_state_mcp.py` 里是注释镜像），删掉该配置文件并清掉注释引用；若发现真读者，保留文件并在 decommission.md 写明保留理由。
5. 测试：`git rm tests/test_decision_mcp.py`；`tests/test_m2_dd_gate_delivery.py` 依赖 `fleet_graph.decision_mcp`——若整文件都围绕该投递面则整删，否则只删相关用例、保留其余（不要留 skip）；`tests/test_deploy_unit.py` 删掉 decision-mcp 单元断言与 DEFAULT_PORT / 保留端口断言（约 line 38、401-411）。
6. 清掉 `src/fleet_graph/line_state_mcp.py` 等保留文件里对 decision_mcp 的注释/docstring 引用。
7. `docs/operating.md` 里 `decision serve` / :5614 / `decision_deliver` 的运维段落（含约 line 596 表格行）改成「已下线（decommission 批次 1）」一行，不重写其它段落。
8. `docs/specs/minimal/decommission.md`：批次 1 标为已执行并列出实际删除清单；`docs/specs/minimal/context.md` 第 3 行同步一句进度。

## 不改什么
- 不动 `src/fleet_graph/minimal/**` 与 `tests/test_minimal_*.py`。
- 不动 `arbiter/`、`scheduler/`、`supervise/`、`goal_interrupt/`、`decision_bridge/`、`graphs/`、`bus/`、`state/` 的功能代码（后续批次各自处理）。
- 不重构任何保留代码、不顺手改风格；不动 pyproject 的 `fleet-graph` console script 本体与 minimal 的两个 script。
- 不为被删能力做替代实现。

## 怎么验收
`make verify` 全绿（lint + 全量 pytest + conformance 三脚本）；除 docs 的历史说明外全树无 decision_mcp 引用；`uv run fleet-graph --help` 不含 decision 子命令。
