# Spec 缺陷⑩ 返工重派（wf-8d9737 · d10b）· turn-timeout 两轨口径与报表分桶修正

## 换号声明（§5e：本单携带的新信息）
相对已合入 release 的 d10（dev-fg-a7aeef7859ba，merge 3b50fa18，spec-d10-turn-timeout-variables.md）：**2026-09-04 04:15 监督面更正已采信**（board:dd-talk seq 802，落卷 d10-two-track-findings.md）：
1. 「≥20 轮 glm-5.3 复判」条款作废；
2. 超时拆两轨：线侧 worker_turn_timeout@3000s 预算 与 dd 侧 PROVIDER_UNAVAILABLE@9000s implement fence，两族变量矩阵不同；
3. 报表分桶键改为 seat_session_id / turn_ordinal / session_age（原 seat×model×round_index 不再是分桶键）；
4. 新增真挂 vs 长 turn 撞顶分类口径（回执时刻 − 会话最后活动时刻）。
属 spec 变更，按 goal.md §五「spec 或实现要变→换号重派」开新号；d10 已交付面（变量矩阵落档 + report 骨架 + 首批数据点）保留不重做。

## 交付（全部在 fleet-graph 仓；base = 派单时 fresh fetch 的 origin/release/wf-8d9737 头 222edea）
1. **线侧矩阵补字段**：`worker_turn_timeout` 轮落档在既有 seat/model/round_index/turn_timeout_seconds/input_bytes/output_evidence 之上必带 `seat_session_id`（座位会话 id）、`turn_ordinal`（turn 序号）、`session_age`（会话年龄，秒）；缺任一 → 用例红。
2. **报表分桶改键**：`scripts/turn-timeout-report.py` 分桶改为 seat_session_id × turn_ordinal × session_age（model 降为显示列）；旧记录缺新字段 → 「变量缺失」单列桶，不静默丢弃；无数据如实报空 exit 0（沿用既有阴性）。
3. **两轨分类口径**：线侧轨按「TURN_TIMEOUT 回执时刻 − 会话最后活动时刻」输出真挂（≈0：全程零产出）/ 长 turn 撞顶（< 预算且仍在产出）两类计数（数据源：agent-session envelope + session 目录 mtime/最后消息时间戳）；dd 侧轨独立一节，**只读**既有 dd events 的 PROVIDER_UNAVAILABLE 族（implement fence 内），按 development × re_prepare 代数 × detail 可析出的 provider 端点分桶，计数与时刻如实输出；不可析出的字段标「不可得」，严禁编造。
4. **矩阵书面落卷更新**：docs/脚本 docstring 的变量矩阵表补两轨口径与已知数据点（线侧 3000s 零产出 ≥1 例 2026-09-03；dd 侧 M5 单 e2/e3 两例 16:10:00Z/16:55:33Z，引擎 re_prepare 自愈）。

## 判据（正/负双向）
- 阳性：fixture 注入 AgentSessionTimeout 轮 → rounds 记录含全部三个新字段；report 新分桶正确；真挂/撞顶分类正确；dd 侧节对样例 events 输出正确计数。
- 阴性：抹掉 seat_session_id → 红；分桶键回退 seat×model×round_index → 红；缺字段旧记录被静默丢弃而非单列桶 → 红；空数据报非空 → 红；dd 侧编造不可得字段 → 红。

## 变异靶（预枚举，implement 逐靶杀红，final_review 只核回执）
- MUT-1 新字段采集停用（seat_session_id 恒缺）→ 冻结验收红
- MUT-2 分桶键回退旧三键 → 红
- MUT-3 缺字段旧记录静默丢弃 → 红
- MUT-4 空数据/不可得字段弄虚作假 → 红

## 边界
只动 rounds/progress 落档最小面、scripts/turn-timeout-report.py、docs/docstring、新测试 tests/test_turn_timeout_two_tracks.py；不改 agent-session/agent-runtime；不改超时预算；不碰 dd 引擎事件写入面（dd 侧只读）；零测试删除。

## 验收（dd-acceptance 围栏，逐字冻结）
```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_turn_timeout_two_tracks.py'
bash -lc 'make verify'
```