# Spec R3（wf-4601c8）· Stop Response 派单与批 gate —— actions[] 信封、gate 节点六项取证、第二投递路删除（D19、D20）

> 状态：**定稿**（2026-09-05 派单前重校后定稿；round7 等待期产出草稿＋v2 增补）。
> **派单前重校**（2026-09-05T02:0xZ，§5b 独立 worktree @d0b980d32e5dba9a3f033241195d559eb5b49c6f，fetch＋ls-remote 亲证当刻 origin/release/wf-4601c8 头）：dd-acceptance 三条 argv 形参与实际判据面全部受支持——`verify-rebuild.sh --check` 仅接受 01–21（11/12/17 合法），单查模式恰打一行 `NN name VERDICT — …`（em-dash 与 grep 口径吻合），全量恰 21 行 `NN [a-z0-9-]+ (PASS|FAIL) — `；testenv up/down/status/rebuild 形参受支持（up=1 surfaces=7/7、down prod_references=0）。基线活体取证（--env test，原文逐字）：11 `FAIL — 窗口（86400s）内无 dd 单可核（/tmp/r3-baseline-testenv/dd 无带 record.json 的单）`；17 `FAIL — 无 rounds.jsonl 样本（/tmp/r3-baseline-testenv/runs/*/coord/ 下 0 个），派单/gate 轮 actions 无从核`；12 当刻已绿 `PASS — 线外（vrb-selftest-foreign-*）对合成 awaiting_gate 靶单 MCP+HTTP 双路释放均拒绝/无此入口，靶单状态不变`（R2 合流后旧 development_gate/HTTP 已结构性拒绝外线释放——本单义务＝删除第二投递路后 12 维持 PASS 不回退，非由红转绿）；R2 已绿项不回退基线：05/18/19 PASS、make verify 2992 passed/1 skipped（@d0b980d 本轮亲跑）；全量当刻 21 行 9 PASS/12 FAIL、exit=12。
> **v2 增补**（2026-09-04T18:56Z，R1 等待期 round2）：新增「dispatched_by 必填」派单入口断言（行为契约 §1 末条 + 阴性用例 9），先例＝前单 dev-fg-5af16702b3c4 dispatched_by 空串教训；其余未动。
> 判据锚：goal.md §二 R3 与 §四纪律；design.md §1（R3 ↔ 宪法第一条 两类节点、第二条 判定带证据执行变异与写码分开、第六条 人在环上；L1 约束第 2、5 条；D5、D19、D20、S10、S11）、§2 改形复用（self_gate_evidence.py → gate 节点取证；M2「决定者必须是派单者」→ 节点断言；spec-m2-decision-wake 投递机制换 dd.gate_release.v1）、§2 不再往上叠第 1 行（decision_deliver dd 路径 / decision-bridge 消费 / test_m2_dd_gate_delivery.py 旧路用例）、§4 验收 v2 第 11/12/17 项；findings【六项取证的盲区】（S9/S10/S11 三条都要成为 gate 节点断言）、【⑮ 返工契约】（gate REJECT 绑 board 裁决三非空）；specs/r2-graph-unification.md「与 R3 的分界」节。与正本冲突以正本为准。

## 范围（一句话）

coordinator 的 Stop Response 增加与 verdict 正交的 `actions[]`：`dd.dispatch.v1` 由 dispatch 节点消费并 fan-out，`dd.gate_release.v1` 由 gate 节点消费；gate 节点机械履行六项取证义务（前三项引擎机械计算）；`decision_deliver` 的 dd 目标路径与 `decision-bridge` 在 dd gate 上的消费删除，MCP 门只剩外部裁决。

## 交付物

1. 引擎源码（改）：Stop Response schema 与 actions 消费（dispatch 节点 / gate 节点）、`self_gate_evidence.py` 取证逻辑改形进 gate 节点、删除 decision_deliver dd 路径与 decision-bridge 的 dd gate 消费——触点由实现方按行为契约探索定，不做文件级白名单。
2. 新增 `tests/test_r3_stop_response.py`；按 design §2 删除 `tests/test_m2_dd_gate_delivery.py` 中绑定旧投递路径的用例，**同时**新增等价绑定新路径（dd.gate_release.v1 消费）的用例，交付里附「删/补对照表」，覆盖净数不减。
3. 不碰 verify-rebuild.sh、Makefile、名册与部署。

## 行为契约（硬性）

### 1. actions[] 信封

- Stop Response 增加 `actions: []`，与 `verdict`（continue/blocked/done/failed）**正交**：verdict 判停走，actions 执行事；空数组合法。
- 每条 action＝`{kind, payload, idempotency_key}`；引擎逐条消费并回执（成功/失败+原因落当轮 rounds.jsonl）。**未识别 kind、schema 不符、重复 idempotency_key → fail-closed**：报错留痕、该 action 记失败，绝不静默吞掉（宪法第九条 失败必须现形）。
- `dd.dispatch.v1`（payload：spec 文本或卷内引用、repo、target base、stage_models、timeouts 等＝development_create 内部函数现有入参）由 **dispatch 节点**消费：调 R2 的内部函数 + 图边 fan-out，回执含 development_id 与 launches 引用。
- `dd.gate_release.v1`（payload：development_id、verdict=APPROVE|REJECT、decided_by、六项取证逐项数字与证据引用）由 **gate 节点**消费。
- rounds.jsonl 当轮记录 actions 原文与逐条消费回执——这是验收 17 项「派单轮 actions 含 dd.dispatch.v1、gate 释放轮含 dd.gate_release.v1」的数据源。
- **dispatched_by 必填（v2 增补，硬性）**：`dd.dispatch.v1` payload 必带 `dispatched_by=<line-id>`（发起该 action 的线身份，非空），dispatch 节点调 `development_create` 时逐字透传；**缺参即 fail-closed**——action 记失败留痕、不派单（与未识别 kind 同处置）。理由（先例＝前单 dev-fg-5af16702b3c4 dispatched_by 空串教训）：漏参在 record 冻结后**不可修复**（dispatched_by 进 bootstrap 冻结面，M2 断言 decided_by==dispatched_by 对空串永不成立，单据挂起无人能合规释放）；幂等重入**不回填**（同 (repo, spec, base) 幂等命中只复回旧 record 的空串，不会补值）——防线必须在派单入口，不是事后对账。

### 2. gate 节点六项取证

- 六项＝三方验收逐字一致 / 改动未越界 / 零测试删除 / 亲跑验收 / 全量回归对基线 / 变异回执核验；**前三项引擎机械计算**（三方命令归一哈希比对、diff name-status 对 spec 交付物清单、tests/ 删改探测）。
- 后三项由节点执行或核验：亲跑验收＝在 merge-candidate worktree 逐字跑 record.json acceptance_commands 抄回显；全量回归＝base/merge 双侧 make verify 对数（S9）；变异回执＝终审机械枚举执行、gate 只核回执计数一致（缺回执 fail-closed）。
- `self_gate_evidence.py` 改形进节点：`receipt_on_head` 选回执、`diff_added_lines` 机械枚举变异靶、fail-closed 纪律逐字保留；改形后原 MCP 投递前取证的旧调用点删除（同族枚举义务：全仓 grep 旧调用点为零）。
- 节点断言（findings 三盲区 + M2）：`decided_by == dispatched_by`（不等即 REJECT+留痕，M2 身份不变量）；S10 消费证据＝gate 节点回执本身（非「起了 unit」）；S11 释放 awaiting_gate 单的唯一路径＝本线图内 gate 节点消费自己的 dd.gate_release.v1。
- REJECT 走 ⑮ 返工契约：绑定 board 裁决三非空（明确的问题 / 建议答案 / 不答的代价），缺任一 → gate 拒收该 REJECT 并留痕。

### 3. 第二投递路删除（S11）

- `decision_deliver` 的 dd 目标路径删除；`decision-bridge` 在 dd gate 这条路上的消费删除；MCP 门只剩**外部裁决**（decision 面对 waiting_decision 线的投递保留——那条路不经 dd）。
- 同族枚举义务：全仓枚举「绕过 gate 节点释放/推进 dd 单」的全部出现点（HTTP 面、CLI、bridge、直写 record/result 的路径），逐项处置清单落卷；`grep` 探针为零作红靶。
- 删除后验收 12 项必须成立：以任意身份经 MCP 或 HTTP 尝试释放 awaiting_gate 单 → 无此入口或稳定拒绝码、单据状态不变。

## 阴性用例与变异红靶（成对：红锚 + 注入翻转）

1. `test_actions_unknown_kind_fails_closed`：注入未知 kind / 坏 schema / 重放 idempotency_key → 该 action 记失败留痕，不静默。变异：吞异常当成功 → 红。
2. `test_dispatch_action_drives_graph_edge`：dd.dispatch.v1 消费后子图实例化 + rounds.jsonl 回执含 development_id。变异：绕过 dispatch 节点直调内部函数不留回执 → 17 项判据单测红。
3. `test_gate_release_requires_decided_by_dispatcher`：以他线身份（decided_by≠dispatched_by）投 dd.gate_release.v1 → REJECT+留痕。变异：去掉断言 → 红。
4. `test_gate_six_obligations_mechanical_first3`：伪造三方命令不一致 / 越界文件 / 测试删行 → 前三项机械计算各自 RED。变异：把某项探测改成恒过 → 红。
5. `test_gate_missing_mutation_receipt_fail_closed`：无变异回执 → 不释放。变异：默认放行 → 红。
6. `test_no_bypass_release_path`：MCP/HTTP 双路释放合成 awaiting_gate 靶 → 稳定拒绝码、状态不变（12 项单测版）；`grep` 探针：decision_deliver dd 路径 / decision-bridge dd 消费 = 0。
7. `test_reject_contract_three_nonempty`：REJECT 缺 board 裁决任一非空项 → 拒收。
8. 元：删/补对照表用例数净不减；make verify 全绿；R2 成果不回退（18/19 项单测仍绿）。
9. `test_dispatch_requires_dispatched_by`（v2 增补）：dd.dispatch.v1 缺 dispatched_by 或空串 → action 失败留痕、development_create 未被调、零图边；带参 → record.dispatched_by 与 payload 逐字相等。变异：缺参静默派单（空串入 record 冻结面）→ 红（先例：dev-fg-5af16702b3c4 空串挂死，M2 断言永不成立）。

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_r3_stop_response.py'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash -c '\''R=/tmp/r3-accept-testenv; rm -rf "$R"; bash scripts/testenv.sh up --root "$R" >/tmp/r3-te-up.out 2>&1; uprc=$?; V="env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy"; CHK(){ bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R --check $1" | grep -cE "^$1 [a-z0-9-]+ PASS — "; }; c11=$( [ "$uprc" -eq 0 ] && CHK 11 ); c12=$( [ "$uprc" -eq 0 ] && CHK 12 ); c17=$( [ "$uprc" -eq 0 ] && CHK 17 ); c18=$( [ "$uprc" -eq 0 ] && CHK 18 ); c19=$( [ "$uprc" -eq 0 ] && CHK 19 ); lines=$( [ "$uprc" -eq 0 ] && bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R" | grep -cE "^[0-9]{2} [a-z0-9-]+ (PASS|FAIL) — " ); bash scripts/testenv.sh down --root "$R" >/tmp/r3-te-down.out 2>&1; drc=$?; refs=$(grep -oE "prod_references=[0-9]+" /tmp/r3-te-down.out | head -1); echo "up=$uprc lines=$lines c11=$c11 c12=$c12 c17=$c17 c18=$c18 c19=$c19 down=$drc $refs"; test "$uprc" -eq 0 -a "$lines" -eq 21 -a "$c11" -eq 1 -a "$c12" -eq 1 -a "$c17" -eq 1 -a "$c18" -eq 1 -a "$c19" -eq 1 -a "$drc" -eq 0 -a "$refs" = "prod_references=0"'\'''
```

（判据：21 行读数齐；**11/12/17 三项 PASS**——11 decided_by 100% 等于 dispatched_by 且记录由图内 gate 节点写出、12 线外无释放入口（基线已绿，删除第二投递路后不得回退）、17 actions 信封两半齐全（R2 后缺的半边在本单补齐）；18/19 维持 PASS（R2 成果不回退）；干净回收零生产引用。）

## 边界

- 只动 fleet-graph 仓；删除范围严格＝design §2 点名的第二投递路三件（decision_deliver dd 路径、decision-bridge dd 消费、test_m2 旧路用例）+ 本 spec 契约要求的触点；外部裁决投递路（decision 面对 waiting_decision 线）保留。
- 验收全部在 testenv 与离线单测；生产零写；不部署不翻名册。
- 11/17 的样本必须是**经图路径真实产生**的派单+gate 记录——禁手写 record.json 伪造样本骗过 11 项（合成靶只允许用于 12 项的「尝试释放被拒」侧）。

## 开放点（实现方回执强制作答）

1. testenv 内如何**确定性**产出 11/17 所需真实样本（候选：stub 座位跑微型单 / 引擎级 fixture 驱动图路径），约束：不依赖外部网关与真实模型调用、不降判据、可重复。
2. `dd.dispatch.v1` payload 与 `development_create` 内部函数入参的逐字段映射表；idempotency_key 与 dd 幂等键的关系。
3. rounds.jsonl 的 actions 原文与回执的确切 schema（字段名冻结进本单测试）。
4. 同族枚举清单全文（绕授权释放 dd 单的全部出现点及逐项处置）。
