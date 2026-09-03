# Spec 引擎缺陷修复（wf-8d9737）· gate-REJECT rationale 必须成为返工代权威输入 + 变异靶枚举左移

## 新信息（两案独立同形定证）
1. **S12 重放单 dev-fg-79d528db4375 g2（re-seal 重放）**：REJECT（msg_01M1M8VKWB03SJ9JKBJJSMRNVW）后 development_start 报 started=true/generation=2，但 stages/ 无 implement-g2 提示词、agent-runs/ 仅 g1 三个目录、events.jsonl 末条仍是 g1 human_gate@18:35:24Z，g1head(e162cd61)→g2head(455affbd) diff 仅 .dd-evidence/acceptance.json 一行——下一代「重放封存回执链」而未派发任何 implementer，闸裁决返工面从未到达代码编写段（裁决记录 msg_01M1MA0HNGC91K1ZHSXSZC4TRN）。
2. **M5 dev-fg-eee4da1e3649 g2（派发未对准）**：REJECT（msg_01M1M82BRPNC09NASNHJJKJ1Z2）后 g2 真实派发了 implementer（新 agent-run 在场），但增量仅为 deploy :5615 单元接线断言 +13 行（内部 rf 反馈项），闸裁决指定的返工面（dd_runner LineRebase 接线覆盖用例）未执行，MUT-1 删发射依旧零红（裁决记录 msg_01M1MA9RH9X34KQT1GW214Y4GJ）。
两案共同根因：**引擎未把闸 REJECT 的 rationale 作为返工代的权威、必选输入**——要么整段跳过派发（re-seal 重放），要么派发了但 prompt 不含闸裁决要点，implementer 只看见内部 review 反馈。

## 交付（全部在 fleet-graph 仓，base=e3320bd）
1. **返工契约 A——rationale 注入可证**：凡单据以 GATE_REJECTED（human_gate REJECT）终态后 development_start 起的下一代（generation N+1），其 implement 阶段提示词（stages/implement-g{N+1}-a?-prompt.md）**必须机械携带该 REJECT 裁决的要点**：裁决消息 id（refs 锚）、decision=REJECT、rationale 全文或其返工最小面段。实现面：control_plane 的 gate decision 记录（gate_decision_path，L138 一带）在 start 起下一代时被读取并注入 implement prompt 组装处（引擎侧 prompt builder）；提示词内以可 grep 的锚标记（如 `gate-reject-rationale:` 段头+消息 id）落盘，验收可机械断言。
2. **返工契约 B——re-seal 重放识别与拒绝**：development_start 起的「新代」若引擎无法为其组装出新的 implement 派发（如全部阶段命中封存回执且无新 agent-run），必须**拒绝启动并报结构化码**（建议 `REWORK_REPLAY_REFUSED`，附缺什么：无新提示词/无新派发），不得静默产出一代「假新代」（现 S12 g2 形态：回执链重放+只换 acceptance.json）。真返工的最小在证：新 implement 提示词落盘 + 新 agent-run 目录产生。
3. **变异靶枚举左移（本单起落实）**：spec 预枚举 MUT 靶清单（见下），implement 产出的测试必须对预枚举靶**逐个杀红**（一次性副本内删靶→对应用例红）；final_review 不再首次暴露靶存活——fr 只核验 spec 预枚举靶全红，不自行首次枚举。

## 预枚举 MUT 靶清单（验收即用）
- **MUT-R1（rationale 注入缺失）**：删/绕过「gate REJECT rationale → 下一代 implement prompt」的注入路径（如注入点置空）→ 断言「REJECT 后 start 的下一代 prompt 含 gate-reject-rationale: 锚与裁决消息 id」的用例必须红。
- **MUT-R2（重放拒绝缺失）**：拆掉 re-seal 重放识别（恒放行）→ 构造「无新提示词+无新 agent-run」的假新代启动 → 必须被拒的用例反转而红（即拆掉后该用例能抓到放行）。

## 判据（正/负双向）
- 阳性：REJECT 后 start → stages/ 出现新代 implement 提示词且含 rationale 锚与裁决消息 id；launches/agent-runs 出现新派发；单据 events 记录新代真实段。
- 阴性（MUT-R1/R2 如上）；另：非 REJECT 终态（complete/fabrication）不适用该路径（不得误注入或误拒）。
- 引擎既有返工路径（cr/fr REJECT 内循环）行为不回归。

## 边界
- 只动引擎侧（control_plane/start 与代启动、prompt builder、必要的事件/状态留痕）+ 新测试（建议 tests/test_rework_contract.py）；不动 verify-lim；不做 D20 拓扑；不改 reconfigure 语义。零删除既有测试。

## 验收（dd-acceptance 围栏，逐字冻结）
```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_rework_contract.py'
bash -lc 'make verify'
```