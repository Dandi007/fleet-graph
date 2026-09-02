# M5 —— 监督面接手：一次调用拿到全部现状（优先级最高）

把「监督面冷启动接手」做成**一个只读 MCP 工具**，一次调用返回一个零上下文 session 接手所需的**全部**，且每一项都是权威值而非线索（wf-525fd4 goal.md M5，用户 2026-09-02 15:5x 追加）。参考 `src/fleet_graph/decision_mcp.py` 的 FastMCP 模式（`MCP_SERVER_NAME` + `build_*_mcp_server(...)` 可无传输层单测 + `serve(...)` + reserved-ports 单一来源纪律）。注册名/端口遵循既有 reserved-ports 单一来源，不得撞已占用 loopback 端口。

## 交付物

1. 一个只读「监督面接手」MCP 工具（窄且自解释，非「一个 call 工具 + 一个 path 参数」假 MCP）。一次调用返回如下每一项，权威值而非线索：
   - **当前监督卷 folder_id**（没有它就接不上历轮处置与待办）
   - **授权模式**（full-auto / semi-auto；缺失须 fail-safe 到 semi-auto）
   - **名册**：总数 / enabled / 每条线 alias+seat
   - **每条线运行状态**：terminal / parked / wake_facts_stale / generation / round / heartbeat_age_s
   - **等我拍板的清单**：驻停等裁决的线 **+** `awaiting_gate` 的 dd 单（两类，缺一不可）
   - **待收割的清单**：已放行、终态 complete、但产物尚未进默认分支的单
   - **维护窗 flag 状态**（生效期间禁止一切拉起/修复）
   - **当前 main / release / 进程实际在跑的 release**（判「修复到底生效没有」）
2. `tests/test_m5_supervision_handoff.py`（本单验收目标）。

## 双向判据（不可弱，逐字对齐 goal.md M5）

- **阳性**：一个**零上下文**的新 session，只调这一个工具，就能正确回答「现在哪几条线在跑 / 谁在等我拍板 / 有什么该收割 / 我的作业账在哪一卷」。**验收方式就是真做一次冷启动演练，不是断言。**
- **阴性①**：**该工具不得暴露任何写能力**——接手是只读动作。
- **阴性②**：**任一项缺失或降级时必须显式说明**，不许静默返回空值让调用方以为「没有」。（踩过：把「读不到」当成「没有」，据此误判过一条线的状态。）

## 红线纪律

- 读取必须走既有 `:7494` 读模型视图（`/v1/lines` `/v1/decisions` `/v1/harvestable` `/v1/enrollments`）与既有 dd 服务/名册数据源，**禁止自创第二套读法**。
- 工具切分窄且自解释；这是**合成**（名册 × 线状态 × 待裁决 × 待收割 × 监督卷），不是把 `/v1/lines` 原样包一层。
- `build_*` 函数必须可无传输层单测（参考 decision_mcp.py）；测试不得触碰生产账本/生产文件（health-isolation 规则）。
- 阴性①可回归：测试断言 `tools/list` 结果与各工具 `inputSchema` 不出现任何写原语/写动作，一经加入写原语即变红。
- 阴性②可回归：对任一「读不到/缺失」的数据项，返回体必须显式 `unavailable`/`missing` 标记（不得返回空对象/空数组冒充「没有」）；测试注入缺失场景断言该标记存在。
- 阳性是冷启动演练：测试以零上下文身份只调这一个工具，断言返回体足以回答上述四问（含监督卷 folder_id、线状态、待裁决清单、待收割清单），且逐项与同刻 `:7494` 权威回答一致。
- `:7494` 或任一上游不可达时如实报「不可判定」并按**红**处理，同时给出不可判定的证据；不得静默变绿。
- 不删除/改写任何既有测试或 `scripts/` 下脚本；`make verify` 不得回归（新增文件过 ruff/format）。

## 验收

```dd-acceptance
uv sync --frozen
uv run pytest -q tests/test_m5_supervision_handoff.py
```