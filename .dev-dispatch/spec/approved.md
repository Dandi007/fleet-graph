# spec-m4b — line_message ack 落档与驻停对照证据面 + verify-lim 15/16 探针对齐（定稿）

- 状态：定稿 2026-09-04T08:25Z（正本依据：verify-lim-15-16-gap-note-20260904.md；r10 复跑 15/16 双红实证：verify-lim-20260904-r10-run.md）
- base 钉死：origin/release/wf-8d9737 @ 7f20b340a69bb8e2ed29964c9abff5a54419cd09（建单前 ls-remote 复核无漂移）
- 背景：M4 机制代码在位（RunArtifacts.record_line_message_acks→line-message-acks.jsonl 台账、rounds.jsonl 镜像、fleet_state 折叠 wake_facts.line_message_acks、bare decision token 机械守卫 rejected/message_is_not_a_decision），但 (a) 从未有一次真实 instruction 投递+drain 产生记录；(b) 无『仅 inbox 不解除 waiting_decision』的驻停对照证据面；(c) verify-lim check 15 else 分支与 check 16 第三分支为硬编码 FAIL 脚手架，不读任何 ack/驻停源——两条永红。

## 交付面（三条，验收必达）

1. **ack 落档机制收口**：line_message(kind=instruction) 投递→线 drain 后，ack 行落 `<run_root>/<line>/line-message-acks.jsonl`（行形状 `{round, at, message_id, outcome, reason}`）并镜像 rounds.jsonl（line_message_acks/unacked_instructions）；`:7494 /v1/lines` 的 `wake_facts.line_message_acks`（最新在前、tail 截断）对**最近一条 inbox instruction** 可被探针机械读取；info 类消息无 ack 义务、bare decision token instruction 保持 rejected/message_is_not_a_decision 守卫（在位行为不得回退）。若现机制对探针可读性有缺口（如 wake_facts 折叠条件、台账路径发现），补齐之。
2. **驻停对照证据面**：一次收信事件的前、后两个时点，`.scheduler/wf-*.json` 的 `waiting_on`/`waiting_decision` 驻停字段与 `:7494 /v1/lines` 派生面可被机械快照对照——使『仅 inbox 消息不解除 waiting_decision』成为可断言命题（before/after 快照对+字段 diff，不变为绿）。
3. **verify-lim 15/16 探针口径对齐**：check 15 与 check 16 的占位分支改为真实探针——15 按最近 instruction 的 message_id 比对 `wake_facts.line_message_acks`/台账；16 参照 check 12 先例**现场合成靶**（探针自备：投一条 line_message→驱动一次调度 tick→tick 前后快照驻停字段→断言不变；跑完即清，无真实单/生产线被触碰）。两条从红转绿且 `--check 15`/`--check 16` 可复现双 PASS。

## 红靶（约束性条款）

- 投递一条真实 instruction（合成靶线）+ 一个调度 tick 后：ack 行入台账且 state 面可读 → check 15 PASS；
- 同一事件 tick 前后驻停快照对照**不变**（仅 inbox 不解除 waiting_decision）→ check 16 PASS；
- 阴性①：info 类不产生 ack 行；阴性②：bare "APPROVE" instruction 被守卫拒绝且不冒充裁决；
- 复现性：`--check 15`/`--check 16` 连跑两次均 PASS（自备靶自清理，两次运行互不污染）；
- 全量 `make verify` 零绿转红（基线 2838+1 起算）。

## Non-Goals 与安全

- 不改 line_message 工具面语义与 goal MCP 契约；不动 M1 waiting 词表；不触 agent-bus；冻结面维持（判据 02/08 零删除、M5 g4 不 start、S12 不 start）。
- 探针合成靶一律自清理（check 12 先例），生产名册线零触碰。

## dd-acceptance

```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_line_message_ack_evidence.py -q
bash scripts/verify-lim.sh --check 15
bash scripts/verify-lim.sh --check 16
```

测试文件组织可调，红靶条款逐条可机械验证；15/16 探针改动即交付面 3 本体。
