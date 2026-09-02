# Spec M0-a（wf-8d9737）：新增 `scripts/verify-lim.sh` —— design.md §8 的 16 项验收判据脚本

## 背景

wf-8d9737（舰队 less-is-more 重构线）的北极星是本仓 design.md §8 的 16 项验收全绿。本单交付全线的唯一判据脚本：可执行的 `scripts/verify-lim.sh`，逐条机械实现 16 项检查，每项独立输出 PASS/FAIL 与依据，整体 exit code = FAIL 项数。

**首轮跑出来大面积红是正常起点，不是缺陷。** 大部分项对应的机制（waiting_dd、裁决即唤醒、state_takeover、line_message、release 分支模型……）要到 M1–M8 才落地。本单交付的是「诚实报红」的能力，不是全绿。任何把未上线机制折算成 PASS/SKIP 的实现都是错的。

## 硬性要求

1. 新增 `scripts/verify-lim.sh`，git mode 100755，`bash scripts/verify-lim.sh` 幂等可重复。默认只读生产状态；唯一允许的主动动作是 check 12 对一个**不存在的合成 id** 的探针投递（见下），不得触碰任何真实 dd 单、线、频道或数据。
2. **断言对象是已部署的生产事实，不是脚本自己所在工作树的源码**（监督面 S5 裁决，2026-09-03）：systemd user unit 与 /proc/<pid>/cmdline、agent-bus :7490（Bearer token 用 `/data/agent-bus/tokens/fleet-graph.token`）、state :7494、goal MCP :5611、dd MCP :5610、decision MCP :5614、bus MCP :5608、`/data/fleet-graph/runs/`（heartbeat.json、terminal.json、.scheduler/）、`/data/fleet-graph/dd/`（record.json、result.json、events.jsonl、launches.jsonl、dd.log）、名册 `/data/apps/fleet-graph/current/config/ronin-lines.json`。把这条写进脚本头部注释。
3. 代理卫生（S6）：脚本开头 `unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy`（或对每个回环 curl/jq 探测显式 `env -u …` 包裹），回环调用不得走 SOCKS。
4. 16 项检查，顺序、id、判定如下。每项输出**恰好一行** stdout：`NN <id> PASS|FAIL — <依据>`；FAIL 的依据必须含当次观测到的原始事实（计数、argv 片段、状态清单、错误原文），PASS 的依据必须含通过的标准证据。判定只能来自当次观测：**禁止硬编码 PASS；机制未上线的项必须 FAIL（依据写明缺什么），不得 SKIP、不得折算成 PASS**。
5. 整体 exit code = FAIL 项数（0–16）。单项探针出错（curl 非零、jq 解析失败、文件缺失）→ 该项 FAIL 并把错误原文作为依据，**脚本本身不得崩溃**（不要 set -e 全局终止；每项独立容错）。单项探针超时上限 15s，整脚本目标 < 3 分钟。
6. 支持 `--check <nn>`（只跑指定项）与 `--window-seconds <n>`（check 11/13/14 的时间窗，默认 86400）。结尾打印一行汇总 `TOTAL pass=<n> fail=<m>`（不得以两位数字开头，避免污染判定行计数）。
7. 风格照本仓 `scripts/verify-mcp-only.sh`（wf-525fd4）的头部注释惯例（职责、用法、退出码逐条写清）与 `set -u`、`set -o pipefail`；冲突时以本 spec 为准。

## 16 项检查定义

判定基准 = design.md §8 各行「通过标准」列。建议实现已给出，可等价替换，但断言的事实必须相同。

- **01 test-instances-stopped**：`systemctl --user list-units 'agent-bus-*' --plain --no-legend`（含 loaded/active/inactive 全部已加载单元）名集合 ⊆ {agent-bus-server, agent-bus-mcp}。残留（如 :7491/:17590/:7493 三个试验实例对应 unit）→ FAIL，依据列出残留 unit 名与状态。
- **02 dead-protocols-deregistered**：`curl -s -H "Authorization: Bearer $(cat /data/agent-bus/tokens/fleet-graph.token)" http://127.0.0.1:7490/v1/protocols` 响应中子串 `coord.` 出现次数 == 0。依据给计数与命中样例。（当前真机 20 处 → 必红。）
- **03 decisions-zero-swallowed**：`curl -s http://127.0.0.1:7494/v1/decisions | jq '[.decisions[]|select(.state=="swallowed")]|length'` == 0。依据给总数与分状态计数。（当前真机 549 条中 231 swallowed → 必红；这是本单验收锚点。）
- **04 delivery-wakes-line**：取 :7494 里最近一条已送达（consumed/delivered 语义）且 target 为线（wf-*）的裁决，检查 `systemctl --user list-units 'fleet-graph-line-*'` 中存在 ActiveEnterTimestamp 晚于该裁决送达时刻（容差 90s）的新代 unit。无已送达线裁决、或时间对照不成立 → FAIL（依据给最近裁决时刻 vs 各线 unit 的启动时刻）。M2 之前预期红。
- **05 waiting-zero-llm-spend**：枚举 `/data/fleet-graph/runs/` 各线的 `.scheduler/<wf>.json` 与 `terminal.json` 驻停/状态字段：当前没有任何线处于 `waiting_dd` 语义 → FAIL（依据列出各线当前状态与来源文件，证明状态词表未上线）。若存在 waiting_dd 的线：再查该线 alias 在驻停窗口内的模型账本请求计数（网关 :15722 的 usage/账本面，探测过的端点写进依据），>0 → FAIL；账本面不可查询 → FAIL 并写明探测过的端点与错误。
- **06 acceptance-command-frozen**：goal MCP :5611 的 `goal_status` 面为每个已入编目标暴露 acceptance/acceptance_digest，且暴露值与该目标名册/载体钉的摘要一致、且引擎对摘要不一致拒绝点火（可观测降级：PASS 需「digest 字段存在 + 与名册钉的一致 + 有拒绝点火的结构化码面」三者都有，缺哪个依据里写哪个）。面不可调或缺字段 → FAIL。M1 之前预期红。
- **07 seat-single-source**：`tr '\0' ' ' </proc/$(systemctl --user show fleet-graph-dd-mcp -p MainPID --value)/cmdline` 不含 `--stage-model`。（当前真机带 `continuous_review=deepseek-v4-pro`、`final_review=deepseek-v4-pro` 两键 → 必红；验收锚点。）
- **08 public-interface-mcp-only**：`grep -rEn 'curl .*:(7490|7494)|fleet-graph line |fleet-maint' /data/code/self/agent-skills/plugins/agent-skills/skills/fleet-supervisor/SKILL.md` 命中数 == 0。依据给命中行。
- **09 takeover-one-call**：一次零上下文调用拿到六项（名册 / 线状态 / 等拍板 / 待上线 / 授权模式 / 当前 release）。探测：state 面 :7494（及 M6 后的 MCP）是否存在 takeover/state_takeover 端点或工具并真调用一次；不存在或缺项 → FAIL（依据写探测结果与缺失项清单）。
- **10 mcp-function-probes**：对五个面 bus MCP :5608、goal :5611、dd :5610、decision :5614、state :7494 各做 tools/list（或该面的等价发现调用）+ 一个只读工具真调用，全部成功 → PASS；任一失败 → FAIL（依据逐面列结果）。按各面真实协议实现（MCP JSON-RPC over HTTP 或既有 JSON API），探针超时 10s。
- **11 dd-gate-by-dispatching-line**：窗口（默认 24h）内 `/data/fleet-graph/dd/*/record.json` 中已出现闸裁决的单，逐张核 `decided_by == record.dispatched_by` 且 principal 为该线（对照 :7494 裁决记录的 target/principal）。窗口内无带闸裁决的单 → FAIL（依据给窗口内单数与裁决来源分布；今天闸由监督面批，预期红）。
- **12 foreign-delivery-refused**：以本机可用的最低权身份（或显式无效 principal）对**不存在的合成 id** `dev-fg-lim-selftest-probe` 调 :5614 decision_deliver(REJECT)：期望被拒且拒绝码含 `NOT_DISPATCHING_LINE`。实际返回其它结构化拒绝码 → FAIL 并贴返回原文（M2 前必红）；**返回被接受 → 严重红**（依据标 ACCEPTED）。绝不触碰真实单。
- **13 dd-touches-line-branch-only**：窗口内每张 dd 单：`record.json.remote_ref` 以 `refs/heads/release/<line-id>` 为前缀，且 canonical `/data/code/self/fleet-graph` 的 `origin/main` 上无该单的直接提交。（当前 remote_ref 是 `refs/heads/dd/<id>` → FAIL，依据贴实际 remote_ref。M5 之前预期红。）
- **14 rebase-before-dispatch**：窗口内每张 dd 单的 configure 段记录（events.jsonl/dd.log）含 rebase 到 `release/<line-id>` 的步骤。无该机制（M5 前）→ FAIL（依据贴窗口内最近一张单的 configure 事件样例，证明无 rebase 记录）。
- **15 message-delivered-and-acked**：goal MCP :5611 工具面存在 `line_message` 工具 → 进一步抽验最近一条给线的 inbox instruction 有 ack 落档；工具不存在 → FAIL（依据给 tools/list 摘要）。
- **16 message-cannot-impersonate-decision**：先决同 15：`line_message` 不存在或无 waiting_decision 样本可查 → FAIL（依据写缺什么）。工具在位后：验证「仅 inbox 消息不解除 waiting_decision 驻停」（读 .scheduler 驻停字段在最近一条 inbox 消息前后的对照；只读实现，不主动投递）。

## 边界与纪律

- 只新增 `scripts/verify-lim.sh` 一个文件（如仓内脚本清单/文档需要机械同步登记，仅做登记）；不改其它业务码；**零测试删除**。
- 本单不做任何让检查项变绿的功能实现（那是 M1–M8 各单的事）；也不要为了让某项好测而改动生产配置。
- 判据原文在 design.md §8（本 spec 已内联 16 项判定，冲突时以 §8 原文为准）；部署事实锚点与代理陷阱见 goal.md §7 S5/S6。

dd-acceptance
bash -lc 'test -x scripts/verify-lim.sh'
bash -lc 'out=$(bash scripts/verify-lim.sh 2>/dev/null); rc=$?; n=$(printf "%s\n" "$out" | grep -cE "^[0-9]{2} [a-z0-9-]+ (PASS|FAIL)"); f=$(printf "%s\n" "$out" | grep -cE "^[0-9]{2} [a-z0-9-]+ FAIL"); test "$n" -eq 16 && test "$rc" -eq "$f"'
bash -lc 'c=$(bash scripts/verify-lim.sh 2>/dev/null | grep -E "^(03|07) " | grep -c FAIL); test "$c" -eq 2'