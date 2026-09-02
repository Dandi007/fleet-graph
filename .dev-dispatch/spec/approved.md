# fleet-graph 裁决通信改 MCP 同步接口（successor，含 gate 返工 R1/R2）spec

- 目标仓：`/data/code/self/fleet-graph`（https://github.com/Dandi007/fleet-graph）
- 分类：用户拍板需求（裁决通信改 MCP 同步接口）的 successor 单
- 前身：dev-fg-4fc76d3bebd4 已 gate REJECT（head 2be6ed67），本单携带返工新信息，属「spec 意图变化」换新号——**旧单 shut、无信息不重派**

## 0. successor 新增信息（相对前身 spec 的变化，缺一则属无信息重派）

- **R1 端口更换**：把决策 MCP 服务端口从 **5613 改为 5614**（监督面已扫：5602-5613 连续占满、5614/5615/5616 空闲；真机 `ss` 复核一致）。三处一并改：`deploy/systemd/fleet-graph-decision-mcp.service` 的 `--port`、`src/fleet_graph/decision_mcp.py` 的 `DEFAULT_PORT`、`src/fleet_graph/cli.py` 的端口 `default=`；并把 unit/代码内注释按事实重写（把 5613 补进已占端口清单）。
- **R2 保留端口清单入仓 + 能红的端口断言**：把「已占端口清单」从注释提升为仓内真文件（单源，如 `config/decision-mcp-reserved-ports.json` 或模块内常量，形态由 implementer 定）；加一条能红的用例——断言「本 unit 声明的默认端口 不在 保留/已占端口清单中」。机械 red/green：把端口改回 5613 该用例**必须红**，用 5614 必须**绿**。**R2 属 CI/验收期判据，禁止做成「启动时探测端口是否被占」的运行时行为。**

## 1. 现象与真因（原契约不变）

- 现象：裁决投递 = 「往任意形态 agent-bus 消息通道发一条 + 祈祷有人认领」。实证：`wf-a6cfea` 一条已批准裁决四投四败、线空等至今；今日 429 条裁决吞 188 条（43.8%）。gate 对前身单补一刀：默认端口 5613 与已在跑服务冲突。
- 真因：自由格式通道把「投递成功（HTTP 200）」与「被 owner 消费」拆成两件无同步反馈的事。

## 2. 修复方向（契约）

1. 新 MCP 工具，**同步定论**：返回「已送达且被 owner 消费」或「明确拒绝原因（线未驻停 / 无此等待方 / 载荷不合法）」，禁止 HTTP 200 后静默吞。
2. **入参最小**：`line` + `decision`（APPROVE/REJECT）+ `reason`；`question`/`card` 由服务端解析。
3. **时序不归调用方**：线未驻停 → 阻塞等待（带超时）或返回可重试明确信号，绝不丢。
4. **载荷合法调用点即报错**（decision 仅 APPROVE/REJECT）。
5. **可观测**：投递→消费全链路可查，吞掉率上 metrics。
6. **旧 bus 通道保留兼容**，不破坏性切换。
7. **端口 5614**（R1）+ **保留端口清单入仓并断言**（R2）。

## 3. 真机判据（四失败模式阴性 + 阳性 + 端口 red/green）

1. 线未驻停时调用 → 明确拒绝（或可重试信号，绝不吞）。
2. question/card 对应不一致时调用 → 明确拒绝。
3. 载荷非法（缺字段 / decision 非 APPROVE|REJECT）→ 调用点即报错。
4. owner 不存在时调用 → 明确拒绝「无此等待方」。
- 阳性：线 `parked + waiting_on=decision` 调 APPROVE → 返回「已送达已消费」且驻停解除。
- **端口（R2）**：改回 5613 → 端口断言用例红；用 5614 → 绿。

## 4. 验收（dd-acceptance）

```dd-acceptance
uv run pytest -q tests/test_decision_bridge.py tests/test_decision_publisher.py tests/test_bus.py
make verify
```

（R2 端口断言用例由 implementer 新增于 tests/，被 `make verify` 全量 pytest 覆盖；改 5613 红、改 5614 绿为本单 mechanical red/green 判据。）

## 5. 铁律

- 代码/review 一律交 dev-dispatch；git worktree；生产主 checkout 只读、仅 ff-only。
- 旧 bus 通道兼容不破坏。