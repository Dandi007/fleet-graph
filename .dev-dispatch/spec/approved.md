# fleet-graph 裁决通信改 MCP 同步接口 spec

- 目标仓：`/data/code/self/fleet-graph`（https://github.com/Dandi007/fleet-graph）
- 分类：用户拍板需求（直写 goal 2026-09-02 08:3x「裁决通信应改为 MCP 接口」），A 类基建缺陷
- 形态自决：新/并入现有 MCP、注册名、是否复用 `development_gate` 由 implementer 定；本 spec 只定契约

## 1. 现象与真因

- 现象：裁决投递 = 「往任意形态 agent-bus 消息通道发一条 + 祈祷有人认领」。实证：`wf-a6cfea` 一条早已批准的裁决四投四败（refused line not parked → no waiting owner ×2 → goal 直写 105s 未唤醒），线空等至今；今日 429 条裁决吞 188 条（吞掉率 43.8%）。
- 真因：自由格式通道把「投递成功（HTTP 200）」与「被 owner 消费」拆成两件无同步反馈的事。四类失败模式：①时序竞态（未驻停即拒/过晚被驻停基线吸收）；②认领匹配（card/question 对应多来源——板上 note、arbiter subject_id、scheduler 登记值——互不一致）；③载荷形状（`work.decision.v1` 四字段、decision 仅 APPROVE/REJECT）；④静默丢弃（`state=swallowed` 需另查 `/v1/decisions` 才见）。

## 2. 修复方向（契约）

1. 新 MCP 工具，**同步定论**：一次调用返回二者之一——「已送达且被 owner 消费」或「明确拒绝原因（线未驻停 / 无此等待方 / 载荷不合法）」。禁止 HTTP 200 后静默吞。
2. **入参最小无歧义**：调用方只给 `line` + `decision`（APPROVE/REJECT）+ `reason`；`question`/`card` 对应关系由服务端从该线驻停态自行解析（不要求调用方在三来源里猜）。
3. **时序不归调用方**：线未驻停时阻塞等待（带超时阈值）或返回「可重试 + 明确条件」信号，绝不丢弃。
4. **载荷合法在调用点即报错**（decision 只收 APPROVE/REJECT，缺字段/畸形即拒），非发出去后被下游拒。
5. **可观测**：每次投递→消费全链路可查；吞掉率上 metrics。
6. **旧 bus 通道保留兼容**，不做破坏性切换；先并行对账、后凭证据退役。

## 3. 真机判据（必须能红，四失败模式各一阴性用例，每条须拿明确拒绝而非静默成功）

1. 线未驻停时调用 → 明确拒绝「线未驻停」（或阻塞/可重试信号，绝不吞）。
2. question/card 对应关系不一致时调用 → 明确拒绝（服务端解析失败即有同步报错）。
3. 载荷非法（缺字段 / decision 非 APPROVE|REJECT）→ 调用点即报错。
4. owner 不存在时调用 → 明确拒绝「无此等待方」。
- 阳性：线 `parked + waiting_on=decision` 时调 APPROVE → 返回「已送达已消费」，且该线驻停解除（waiting_on 清除 / 心跳推进）。

## 4. 验收（dd-acceptance）

```dd-acceptance
uv run pytest -q tests/test_decision_bridge.py tests/test_decision_publisher.py tests/test_bus.py
make verify
```

## 5. 铁律

- 代码/review 一律交 dev-dispatch；git worktree；生产主 checkout 只读。
- 旧 bus 通道兼容不破坏。