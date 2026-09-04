# Spec R0（wf-4601c8）· scripts/verify-rebuild.sh —— 验收标准 v2 二十一项的诚实报红脚本

> 判据锚：wf-4601c8 goal.md §二 R0 与 §四纪律、design.md §4「验收标准 v2」（二十一项，编号稳定）、design.md §2 直接复用（M0A verify-lim.sh 探针骨架）。目标分支 `release/wf-4601c8`；base = origin/release/wf-4601c8 @ 14c11fe6f5a5。与正本冲突以正本为准。

## 交付物（恰好两个新文件，其余零改动）

1. `scripts/verify-rebuild.sh`（新增，可执行 bash，chmod +x）
2. `tests/test_r0_verify_rebuild.py`（新增）

## 脚本行为契约（硬性）

1. **输出格式**：每项恰好一行 `NN <id> PASS|FAIL — <依据>`（分隔符是「空格+em dash U+2014+空格」，与 verify-lim.sh 的 emit 一致）；NN 为 01–21 两位数字；id 用下文表的 kebab-id；依据非空，含命令/路径/输出摘要（压缩成一行）。恰好二十一行、按 01–21 顺序输出（`--check NN` 时只输出该行）。
2. **退出码 = FAIL 项数**（0–21）。单项探针出错（curl 非零 / jq 解析失败 / 文件缺失 / 超时）按该单项 FAIL 计并带错误原文；脚本不得因单项崩溃（无全局 set -e；照 verify-lim.sh 用 `set -u` + `set -o pipefail`）；单项探针超时上限 15s。
3. **代理卫生（S6）**：脚本开头 `unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy no_proxy NO_PROXY 2>/dev/null || true`；回环 curl 加 `--noproxy '*'`。
4. **复用 M0A 骨架**：`sanitize` / `emit`（改名 `vrb_emit` 或保留 emit 均可，但见第 7 条命名约定）/ `mcp_init` / `mcp_json` / `json_get` / `needs_check` 与 `--check NN`、`--window-seconds S` 参数从 `scripts/verify-lim.sh` 复制改形（复制进本脚本、不 source 它；头部注释注明「骨架复制自 verify-lim.sh（M0A），检查项按 wf-4601c8 design.md §4 重写」）。mcp_init/mcp_json 的 clientInfo 名字用 verify-rebuild。
5. **断言对象是已部署的生产事实**（监督面 S5 裁决沿用）：默认指向部署 current（/data/apps/fleet-graph/current）、agent-bus :7490、state :7494、MCP 5608/5610/5611/5614、/data/fleet-graph/runs、/data/fleet-graph/dd。
6. **每个外部依赖都必须有 `VRB_*` 环境变量可覆盖**（覆盖只改探针指向，不改判据；这是测试造 fixture 的唯一入口，也让 R1 的 `--env test` 以后可以直接复用）：
   - `VRB_SYSTEMCTL`（默认 `systemctl`； stub 脚本须能应答 `--user list-units … --plain --no-legend` 与 `--user show <unit> -p MainPID --value`）
   - `VRB_CURRENT`（默认 `/data/apps/fleet-graph/current`）
   - `VRB_BUS_BASE`（默认 `http://127.0.0.1:7490`）、`VRB_BUS_TOKEN_FILE`（默认 `/data/agent-bus/tokens/fleet-graph.token`）
   - `VRB_STATE_BASE`（默认 `http://127.0.0.1:7494`）
   - `VRB_MCP_BUS` / `VRB_MCP_DD` / `VRB_MCP_GOAL` / `VRB_MCP_DECISION`（默认 5608 / 5610 / 5611 / 5614）
   - `VRB_RUNS_ROOT`（默认 `/data/fleet-graph/runs`）、`VRB_SCHED_DIR`（默认 `$VRB_RUNS_ROOT/.scheduler`）
   - `VRB_DD_ROOT`（默认 `/data/fleet-graph/dd`）
   - `VRB_ROSTER`（默认 `$VRB_CURRENT/config/ronin-lines.json`）
   - `VRB_SKILL_FILE`（默认 `/data/code/self/agent-skills/plugins/agent-skills/skills/fleet-supervisor/SKILL.md`）
   - `VRB_PERSONA_FILES`（冒号分隔的线 persona 文件集；默认从 `$VRB_ROSTER` 里 enabled 线的 persona/seat 路径字段取，取不到则空集并在依据里注明）
   - `VRB_LLM_LEDGER`（灵智账本查询面 URL，见 05；默认值实现时按 llm-usage-dashboard / new-api 网关 request_events 的真实查询面定，写死在脚本头并注释依据）
7. **检查函数命名约定（变异红靶的机械注入点，硬性）**：每项一个 bash 函数 `vrb_check_NN`（NN 两位数字），函数体首行不得是 `return`；判定一律经 `vrb_emit NN id VERDICT evidence` 发出（vrb_emit 语义照抄 verify-lim 的 emit）。主循环按 01–21 顺序调用（needs_check 过滤）。
8. **禁止恒 PASS**：每项判定必须来自当次真实探针读数（命令执行 / HTTP 请求 / 文件读取）。机制不存在、数据源不可达、样本为空 → 该项 FAIL，依据写明缺什么。禁止任何形式的「跳过即过」「异常吞掉当过」。
9. **禁止触碰真实对象**：一切「主动打」的检查（04/06/12/14/15/16）只允许对**现场合成的一次性靶**进行，靶命名一律 `vrb-selftest-` 前缀，跑完即清（清理失败要在依据行报告）；合成不了的（所需机制尚未落地）按第 8 条 FAIL 带依据，绝不动真线、真单、真频道。模式先例：verify-lim.sh check 12 的 PROBE_DEV_ID 现场合成靶。只读检查不得写任何生产路径。

## 二十一项与机械查法（编号与 id 稳定；「通过标准」是判据不是起点预期——起点大面积红属正常，如实报）

| NN | id | 验收项 | 怎么查（机械） | 通过标准 | 来源 |
|---|---|---|---|---|---|
| 01 | trial-instances-stopped | 试验实例已停 | `$VRB_SYSTEMCTL --user list-units 'agent-bus-*' --plain --no-legend`，取单元名集合 | 只剩 agent-bus-server、agent-bus-mcp | design §4/§8 |
| 02 | dead-protocols-deregistered | 死协议已注销 | `GET $VRB_BUS_BASE/v1/protocols`（Bearer `$VRB_BUS_TOKEN_FILE`），数 kind 前缀 `coord.` | 0 | §8 |
| 03 | decisions-zero-swallowed | 裁决零吞 | `GET $VRB_STATE_BASE/v1/decisions`，`--window-seconds`（默认 86400）窗口内 state==`swallowed` 计数（时间字段不可得时全量计数并在依据注明） | 0 | §8 |
| 04 | external-decision-wakes-line | 外部裁决送达即唤醒 | 经 decision MCP（$VRB_MCP_DECISION）对一条现场合成的 `vrb-selftest-` blocked 靶线投裁决；随后查靶线下一代 unit 出现且裁决**被消费**（非仅起 unit） | 消费证据成立（S10） | §8、S10 |
| 05 | waiting-zero-consumption | 等待零消耗 | 窗口内 `waiting_dd` 线（读 `$VRB_SCHED_DIR/*.json` 的状态字段）× `$VRB_LLM_LEDGER` 按该线 alias 的 request_events 计数 | 全部为 0；无 waiting_dd 样本或账本不可达 → FAIL 并注明缺哪样 | §8 |
| 06 | acceptance-supervisor-only | 验收标准只有监督者能改 | 执行方向：以非监督者 principal 对合成靶改 acceptance → 期望拒绝+留痕；监督者向：**只读**现有 `contract_changed` 留痕（绝不主动改真契约） | 两向都成立 | D16 |
| 07 | seats-single-source | 座位单一来源 | dd-mcp 进程 cmdline（`$VRB_SYSTEMCTL --user show fleet-graph-dd-mcp -p MainPID --value` → `/proc/<pid>/cmdline`）+ 窗口内 `$VRB_DD_ROOT` 各单 record.json.seats 来源字段 | 无 `--stage-model`；seats 来自派单请求或 role registry | §8 |
| 08 | public-interface-mcp-only | public interface 只有 MCP | `grep -rEn 'curl .*:(7490\|7494)\|fleet-graph line \|fleet-maint' $VRB_SKILL_FILE $VRB_PERSONA_FILES…` | 0 命中 | §8 |
| 09 | takeover-one-call | 接手一次调用 | 经 MCP（decision/state 面 tools/list + 只读调用；R5 后为 state_takeover）零上下文取六项：名册、线状态、等拍板、待上线、授权模式、当前 release | 六项齐（缺项显式标注不算齐） | §8 |
| 10 | mcp-function-probes | 功能探针 | 五个面（bus/dd/goal/decision/state）逐面 `tools/list` + 一个只读工具真调用（照 verify-lim check 10 的 mcp_tool_names + 调用骨架） | 全部成功 | §8 |
| 11 | gate-decided-by-dispatcher | gate 由派单线自判 | 窗口内到闸/释放单（`$VRB_DD_ROOT/*/record.json`、`result.json`）gate 记录 `decided_by` 与该单 `dispatched_by` 逐单比对 | 100% 相等（无样本 → FAIL 注明无样本） | D5、D20 |
| 12 | gate-unforgeable-outside-line | 线外无法批 gate | 以非派单身份对现场合成 `vrb-selftest-` awaiting_gate 靶单（照 verify-lim check 12 先例合成 record.json+status.json）经 MCP 与 HTTP 双路尝试释放，跑完即清 | 无此入口或稳定拒绝码；靶单状态不变 | S11、D20 |
| 13 | dd-touches-line-branch-only | DD 只碰线分支 | 窗口内单 record.json 的 `target_base_commit` 与 `remote_ref`：base 是否等于派单时 `release/<line-id>` 头、merger 推的是否 `release/<line-id>` | 全部成立 | D6 |
| 14 | rebase-before-dispatch | 派单前 rebase | `$VRB_DD_ROOT/*/events.jsonl`（或 configure 段日志）中 configure 首步 rebase 记录 + release_behind 读数（state 面或派生） | 出现 rebase 记录且 release_behind 回 0 | D6 |
| 15 | message-delivered-and-acked | 消息必达必回 | 对合成靶线发 line_message(text, instruction)（监督者工具缺失时按第 8 条 FAIL 注明），查下一代输入含该消息与 ack 台账行 | 两者都成立 | D7 |
| 16 | message-not-a-decision | 消息不能冒充裁决 | 对合成 waiting_decision 靶线只发 line_message("APPROVE")，查驻停是否解除与回执 | 驻停不解除，回执写明消息不是裁决 | D7 |
| 17 | dispatch-gate-via-stop-response | 派单与批 gate 走 Stop Response | 名册线 MCP 工具集无 dd-mcp 入口；`$VRB_RUNS_ROOT` 各线 `coord/rounds.jsonl` 派单轮 `actions` 含 `dd.dispatch.v1`、gate 释放轮含 `dd.gate_release.v1`；线内无对 dd-mcp 的工具调用记录 | 两者都成立 | D19、D20 |
| 18 | disk-not-a-channel | 磁盘不当信道 | 代码级 grep `$VRB_CURRENT/src`：调度器唤醒路径读取 `terminal.json` / `.scheduler` 传递 dd 终态的分支（读文件内容当事件的模式） | 0 | D18 |
| 19 | graph-state-rebuildable | 图状态可重建 | 测试环境里删一条 parked 线 checkpoint 库后下一 tick 从 work folder + record.json 重建（R1 前无 testenv：按可查证据判，无环境 → FAIL 注明） | 重建且不重复派发、不丢结果 | 不变量四 |
| 20 | testenv-e2e | 测试环境端到端 | `scripts/testenv.sh` 存在性 + `up` 后目标架构页 Ⅴ 全流程五步回显（R1 交付物，R0 缺 → FAIL 注明） | 五步回显齐 + 生产零变更 | D8、M9 |
| 21 | deletion-list-assertions | 删除清单存在性 | §7.1 九项 + §7.2 十三项（清单见下节）逐对象机械断言「确实没了」；输出行汇总 `§7.1 gone=x/9 §7.2 gone=y/13` + 前几个仍在对象名 | 全部确实没了 | §7 |

「怎么查」列与本表冲突时以 wf-4601c8 design.md §4 原文为准（本表是其逐项机械化细化）。

## 21 的对象清单（源自 wf-8d9737 design.md §7.1/§7.2；goal R6 按此删）

**§7.1 九项**（断言对象已消失）：
1. agent-bus 试验实例 `agent-bus-test` :7491、`agent-bus-staging` :17590、`agent-bus-autodev-test` :7493（systemctl）
2. `wf-observe.service`
3. 退役 unit 文件族 `loop-engine-*`、`loop-mcp`、`ronin-auto-gate`、`ronin-babysitter`、`ronin-pump-*`（.service 与 .d）
4. 看板频道族 `gd:e2e-gdrun-*`、`chat:testroom`、`chatgroup:livetest-*`、`coord:observability-successors-*`、`board:dd-talk-staging-*`、`board:agent-runtime-profile-schema-*`
5. 死协议族 `coord.*` v1/v2、`dd.plan.*`、`coordination.dispatch-request.v1`、`probe.reqtype.v1`、`research.smoke.v1`、`agent.run.started`/`agent.run.exited` v1 与 v2
6. ronin-mcp dev/gate 族 13 个与 pump 族 3 个死工具（tools/list）
7. dd-mcp 5 个 NOT_SUPPORTED 工具（deployment_create、deployment_status、development_control、development_relock、development_steer）
8. dd-mcp unit 残余 `--stage-model continuous_review` / `--stage-model final_review` 覆盖键（cmdline）
9. Tempo（部署存在性）

**§7.2 十三项**（断言调用面/对象已消失）：
1. `decision-bridge` + goal.md 直写信道（supervise/e7_*）
2. ronin-mcp 整个门面
3. `work.card.v1` 与 `board:work-index`
4. `dd/<dev>/status.json` 与 `/v1/lines.parked` 字段
5. `fleet-l0.py` + Monitor 唤醒路
6. `:7494` 作为调用面（skill/persona 中作为调用面出现）
7. CLI `line revive` / `set-seat` / `supervisor reset` / `fleet-maint.sh` 作为调用面
8. `/data/ronin` 整目录（41 个 alias token 迁 `/data/fleet-graph/secrets` 完成；断言只查 `/data/ronin` 不再被引用与 token 新路径存在性，**不删不改**）
9. A2 arbiter timer
10. 线的 dd 轮询（源内轮询分支）
11. goal.md 直写捎话 + `line set-seat` CLI
12. 监督面待办 T-2b / T-2c
13. 监督面逐单批闸 SOP（fleet-supervisor SKILL §0 表、§0.7、§4.5 手工收割步骤）

21 项输出仍是一行（二十一行总格式不变），依据行内给两个分数与前几个残留对象名；逐对象明细写进该行依据（可截断，截断注明 `…`）。

## 变异红靶（硬性；「把某项改成恒 PASS 时对应变异用例必须能红」）

测试文件对 01–21 **全部**项实现两组成对用例（靶子机械枚举、不由实现方自选——S12 精神）：

1. **红锚用例** `test_check_NN_reports_fail_on_bad_fixture`：用 `VRB_*` knob 造「该项必须 FAIL」的坏 fixture（死端口 / 空目录 / stub systemctl 或 HTTP stub 的坏响应 / 缺文件——按项选最自然的一种），以 fixture 环境跑 `bash scripts/verify-rebuild.sh --check NN`，断言 exit=1 且输出 `NN <id> FAIL — `。→ 若该项被写成恒 PASS，此用例红。
2. **变异元用例** `test_check_NN_mutation_to_constant_pass_is_detectable`：把脚本复制到 tmp，向 `vrb_check_NN` 函数体首行注入 `vrb_emit NN <id> PASS "mutation: forced pass"; return`（用 python/sed 注入，对 21 个函数名逐一可行——这就是第 7 条命名约定存在的理由），对同一坏 fixture 跑注入版，断言输出 `NN <id> PASS`。→ 证明「改成恒 PASS」这个变异会翻转判定、红锚用例确实抓得住它。

红锚用例一律用 knob fixture，不得依赖本机当刻生产行为。另对机械上可行的至少 6 项（01/02/03/08/10/21）加**绿侧用例**（好 fixture → `PASS` 且 exit=0），防「恒 FAIL」的反向作弊。

## 结构与元测试（同文件）

- 脚本存在、可执行位、`bash -n` 过。
- `--check 01` 只输出一行；`--check 99` 报错非零；`--window-seconds` 可传。
- 全 knob 指向空目录/死端口的 fixture：恰好 21 行、全部 FAIL、exit=21。
- 退出码恒等于 FAIL 行数（坏 fixture 与任一好 fixture 两种跑法都验）。
- 每行依据非空（`— ` 后有内容）。
- **零测试删除**：既有测试文件一个不动、一行不删。

## 边界

- 只动 fleet-graph 仓；只新增上述两个文件；Makefile、既有脚本（含 verify-lim.sh）、既有测试零改动。
- 脚本对生产只读 + `vrb-selftest-` 合成靶（跑完即清）；不部署、不重启、不删数据、不碰 /data/ronin。
- 测试全部离线自足（tmp fixture + 本地 stub），不得依赖生产端口可达。

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_r0_verify_rebuild.py'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash scripts/verify-rebuild.sh > /tmp/vrb-acceptance.out 2>&1; rc=$?; lines=$(grep -cE "^[0-9]{2} [a-z0-9-]+ (PASS|FAIL) — " /tmp/vrb-acceptance.out); fails=$(grep -cE "^[0-9]{2} [a-z0-9-]+ FAIL — " /tmp/vrb-acceptance.out); echo "verify-rebuild lines=$lines fails=$fails exit=$rc"; test "$lines" -eq 21 && test "$rc" -eq "$fails"'
```
