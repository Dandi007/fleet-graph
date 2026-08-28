# E1 决议事件桥扩展

**输入设计：** 已定稿的 `design-e1.md`（2026-08-28），尤其是第 1.1、3--9、10 节。

## 目标

在 fleet-graph 中交付 E1 决议事件桥扩展：把 `board:work-notes` 上已发布且有效的 `work.decision.v1` 以纯读方式，严格映射到唯一的等待实体，并通过既有受控入口恢复。不得改变 agent-bus 内核、队列租约语义、现有轮询兜底或生产部署流程。

## 相较 `dev-fg-8af1460b39b6` 的新增范围

本单是对已完成观测切片 `dev-fg-8af1460b39b6` 的扩展，必须明确实现并回归验证以下新增项：

1. 真实 SQLite 持久层，采用 WAL 和 `synchronous=FULL`，同一立即事务保存独立的 `event_id`/`channel_seq` source cursor 与 receipt/intent；cursor 只在事件获得终态 disposition 后推进，SQLite 不可读、不可写、锁住或损坏时 fail-closed，绝不以内存 cursor 继续。
2. durable receipt 状态机及崩溃恢复：持久化 `intent_recorded` 后才外呼；receipt 含 source message、精确 target/generation/question、action key、状态、reason 和 source event；重启安全重放且不丢事件。
3. 严格 resolver：只接受 allowlist channel `board:work-notes` 和精确 kind `work.decision.v1`；验证 payload/decision/refs，重新核对 waiting owner 的 card、question、generation 和当前状态；零匹配、多匹配、stale 或 invalid 均写结构化终态 no-op receipt，绝不模糊猜测或恢复任意 URL/目标。
4. owner-side action-key durable 去重：action key 精确为 `e1:<source_message_id>:<target_kind>:<target_id>:<generation>`，DD 与 line 恢复拥有者以 action key 和目标 generation 作持久唯一约束；bridge adapter 透传它，重复 transport 调用必须返回同一逻辑成功而不得重复验收/恢复。
5. 独立 `fleet-graph-decision-bridge.service` unit：专用最小权限 principal/credential、受限状态目录，`Restart=on-failure`，可独立 start/stop/restart/mask，且不得与 `fleet-graphd.service` 具有 `Requires=`、`PartOf=` 或进程连坐；bridge、observer 和子 unit 不得继承或获得 decision publish credential。
6. 隔离运行态的真实进程演练：fake bus、fake resume owner、真实 SQLite、真实 bridge 进程，不得以 mock 直接调用 handler 代替。覆盖小于 5 秒恢复和 kill/restart exactly-once；并覆盖 bus 不可用、SQLite 不可写时零 resume 且旧轮询 fixture 未停用。

## 边界与安全约束

- 只读调用 `GET /v1/events?after=&channel_id=board:work-notes` 和必要时 `GET /v1/channels/board:work-notes/messages?after_seq=`；禁止 consume/inbox_consume/ack/nack、subscription、bus schema/storage/delivery 修改、publish 或直接写 terminal/checkpoint。
- 保留 GateAutoResumer、wake 探针和所有既有轮询作为约 60 秒兜底；任何 5 秒目标失败均不得删除兜底。
- DD 恢复必须经 `development_gate(development_id, resume=True)` 的既有认证适配器，并重新读取 board verdict；line 只能经已登记、只允许该 waiting generation 的受控入口恢复。
- 若稳定可恢复 event identity/order、只读最小 ACL、或可持久去重的 owner resume entry 任一不可实现，则停在 observe-only，发 question note；不得通过 lease/consume、扩大 token 或直接写状态文件绕过。
- token 不得进入 unit 文本、日志、SQLite、receipt、测试输出或环境快照。
- 仅修改 fleet-graph；不改 agent-bus 内核。

## 委托与 worktree 约束

所有代码编写、测试实现和代码 review 必须委托 `dev-dispatch` 完成。协调会话不得编写、编辑或审查业务代码。所有 git 操作仅可发生在 `/data/worktrees/fleet-graph-e1-extension-fresh-20260828` 及其后续独立 worktree；严禁在生产主 checkout 中写码、验证、checkout、switch、reset 或 detach。本单不执行生产部署；生产主 checkout 仅允许在远端 main 合并后由监督面执行 `git pull --ff-only`。

## 验收

验收脚本必须产生 JSON evidence，含 UTC 时间戳、source message id、target/action key、每次 owner 调用计数、cursor 前后值、receipt 状态与退出码。`resume-under-5s` 从 fake bus 成功接受 event 的单调时钟起至 owner 持久成功边界小于 5 秒。`kill-restart-exactly-once` 在 `intent_recorded` 后且 owner 首次响应已持久化、bridge 写 `resumed` 前 SIGKILL；重启到收敛小于 5 秒，最终一条 receipt、cursor 单调、仅一个逻辑 resume 且无其它 target 恢复。

```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_decision_bridge.py -q
uv run python scripts/e1_decision_bridge_acceptance.py --scenario resume-under-5s --max-latency-seconds 5
uv run python scripts/e1_decision_bridge_acceptance.py --scenario kill-restart-exactly-once --kill-after intent_recorded --max-recovery-seconds 5
```