# Spec X-4（wf-4601c8）· 失败分类诚实化 —— agent-run 合约违约不再误贴 PROVIDER_UNAVAILABLE + 审计 receipt 链「同回执双落款」线性误报

> 状态：**定稿**（2026-09-05 R7 轮；goal §七 X-4 立案 + R4 单 dev-fg-d9370430e0ce gate 裁决 msg_01M1R8216FCWNA54A9VEAN4C3G rationale 承诺另立单修复）。
> 判据锚：goal.md §七 X-4 行（缺陷、取证、建议原文）；宪法第九条 失败必须现形（分类错误=失败面目失真：分类表把它当传输层重试、运营面按「网关不可用」误判、该次 spend 落 unknown）；wf-8d9737 findings【同型缺陷三连】（修一处先全仓枚举同族出现点）；dd/egress.py 既有分层语义（ROOT_CAUSE_TRANSPORT/EXECUTION/BUSINESS 与 ROOT_CAUSE_DISPOSITION，本单不改判据只改错贴）。与正本冲突以正本为准。

## 范围（一句话）

两处独立的「失败/链条被误述」修复：①dd_actors 把「agent-run 正常结束但无结构化输出」的合约违约从 PROVIDER_UNAVAILABLE/transport 改判为 INVALID_HANDOFF_SCHEMA/execution，并让该路径的 spend 归属执行生命周期而非 unknown；②监督面审计 receipt_chain_linked 的线性规则为「同一回执 failed/success 双落款」的重跑路径增加与 rework 边同族的 retry 边，消除 X-4 路径上的误报红项。

## 取证基线（两处缺陷的逐字事实，实现方不得重新定义问题）

### 缺陷 1（X-4 误分类）

- 代码位：`src/fleet_graph/graphs/dd_actors.py:377-383`——`if status.result is None or not status.ok:` 一律 `failure_code=PROVIDER_UNAVAILABLE, detail=f"{stage.id} run {run_id} ended {status.state}"`。
- 实例（goal §七 X-4 原文 + 本轮亲取 result.json 逐字）：dev-fg-d9370430e0ce implement attempt 2，agent-run run_id `16dbffe9-ed06-57c7-b559-2a835eaa2e89`，`/data/fleet-graph/dd/dev-fg-d9370430e0ce/agent-runs/16dbffe9-ed06-57c7-b559-2a835eaa2e89/2026-09-05-06-20-59-166-633f0d/result.json` 关键字段逐字：`{"state": "failed", "exit_code": 97, "contract_error": "no structured output found in stdout", "agent_error": false, "agent_error_subtype": null, "route_attempts": [{"route": "glm-5.3-flash@opencode/gw", "error_class": null, "signal": null, "http_status": null, "exit_code": 0, "duration_s": 763.387}]}`。模型 12.7 min 内 52 条消息正常 finish，路由尝试无 http_status/signal/error_class、每次尝试 exit_code=0——**零传输证据**；网关 :15722 同时健康。事件却记 `failure_code=PROVIDER_UNAVAILABLE, root_cause=transport`（e5），被分类表当传输层有界重试，spend 落 `unknown`。
- 既有正确先例（同函数内）：`parse_envelope` 抛 CoordinatorFault → `INVALID_HANDOFF_SCHEMA`（dd_actors.py:385-396）。本缺陷是同一语义（合约违约）发生在 agent-run 自身的结构化输出合约上，却走了 PROVIDER_UNAVAILABLE。

### 缺陷 2（审计链线性规则误报）

- 代码位：`src/fleet_graph/supervise/audit.py:242-288` `_check_receipt_chain`——`acceptable = {expected}`；仅 `is_rework_link(previous.verdict)`（REJECT，dd/chain_rules.py:38-46）时追加 `rework_link_parent(previous.receipt)`。
- 实例（dev-fg-d9370430e0ce evidence receipt_chain 逐字）：rev4 `{stage: implement, attempt: 2, verdict: "failed", input_commit: 62cadf3, output_commit: 62cadf3, receipt_digest: sha256:0970a521…, parent_handoff_receipt_digest: sha256:e07fa440…}` → rev5 `{stage: implement, attempt: 2, verdict: "success", input_commit: 62cadf3, output_commit: 34795fa, receipt_digest: sha256:0970a521…（与 rev4 同 digest）, parent_handoff_receipt_digest: sha256:e07fa440…（与 rev4 同 parent，即 re-prepare handoff）}` → rev6 `{stage: continuous_review, parent_handoff_receipt_digest: sha256:0970a521…}`。规则在 rev5 报 `parent e07fa440… != expected 0970a521…`——但 rev4/rev5 是**同一 attempt-2 回执在失败/成功两次落款**（re-prepare 后重跑，attempt_id 与 receipt_digest 均相同），rev6.parent=rev5.receipt_digest 链在下一修订即复联。成因即缺陷 1 的 e5 误分类重跑路径，属规则未建模的合法 retry 边，非链条断裂。

## 交付物

1. 引擎源码（改）：
   - `graphs/dd_actors.py` 失败分类：`status.result is None or not status.ok` 时按 agent-run result 的证据分流（细则见行为契约 1），detail 必须携带原始 `contract_error`/`agent_error_subtype`/路由证据字段原文（失败必须现形）。
   - `supervise/audit.py` + `dd/chain_rules.py`：新增 retry 边判定（细则见行为契约 2），与既有 rework 边同一模式（规则函数放 chain_rules.py，audit 消费；dd_replay 若走同一链行走器必须同步消费同一规则——先全仓枚举 `parent_handoff_receipt_digest` 与 receipt 链行走器的全部出现点，交付物附枚举清单与逐项处置）。
2. 新增 `tests/test_x4_fault_classification.py`（分类分流 + spend 归属）；`tests/test_supervise_audit.py` 增补 retry 边用例（fixture 用上述 rev4/rev5/rev6 逐字形状）。既有测试零删除；测试函数总数不减。

## 行为契约（硬性）

### 1. 失败分类按证据分流（X-4）

`status.result` 存在且 `not status.ok`（agent-run 终态 failed）时：

- **传输证据**（`route_attempts` 任一尝试含非空 `http_status`、非空 `signal`、非空 `error_class`，或该尝试 `exit_code != 0`；或 `state == "lost"` / `status.result is None`）→ 维持 `PROVIDER_UNAVAILABLE`（root_cause transport，走既有 backoff 重试）。
- **合约违约**（`contract_error` 非空，且无上述传输证据）→ `INVALID_HANDOFF_SCHEMA`，detail 含 `contract_error` 原文与 run_id（root_cause execution，经 R1-c reconfigure 通道处置，不再吃传输层有界重试）。
- **agent 侧失败**（`agent_error == true`，无传输证据）→ 非传输类失败码（沿用/新增 execution 族命名，回执必须携带 `agent_error_subtype` 原文；不得回退 PROVIDER_UNAVAILABLE）。
- 分类逻辑收敛为可单测的纯函数（输入 result.json 形状的 dict，输出 failure_code+detail 构造素材），`dd_actors` 与任何同族调用点共用同一函数（【同型缺陷三连】纪律：交付物附全仓枚举 `PROVIDER_UNAVAILABLE` 出现点清单与逐项处置）。
- **spend 归属**：合约违约与 agent 侧失败路径的 run 已完整跑完（route_attempts 有时长与消耗），spend 记入该 stage 的失败生命周期（带失败分类元数据），不再落 `unknown`；传输中断路径维持 `unknown` 现状。

### 2. 审计链 retry 边（同族于既有 rework 边）

- `chain_rules.py` 新增 `is_retry_link(previous_verdict)`（previous.verdict 为失败态，如 `failed`）与 `retry_link_parent(previous)`（= previous 的 `parent_handoff_receipt_digest`，即 re-prepare handoff digest）。
- `audit._check_receipt_chain` 在 `acceptable` 追加该 parent 当且仅当**四条件同时成立**：previous 与 record 同 `stage`、同 `attempt`、同 `receipt_digest`、record.verdict 为 success 而 previous.verdict 为失败态（「同一回执双落款」的机械判定，不开「任意 failed 后随便认 parent」的口子）。
- rev4→rev5→rev6 逐字 fixture 必须绿；篡改任一条件（不同 receipt_digest / 不同 attempt / verdict 组合不符）必须红。
- `input_commit != previous.output_commit` 的连续性检查不变（rev5.input=rev4.output 已成立）。

### 3. 不碰的东西

- 不改 `dd/egress.py` 的 ROOT_CAUSE 判据与 ROOT_CAUSE_DISPOSITION；不改 verify-rebuild.sh 任何判据；不碰名册、Makefile、testenv 判据面；不删任何既有测试。

## 阴性用例（成对红锚+注入翻转）

1. **误分类回归靶**：以缺陷 1 的 result.json 逐字形状为 fixture，断言 failure_code==INVALID_HANDOFF_SCHEMA 且 detail 含 contract_error 原文；变异（把分流删掉回到一律 PROVIDER_UNAVAILABLE）→ 红。
2. **传输仍传输**：http_status=502 / signal=9 / error_class 非空 / 尝试 exit_code=3 四个 fixture 各断言 PROVIDER_UNAVAILABLE；变异（把传输也改判 execution）→ 红。
3. **lost/无 result**：RunStatus("lost") 与 result is None 仍 PROVIDER_UNAVAILABLE；变异 → 红。
4. **审计 retry 边**：rev4/rev5/rev6 逐字链绿；三个篡改靶（换 receipt_digest / 换 attempt / previous 非 failed）各红；变异（去掉四条件限定任意 failed 边都认）→ 红。
5. **spend 归属**：合约违约路径不产生 unknown spend 记录、产生带分类元数据的失败生命周期记录；变异 → 红。
6. 元：测试函数总数不减；make verify 全绿。

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_x4_fault_classification.py tests/test_supervise_audit.py'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash -c '\''R=/tmp/x4-accept-testenv; rm -rf "$R"; bash scripts/testenv.sh up --root "$R" >/tmp/x4-te-up.out 2>&1; uprc=$?; V="env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy"; CHK(){ bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R --check $1" | grep -cE "^$1 [a-z0-9-]+ PASS — "; }; c11=$( [ "$uprc" -eq 0 ] && CHK 11 ); c13=$( [ "$uprc" -eq 0 ] && CHK 13 ); c14=$( [ "$uprc" -eq 0 ] && CHK 14 ); c17=$( [ "$uprc" -eq 0 ] && CHK 17 ); c18=$( [ "$uprc" -eq 0 ] && CHK 18 ); c19=$( [ "$uprc" -eq 0 ] && CHK 19 ); lines=$( [ "$uprc" -eq 0 ] && bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R" | grep -cE "^[0-9]{2} [a-z0-9-]+ (PASS|FAIL) — " ); bash scripts/testenv.sh down --root "$R" >/tmp/x4-te-down.out 2>&1; drc=$?; refs=$(grep -oE "prod_references=[0-9]+" /tmp/x4-te-down.out | head -1); echo "up=$uprc lines=$lines c11=$c11 c13=$c13 c14=$c14 c17=$c17 c18=$c18 c19=$c19 down=$drc $refs"; test "$uprc" -eq 0 -a "$lines" -eq 21 -a "$c11" -eq 1 -a "$c13" -eq 1 -a "$c14" -eq 1 -a "$c17" -eq 1 -a "$c18" -eq 1 -a "$c19" -eq 1 -a "$drc" -eq 0 -a "$refs" = "prod_references=0"'\'''
```

（判据：cmd1 两测试文件全绿；cmd2 make verify EXIT=0；cmd3 testenv 回归——11/13/14/17/18/19 维持 PASS（R3/R4 成果零回退）、21 行读数齐、干净回收零生产引用。基线当刻为 14 PASS/7 FAIL，红项属 R5/R6 未派单区，本单不要求也不得改判它们。）

## 边界

- 只动 fleet-graph 仓引擎与测试；不部署、不碰生产名册与 /data/ronin；验收全部在离线单测与 testenv。
- 本单不做 R5（外门收敛）与 R6（仓内删除）的任何判据面变更；verify-rebuild 判据零改动（B-4）。
- 生产侧既有事件（含 dev-fg-d9370430e0ce e5 的历史误记）不回写不追溯——修的是分类器与规则的前向行为。

## 开放点（实现方回执强制作答）

1. `agent_error==true` 的 failure_code 具体命名（沿用现有 execution 族还是新增）：回执必须给出全仓 failure_code 词表对照与选择理由。
2. `dd_replay`/`self_gate_evidence`/b3 链等 receipt 链行走器全仓枚举结果与逐项处置（是否消费同一 retry 边规则）。
3. spend 归属面：`_record_unknown_spend` 与 `_record_cost_obs` 的收敛方式（复用还是新入口），保证幂等不双记。
