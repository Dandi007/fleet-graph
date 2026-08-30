# M2 事件扩容——E5/E6/E7 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在独立 worktree）。
- 归属：goal.md M2「事件扩容」。依赖 M1 传感层 read-model（:7494，main@3477dc74 已落地并常驻）。
- 类别：纯增量（扩词表 + observer 改为只消费 :7494），不改 E1–E4，不改判据。

## 交付 A：supervise/events.py 词表新增 E5/E6/E7

1. 常量：EVENT_APPROVED_UNHARVESTED="approved_unharvested"（E5）、EVENT_HEARTBEAT_STALE="heartbeat_stale"（E6）、EVENT_DECISION_SWALLOWED="decision_swallowed"（E7）；并入 EVENT_TYPES。
2. 构造器（沿用 eN- 前缀 dedup key 约定）：
   - approved_unharvested_event(development_id, head_commit, stage) -> key e5-{development_id}
   - heartbeat_stale_event(folder_id, heartbeat_age_s, round, phase) -> key e6-{folder_id}
   - decision_swallowed_event(source_message_id, reason) -> key e7-{source_message_id}
3. validate_event 不变：unknown 事件名仍 raise SupervisorEventError（负例保留）。

## 交付 B：read-model 补 E5 数据面（只读、降级不 5xx）

- 新增第三视图 GET /v1/harvestable -> {"schema_version": <str>, "developments": [{"development_id","head_commit","stage","terminal"}]}。
- 只读 pull /data/fleet-graph/dd/<dev>/status.json + record.json；「已过 gate 但产品 commit 未进默认分支」= head_commit 非空且 terminal != "complete"。坏工件降级，绝不 5xx 全链。不加写权限。

## 交付 C：observer 只消费 :7494（不扫文件）

- scheduler/supervisor_events.py 新增 read-model 消费扫描：stdlib HTTP 客户端 GET 127.0.0.1:7494 的 /v1/lines、/v1/decisions、/v1/harvestable（回环，显式绕过 HTTP(S)_PROXY）。
- 派生：
  - E6：lines 中 heartbeat_age_s 非空且 > 阈值（可配，默认 300s）、terminal is None 且 parked == False。
  - E7：decisions 中 state == "swallowed"。
  - E5：harvestable.developments。
- 复用既有 budget + attempt 计数 + thread_id 语义；fail-open（:7494 不可达=跳过，绝不挡 tick）。
- 这三事件禁止重扫 heartbeat/terminal/bus/bridge 文件（一律经 :7494）。

## 交付 D：测试

- 合成 read-model 快照（注入假 :7494 客户端/快照函数）→ 断言 E5/E6/E7 各自发射、同 key 去重、budget/attempt 沿用；validate_event 负例（unknown 事件名拒绝）保留。
- make verify 通过；E1–E4 零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只读：E5/E6/E7 只发信号，不做写/收割（M3）；decision 凭证沿用既有（仅 E1 剥凭证）。
- 一切改动走 PR，不直改 main；生产主 checkout 仅 ff-only pull。判据只有用户能改。