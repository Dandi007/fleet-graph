# worker turn 报告协议失败不再连坐整代：有界重问一次，仍失败才 fault

## 背景（真机计数，不是推测）

全舰 line 日志（`/data/fleet-graph/logs/wf-*.log`，覆盖 2026-08-27 → 09-01）里
`worker turn report malformed` 共 **25 次**，分布在 **8 条线**：

| 线 | 次数 |
|---|---|
| wf-216dc3 | 7 |
| wf-66300e | 6 |
| wf-3f87f3 | 5 |
| wf-6475fd | 3 |
| wf-7cd0a7 / wf-c106b9 / wf-e313be / wf-fa53cb | 各 1 |

错因分布（同批日志 `sed`+`uniq -c` 现算）：

| JSON 解析错 | 次数 | 典型成因 |
|---|---|---|
| `Expecting ',' delimiter` | 14 | 字符串值里有未转义的 `"` |
| `Expecting value` | 8 | 输出为空或整段非 JSON |
| `Unterminated string starting at` | 2 | 输出被 token 上限截断 |
| `Invalid control character at` | 1 | 串内裸控制字符（`strict=False` 之前的历史例） |

**每一次的代价是一整代**：`graphs/goal_line.py:665-680` 捕获 `ReportProtocolError` 后
记 `verdict=invalid` 并让线 fault，调度器随后起新 generation，**本轮 worker 的全部工作被丢弃**，
coordinator 从头再跑。实录：wf-216dc3 g49 烧掉 1h29m 墙钟 / 16m36s CPU / 14.5G 内存峰值，
round-2 worker 产出因 `Expecting ',' delimiter: line 1 column 4422` 全部作废。

## 现状（读过代码，不是猜）

`src/fleet_graph/work_report.py` 的入口已经有两层机械规范化，方向是对的：

- `_strip_code_fence`（L95-107）：去 ```json 围栏；
- `_extract_embedded_report`（L116-129）：按 `{"schema_version"` 磁头从尾向前 `raw_decode`，
  兼容网关前置噪音；`json.JSONDecoder(strict=False)` 已放过串内裸控制字符。

但这两层都救不了**串内未转义引号**与**截断**：磁头找得到，`raw_decode` 在同一个坏字符上照样炸，
`_extract_embedded_report` 返回 None → `malformed`。这解释了占比最大的那 14 次。

`goal_line.py:665-670` 的注释写明了当前选择是**故意的**：
「fault the line rather than asking the coordinator to weigh an unvalidated report —
no extra coordinator round and no account charge from the retry」。
这条原则的前半句（绝不把未校验报告当成功/blocked）**必须保留**；
但后半句用「省一次重问」换来的是「丢一整代」，实测代价反了。

## 目标

把「报告不合协议」从**连坐整代**降级为**有界重问一次**，且不放宽任何语义校验。

## 交付物

### D1 —— worker turn 报告的有界重问

- `graphs/goal_line.py` 的 worker turn 在捕获 `ReportProtocolError` 时，
  **同一轮内重问该 worker 一次**（不换 generation、不进 coordinator、不记 round）：
  重问 prompt = 原 prompt + 一段机械追加，内容只含协议事实，**不得含任何题目相关提示或对结论的引导**：
  上次输出未通过 v1 协议、`exc.kind` 与 `exc.detail` 原文、以及「只重发报告本体，
  裸 JSON，不要散文、不要围栏」。
- 重问上限 **1 次**（常量可配，默认 1）。重问仍失败 → 维持现有行为：记
  `verdict=invalid` / `reason=WORKER_REPORT_PROTOCOL_FAILURE` / 线 fault，
  `detail` 里带上「重问 N 次后仍失败」与两次的 `exc.detail`。
- 重问次数与两次错因落 round 记录，可被观测面统计（见 D3）。

### D2 —— 截断类的显式识别

- `work_report.py` 对 `Unterminated string` 这一族给出独立的 `exc.kind`（如 `truncated`），
  与 `malformed` 区分。理由：截断是**输出预算问题**，重问一次通常能过；
  未转义引号是**格式问题**，重问也常能过；但两者的运维含义不同，观测面要能分开。
- 不新增任何「修复」逻辑：**不许**猜着补引号、补括号、截断续写。
  本单只做「重问」与「分类」，`去壳不碰语义` 的既有原则一个字不放宽。

### D3 —— 观测面补账（可观测性宪法：这条以前看不见）

- 线级 metrics 增两个计数器：worker 报告协议失败次数、其中经重问挽回的次数，
  按线与 `exc.kind` 打标。这次全舰 25 次是我 grep 日志才发现的——
  **告警面一条都没响过，属监控缺口，本单一并补上。**

## 边界（硬线）

- **绝不把未校验的报告当成功/blocked/空成功**——`goal_line.py:665-670` 注释的前半句是本单的不可动前提。
- 不许猜测性修复 JSON（补引号/补括号/续写截断），不许放宽 `_nonempty_bounded` / `_outcome` / `_did` 等 schema 校验。
- 重问追加文本**不得含任何题目相关内容或对结论的引导**，只讲协议事实——否则等于替 worker 写结论。
- 不动 `converge()` / 调度器的 generation 语义；重问发生在同一 generation、同一 round 内。
- 既有测试一行不许删。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run pytest -q tests/test_work_report.py tests/test_work_report_conformance.py tests/test_goal_line.py
```

阴性必须在位（否则本单不算达标）：
1. 构造串内未转义引号的报告 → 第一次解析失败、重问后返回合法报告 → 该轮**不 fault**、`verdict != invalid`；
2. 构造两次都坏的报告 → 仍 fault，`reason=WORKER_REPORT_PROTOCOL_FAILURE`，detail 含两次错因；
3. 构造截断报告 → `exc.kind` 为截断类而非泛 `malformed`；
4. 回归：合法报告一次通过，**零重问**（证明没有把重问变成常态开销）。
