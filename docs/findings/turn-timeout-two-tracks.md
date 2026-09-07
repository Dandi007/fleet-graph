# turn-timeout 两轨口径与报表分桶（wf-8d9737 · d10b 返工落卷）

2026-09-04 04:15 监督面更正（board:dd-talk seq 802）采信后的书面卷宗，与
`scripts/turn-timeout-report.py` docstring、`fleet_graph.graphs.goal_line`
的 `TIMEOUT_MATRIX_FIELDS` / `classify_turn_timeout` 同源。d10 已交付面
（变量矩阵落档 + report 骨架 + 首批数据点）保留不重做；本卷只补两轨口径。

## 为什么换号

超时原按单轨处理；监督面更正指出这是两族变量矩阵不同的现象：

1. 线侧轨：`worker_turn_timeout`@3000s 预算（goal line 的 worker turn）；
2. dd 侧轨：`PROVIDER_UNAVAILABLE`@9000s implement fence（dd 引擎的
   implement stage run 超时/失败）。

「≥20 轮 glm-5.3 复判」条款作废；报表分桶键改为
`seat_session_id × turn_ordinal × session_age`（原 seat×model×round_index
不再是分桶键）。

## 线侧变量矩阵（超时轮 record 必带，缺任一 → 用例红）

| 字段 | 含义 | 角色 |
| --- | --- | --- |
| `seat` | agents.yaml 座位名 | 显示列 |
| `model` | 会话元数据的 model，未落如实 null | 显示列 |
| `round_index` | 轮序号（round_no） | 非分桶键 |
| `turn_timeout_seconds` | 本轮超时预算（只记录，绝不调整） | 分类输入 |
| `seat_session_id` | 座位会话 id（agent-session 侧） | 分桶键 |
| `turn_ordinal` | turn 序号（本进程对会话的逐 turn 计数） | 分桶键 |
| `session_age` | 会话年龄（秒；runtime 有 start 时间戳从之，否则本进程首次 open 观察起点） | 分桶键 |
| `input_bytes` | 实际注入 prompt 字节数 | 矩阵 |
| `output_evidence` | `{stdout_lines, last_output_at, zero_output, source}` | 分类输入 |

分类随 record 另落三个事实字段：`receipt_at`（回执时刻）、
`session_last_activity_at`（session 目录最新 mtime）、`timeout_class`。

## 真挂 / 长 turn 撞顶分类口径（线侧轨）

```
delta = receipt_at − session_last_activity_at
真挂   true_hang     delta ≈ 0（±5s 容差：回执之外会话再无可观察活动）
                     或 output_evidence.zero_output（全程零产出）
撞顶   ceiling_hit   仍在产出（zero_output 为假）且 delta < 预算
不可得 None          两类都判定不了，报表计 unclassified，绝不硬塞
```

## dd 侧轨（独立一节，只读）

`turn-timeout-report.py --dd-events …` 只读既有 dd events（events.jsonl）的
`PROVIDER_UNAVAILABLE` 族（implement fence 内），按
development × re_prepare 代数 × detail 可析出的 provider 端点分桶；
fence 外的族事件单计 `out_of_fence`；不可析出的字段如实标「不可得」，
严禁编造。dd 引擎事件写入面零改动。

## 已知数据点（首批，书面卷宗）

| 轨 | 对象 | 时刻/预算 | 产出信号 | 观察 |
| --- | --- | --- | --- | --- |
| 线侧 | flash 座位（模型未录） | 2026-09-03，3000s | 零产出 | ≥1 例 3000s 零产出超时（首轮亲历） |
| 线侧 | glm5.3 / glm-5.3 | 2026-09-03 01:5x（监督面） | —— | 切 glm-5.3 后 round2/3 零超时 |
| dd 侧 | M5 单 e2/e3 | 16:10:00Z / 16:55:33Z | —— | `PROVIDER_UNAVAILABLE` 两例，引擎 re_prepare 自愈 |

## 边界

只动 rounds/progress 落档最小面（goal_line 超时路径 + adapter 变量采集）、
`scripts/turn-timeout-report.py`、本卷与脚本 docstring、新测试
`tests/test_turn_timeout_two_tracks.py`；不改 agent-session/agent-runtime；
不改超时预算；不碰 dd 引擎事件写入面（dd 侧只读）；零测试删除。
