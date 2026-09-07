# Spec R2（wf-4601c8）· 图合一 —— dd_pipeline 降为 goal_line 子图，磁盘退回纯持久化（D18）

> 状态：**定稿**（2026-09-05 派单前重校后定稿；round6 草稿＋2026-09-04T23:5xZ 派单前只读复核修订）。派单前重校已做（base worktree @d547950 实测）：testenv up=1 surfaces=7/7、down prod_references=0；`verify-rebuild.sh --env test --root` 与 `--check 05|17|18|19` 形参全部受支持（单查模式恰打一行 `NN name VERDICT — …`）；当刻基线 TOTAL pass=6 fail=15，其中 05=FAIL（无 waiting_dd 样本）、17=FAIL（无 rounds.jsonl 样本，缺 actions 半边＝R3 预期红）、18=FAIL（runner.py:140 json.loads 读 terminal.json——本单靶点实证）、19=FAIL（testenv 无 rebuild 子命令）、20=FAIL（e2e 0/5，R7 范围）。base＝origin/release/wf-4601c8 头 d547950（fetch 后 ls-remote 亲证）。
> 判据锚：goal.md §二 R2 与 §四纪律；design.md §1（R2 ↔ 宪法第十三条 通信协议化对外一个入口、第八条 账随事走；L1 约束第 2 条；D18）、§3 自决「checkpoint A 方案」「R1 先于 R2」、§4 验收 v2 第 17/18/19 项、§2「不再往上叠」第 2/3 行；findings.md【同型缺陷三连】（凡删一路径先全仓枚举同族出现点）。与正本冲突以正本为准。

## 范围（一句话）

线到单是图边、单到线是子图返回值；dd 终态不再经 `terminal.json` / `.scheduler` / 看板拼接传递；图状态可从 work folder + `record.json` 完全重建，checkpointer 退为可删缓存。

## 交付物

1. `src/fleet_graph/graphs/goal_line.py`、`src/fleet_graph/graphs/dd_pipeline.py`、`src/fleet_graph/dd/control_plane.py` 等引擎源码（改）——具体触点由实现方按下列行为契约探索定，不做文件级白名单。
2. 新增测试 `tests/test_r2_graph_unification.py`（+ 按需拆分）；既有测试**零删除**（绑定旧信道的用例改写指向新信道，测试函数总数不减）。
3. `scripts/testenv.sh`（改，两处验收依赖面，非白名单外溢）：① 新增 `rebuild` 子命令＝删 checkpoint 库后从 work folder + dd record.json/result.json 重建图状态的探针（check 19 的探针面：`bash testenv.sh rebuild` 裸调、rc=0、输出含 `rebuild…ok` 或「重建」；不带 --root 时读 $FGT_ROOT——check 19/20 在 --env test 模式下如此调用）；② up 侧补 waiting_dd 判据样本：TEST_ROOT/runs/.scheduler 下落至少一个 waiting_dd 状态的 wf-*.json，且 stubs/ledger.json 的 request_events 查询可达、计数为 0（check 05 现刻因「无 waiting_dd 样本」红，见头部基线）。幂等/down/status 语义不变。
4. 不新增脚本；不碰 Makefile、verify-rebuild.sh（其 17/18/19 判据已冻结）。

## 行为契约（硬性）

### 1. 线到单是图边（Send 语义）

- coordinator 节点产出派单意图后，由**图的边**实例化 dd 流水线：每单一次子图调用、state 互相隔离（LangGraph `Send` 或仓内等价 fan-out API——判据是「边实例化 + 子图 state 隔离 + 返回值汇合」，不是 API 名字；实现方在 spec 回执里写明用的是哪个 API 及版本依据）。
- 线的 MCP 工具集**不再含 fleet-graph-dd-mcp**：线内派单路径＝图内调用 `development_create` 内部函数，全程无 MCP 往返、无对 dd-mcp 的工具调用记录。
- `development_create` 降为 dd 服务内部函数；MCP 面上的同名工具**只留给外门**：仅监督者 principal 可调（非监督者调用稳定拒绝 + 留痕，拒绝码写进回执）。
- M1 状态词表落地：`waiting_dd` 期间该线 alias 的模型请求数为 0。验收 05 项当刻在 testenv 为红（无 waiting_dd 样本，见头部基线）——本单交付须使其转绿（交付物 3.② 的样本＋零计数账本），并在实现后不回退。

### 2. 单到线是子图返回值；磁盘退回纯持久化

- dd 终态（complete/failed、result.json 摘要、output_commit、阶段链）经**子图返回值**进入线状态；调度器唤醒路径上**读取 `terminal.json` / `.scheduler/<wf>.json` 内容作为 dd 终态事件源**的分支全部删除（文件可继续作为纯持久化落盘，但不得再被读作唤醒/终态信号——「写允许、读作事件即违宪」）。
- 看板不再是终态传递路：卡片可挂，线不从看板拼终态。
- 同族枚举义务（findings【同型缺陷三连】）：交付前全仓枚举「以磁盘文件内容当 dd 终态/唤醒事件」的全部出现点（含 `dd/<dev>/status.json` 被外部读、`/v1/lines.parked` 双轨合成），逐项处置清单落卷；全仓 grep 该模式为零作红靶（与验收 18 同判据的单测版）。
- `dd/<dev>/status.json` 与 `/v1/lines.parked` 双轨状态删除：单一状态源＝`record.json`（准入权威）+ `result.json`（终态权威）+ 图状态；对外查询面改从单一源合成。

### 3. checkpoint 走 A 方案（design §3 自决）

- 图状态可从 work folder（progress/findings/INDEX 等持久件）+ dd 侧 `record.json`/`result.json` **完全重建**；checkpointer（sqlite）为可删缓存。
- 删库重建后：不重复派发（以 record.json 已派单事实 + 幂等键判重）、不丢结果（result.json 权威重放）、线继续运转（验收 19 项）。
- 重建不得引入新的磁盘事件源（不得借「checkpoint 丢了」读 .scheduler 补状态——重建输入只有 work folder 与 dd 两权威件）。

## 阴性用例与变异红靶（成对：红锚 + 注入翻转）

1. `test_no_disk_channel_in_wakeup_path`：对 src 做代码级 grep 探针（读文件内容当事件模式）＝0；变异：向调度器唤醒路径注入读 `.scheduler` 内容的分支 → 探针红。
2. `test_terminal_state_via_subgraph_return_only`：monkeypatch 写入伪造 `terminal.json` 终态 → 线不消费、状态不变；子图返回值才改变状态。变异：把消费分支改回读文件 → 用例红。
3. `test_checkpoint_rebuild_no_dup_dispatch_no_loss`：fake work folder + 已派单 record.json → 删 checkpoint 后重建，断言零重复派单、结果保留。变异：去掉幂等判重 → 用例红。
4. `test_outer_gate_mcp_rejects_non_supervisor`：以线 principal 经 MCP 调 `development_create` → 稳定拒绝 + 留痕；监督者 principal → 成功（外门仍在）。
5. `test_line_roster_excludes_dd_mcp`：线的 MCP 工具集断言无 fleet-graph-dd-mcp。
6. `test_waiting_dd_zero_llm_calls`：waiting_dd 窗口内线 alias 模型请求数＝0（假账本计 0）。
7. 元：`git diff --name-status` 内测试函数总数不减；make verify 全绿。

## 边界

- 只动 fleet-graph 仓；不删 /data/ronin、不碰生产部署与名册（引擎行为变更随 release 部署节奏走，B-1 不代做）。
- 验收全部在 R1 testenv 与离线单测里跑；对生产零写。
- 与 R3 的分界：Stop Response `actions[]` 信封（`dd.dispatch.v1` / `dd.gate_release.v1` 的 rounds.jsonl 记录）、gate 节点六项取证、decision_deliver 路删除——都归 R3，本单不做。验收 17 项在 R2 后**允许仍红**（依据行须显示缺的正是 actions 半边）。

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_r2_graph_unification.py'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash -c '\''R=/tmp/r2-accept-testenv; rm -rf "$R"; bash scripts/testenv.sh up --root "$R" >/tmp/r2-te-up.out 2>&1; uprc=$?; V="env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy"; c18=$( [ "$uprc" -eq 0 ] && bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R --check 18" | grep -cE "^18 [a-z0-9-]+ PASS — " ); c19=$( [ "$uprc" -eq 0 ] && bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R --check 19" | grep -cE "^19 [a-z0-9-]+ PASS — " ); c05=$( [ "$uprc" -eq 0 ] && bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R --check 05" | grep -cE "^05 [a-z0-9-]+ PASS — " ); lines=$( [ "$uprc" -eq 0 ] && bash -c "$V bash scripts/verify-rebuild.sh --env test --root $R" | grep -cE "^[0-9]{2} [a-z0-9-]+ (PASS|FAIL) — " ); bash scripts/testenv.sh down --root "$R" >/tmp/r2-te-down.out 2>&1; drc=$?; refs=$(grep -oE "prod_references=[0-9]+" /tmp/r2-te-down.out | head -1); echo "up=$uprc lines=$lines c05=$c05 c18=$c18 c19=$c19 down=$drc $refs"; test "$uprc" -eq 0 -a "$lines" -eq 21 -a "$c05" -eq 1 -a "$c18" -eq 1 -a "$c19" -eq 1 -a "$drc" -eq 0 -a "$refs" = "prod_references=0"'\'''
```

（判据：21 行读数齐；05/18/19 三项 PASS——18 磁盘不当信道=0、19 删库重建成立、05 等待零消耗由当刻红转绿（交付物 3.②）且实现后不回退；17 预期仍红缺 actions 半边属 R3；干净回收 + 零生产引用。cmd1/cmd2 形参与 verify-rebuild/testenv 实际 CLI 已在头部基线亲证。）

## 派单参数备忘（coordinator 用，非 spec 正文）

- 前置：R0 gate APPROVE 合流、R1 完成合流后再派本单；base＝派单当刻 `origin/release/wf-4601c8` 头；`stage_models={"implement":"glm-5.3-flash","continuous_review":"glm-5.3","final_review":"glm-5.3"}`；`timeouts={"implement":9000}`；派单前置探测六连通（goal §四）。
- 可选切分（A-6 自决）：若评估 implement 体量超栅栏，可拆「图边+内部函数化」与「磁盘信道删除+checkpoint A」两单串行——但 18 项红靶在第一单结束时不得仍依赖旧信道存活，避免中间态双轨；默认不拆。
- 开放点（实现方回执必须作答）：仓内 LangGraph 版本的 Send/等价 fan-out API 是哪个；`/v1/lines.parked` 删除后对外查询面的合成来源；同族枚举清单全文。
