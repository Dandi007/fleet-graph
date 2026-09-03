# Spec 缺陷⑩（wf-8d9737）· worker_turn_timeout 根因：变量矩阵含模型/座位

## 背景
真机事实：goal 线 worker turn 出现 3000s 零产出超时（本线 2026-09-03 首轮亲历）；监督面观察（01:5x）：**切 glm-5.3 后 round2/3 零超时**——模型/座位是首要疑变量。现行机制（src/fleet_graph/executors/agent_session.py @36ebafc）：agent-session 回 `TURN_TIMEOUT` → `AgentSessionTimeout` → goal_line worker-turn guard 记 `worker_turn_timeout` 轮、streak breaker 决定去留——**机制在，但超时轮不记变量，根因不可归因**。

## 交付（全部在 fleet-graph 仓，base=36ebafc）
1. **变量矩阵落 record**：`worker_turn_timeout` 轮（与正常轮同路）在 rounds/progress 落档时必带变量矩阵字段：`seat`（agents.yaml 座位名）、`model`（agent-session argv 解析出的 -m 值/链）、`round_index`、`turn_timeout_seconds`、`input_bytes`（本轮输入 prompt+工具面载荷量级）、`output_evidence`（截止超时的产出信号：stdout 行数/最后时间戳/零产出布尔）。缺任一字段的超时轮 → 用例红。
2. **归因报表命令**：新增 `scripts/turn-timeout-report.py`（只读 rounds/progress），按变量矩阵分桶输出超时率（每 seat×model×round_index 桶：总轮数/超时轮数/零产出超时数），exit 0；无数据时如实报空不报错。
3. **矩阵书面落卷**：docs 或脚本 docstring 内落变量矩阵表（模型/座位、round 序号、输入体量、超时预算、产出信号），并填入已知观察：flash 座位 3000s 零产出超时 ≥1 例、glm-5.3 切换后 round2/3 零超时（监督面 01:5x）——作为首批数据点。
4. **最小缓解（仅机械面）**：若数据显示零产出超时集中于特定 model×round 组合，允许在 goal_line 增加「超时轮回放时把变量矩阵写进下一轮输入摘要」的机械透传（让接手模型看见上一轮死因）；**不得**自行调整超时预算或换座策略（那是监督面/用户面）。

## 判据（正/负双向）
- 阳性：构造一个 TURN_TIMEOUT 轮（fixture 注入 AgentSessionTimeout）→ rounds 记录含全部矩阵字段；report 命令分桶正确。
- 阴性：抹掉任一矩阵字段（如 model）→ 用例红；report 对缺失字段的旧记录按「变量缺失」单列桶，不静默丢弃。
- 阴性：report 弄虚作假（空数据报非空）→ 红。

## 边界
- 只动 rounds/progress 落档最小面、新脚本、新测试 `tests/test_turn_timeout_variables.py`；不改 agent-session/agent-runtime；不改超时预算；零测试删除。

## 验收（dd-acceptance 围栏，逐字冻结）
```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_turn_timeout_variables.py'
bash -lc 'make verify'
```