# Spec ⑮-b（wf-8d9737）· gate-reject 来源绑定 board 裁决（返工链权威输入补全）

> 状态：落卷即派（board 2813 msg_01M1N18S6HXHZ2PSV1V1C6T7EZ「⑮-b 请立刻建单」）。
> base 钉死：**origin/release/wf-8d9737 @ 7f20b340a69bb8e2ed29964c9abff5a54419cd09**（=当前部署执行位 releases/20260904-054200-7f20b340a69b）。
> ⚠️ 与协调面指令的偏差声明：指令原文写 target_base=origin/main，但实测 origin/main（d679cf58）**不含 ⑮**（`git grep -c _seal_gate_rework origin/main` 零命中）——返工契约只存在于 release/wf-8d9737。base 若取 main，则本题对象代码（`_seal_gate_rework`/gate-reject.json/gate_rework_dispatch）在基线上不存在，三条交付面无从谈起且红靶无「旧代码」可证。故 base 取 release 头（线内 D6 惯例，同 M4/M5 派单口径），偏差与依据留档。
> 判据锚：board 2813 第二条（三交付面原文）、board 2811（⑮ 半生效取证）、board 2815（g3 驳回，空绑定活例）、卷内 spec-engine-rework-contract.md（⑮ 本体）、tests/test_rework_contract.py（⑮ 既有测试，零删除）。
> 基线活例（旧代码红证，实现者引用）：dev-fg-eee4da1e3649 g3 events 两条 `gate_rework_dispatch` 均 `decision_message_id:""`（2026-09-04T01:24:46Z 派发时；01:58:08Z 驳回+recover 后再派发时——**后者时点 board 2815 已存在 13 分钟**，引擎仍空绑定）。

## 要交付的行为（全部在 fleet-graph 仓引擎侧）

1. **来源改绑（2813 原文①）**：gate REJECT 后为返工代写的 `g<N>/gate-reject.json`，其内容必须来自该单闸口实际消费的 board `work.decision.v1`（human_gate 回执已含 `decision_message_id`，见 dev-fg-a79039ed08fe rev6 先例），不得再取 terminal-facts 一行字：
   - `decision_message_id` / `decided_by` / `rationale` **三者非空**；`rationale` 为裁决消息 rationale **全文逐字**；
   - `source` 字段如实标注来源（board work.decision.v1），不再出现 `terminal-facts` 兜底作为成功路径。
2. **全文入 prompt（2813 原文②）**：返工代 implement 提示词的 `## gate-reject-rationale:` 锚下机械携带该 rationale **全文**（非摘要、非终态行），且锚下出现 `decision_message_id`；board 裁决里写明的返工面关键词必须能在锚下 grep 命中（全文逐字的必然结果）。
3. **空绑定拒绝派发（2813 原文③，红靶）**：构造一次 REJECT 且 `decision_message_id` 为空（绑定不可得）时，`gate_rework_dispatch` **必须拒绝派发**（结构化拒绝码，如 `REWORK_DECISION_UNBOUND` 族），失败要响、可观测；**空绑定不得静默通过**——不允许出现 g3 型「派了人、任务书为空」的中间态。

## 测试与验收

- 新增 `tests/test_15b_gate_reject_source_binding.py`，至少覆盖：
  - 阳性：绑定三非空 + rationale 全文逐字（含返工面关键词）落 gate-reject.json；
  - 阳性：prompt 锚下含 rationale 全文与 decision_message_id；
  - 红靶：decision_message_id 为空 → gate_rework_dispatch 拒绝（断言结构化拒绝码与「未派发」），空绑定静默通过即为红；
  - 反向：绑定成功路径不因「有绑定」而拒绝（防误拒对抗用例）。
- **红靶在旧代码可证红**：在 base 7f20b340a69b 上运行本文件红靶用例必须 FAILED（旧代码空绑定静默通过，g3 活例为证）——实现者在交付证据中给出旧代码红/新代码绿的双跑回显。
- 既有 `tests/test_rework_contract.py` 断言随语义改写更新不算删除；**零测试删除**。
- 边界：只动 fleet-graph 仓引擎侧（`src/fleet_graph/dd/` 控制面、prompt 构造、`graphs/dd_runner` 派发面及其测试）；不动 agent-bus；不 start/reconfigure M5（dev-fg-eee4da1e3649）与 S12（dev-fg-79d528db4375）；部署归监督面。

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_15b_gate_reject_source_binding.py'
bash -lc 'make verify'
```

> 座位（D8）：implement=glm-5.3-flash，continuous_review=final_review=glm-5.3，经 development_create stage_models 传入。
