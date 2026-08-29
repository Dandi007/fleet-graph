# 运维：两个闸门

调度器有两个互不替代的闸。搞混它们的代价在事故里才显形，所以这里写清楚
各自管什么、怎么动、多久生效。

## 闸一：名册（`enabled`）—— 哪些线**允许**跑

`config/ronin-lines.json` 里每条线的 `enabled`。**默认 `false`**：一条线跑
是因为一份过了 review 的配置说它跑。

```jsonc
{ "folder_id": "wf-40fa8d", "seat": "opencode-gpt-terra", "enabled": true }
```

- **改法**：改配置 → PR → 合入 → `deploy/release.sh` → `systemctl --user restart fleet-graphd`
- **生效**：分钟级（要走发布）
- **用途**：P7 分批放量；长期决定舰队编成
- 名册外的线每个 tick 打一行 `line_disabled`——**可见地不跑**，不是静默地不跑

放量下一批要同时改 `tests/test_ronin_lines_config.py::test_exactly_the_canary_is_switched_on`，
否则测试会拦下来。这是故意的：批次是断言，不是某人的记忆。

## 闸二：紧急停机（maintenance-stop）—— 现在**全体停**

```
/data/fleet-graph/maintenance-stop
```

```bash
# 停：注意 expires_at 是必填，且必须是未来时间
cat > /data/fleet-graph/maintenance-stop <<JSON
{
  "reason": "写清楚为什么停，事后要有人看",
  "issued_by": "你是谁",
  "issued_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "expires_at": "$(date -u -d '+4 hours' +%Y-%m-%dT%H:%M:%SZ)"
}
JSON

# 放：删文件
rm /data/fleet-graph/maintenance-stop
```

- **生效**：下一个 tick（≤60s），不需要重启、不需要发布
- **`expires_at` 必填且到期即失效**（用户 2026-08-23 裁决）。这意味着**停机会自己
  解除**——它是止血带不是长期方案。要长期停，改名册。
- **文件解析不了 = 停**（与旧门禁相反）。一张坏掉的闸门文件是运维错误，值得停下来
  看，不值得当作不存在。
- 路径曾是 `/data/ronin/maintenance-stop`，随该栈 P4 退役一并迁走。旧路径现在**不
  被读取**，往那儿写没有任何效果。

## 卡住的线会自己退避（不用你管）

一条线如果**结束时一轮都没往前走**（`terminal.json` 里 `rounds: 0` 且不是 `done`），
调度器给它记一次。连续几次之后重试间隔逐次翻倍：5min → 10 → 20 → 40 …
**上限 6 小时**。日志里的词是 `no_progress`，不是 `cooling_down`：

```json
{"folder_id": "wf-40fa8d", "ignited": false, "refusal": "no_progress",
 "detail": "wf-40fa8d ended 3 run(s) in a row without advancing a round; backing off, 1180s left of 2400s"}
```

看到它，去读那条线自己的 `terminal.json` 的 `reason`——**调度器不读那段文字**（那是
agent 写的散文，读它就成了替它判活干得对不对），但你该读。

三件要知道的事：

- **是退避不是锁死**。blocker 自己好了（服务回来了、有人回答了问题），下一次尝试就会
  接上，不需要谁来解锁。
- **计数落盘**（`<run_root>/.scheduler/<folder_id>.json`），发布重启不清零。否则每次发版
  都会把卡住的线放回全速——而发版正是最没人盯着它的时候。
- **判据是计数不是文字**。同一个 blocker 被 agent 换个说法写出来，字符 bigram 相似度可以
  只有 0.28；任何基于 reason 文本的判重都会当成两个不同问题。

要立刻让它再试一次：删掉那个计数文件。要让它彻底别跑：改名册 `enabled: false`。

## 等人的线会停牌（R0c）

退避解决的是「blocker 可能自己好」的线。还有一类线好不了：coordinator 判了
`blocked` 且声明 `waiting_on: "decision"`——**只有人的裁决能解除**。对这种线，
按退避节奏反复点火只是在按全额 coordinator 成本反复推导出同一个 blockage。
所以调度器把它**停牌**（parked）：不点火，直到出现机械可判定的唤醒事实。

```json
{"folder_id": "wf-1", "ignited": false, "refusal": "parked_awaiting_decision",
 "parked": true, "blocker": "等监督面拍板（L2-5）"}
```

停牌成立的条件（全部机械事实，不读散文）：

- 最近**已记账**的 terminal 是 `blocked` 且 `waiting_on: "decision"`
  （字段由线自己的 finalise 写进 `terminal.json`；缺省和未知值都按 `none`
  处理——只有明确声明 decision 才停牌）；
- 停牌时拍下快照：`parked_at`、goal.md 的 `content_revision`、对应 `run_id`，
  存进同一个计数文件（`<run_root>/.scheduler/<folder_id>.json`），**发布重启
  不丢**。

三个唤醒源，任何一个成立就清快照、回到正常判断顺序（该冷却冷却）：

1. **inbox 来信**：线的 `agent:<alias>` 频道里出现了**晚于 blocked terminal**
   的消息（更早的那些，blocked 那一轮已经读过了）。无 alias 的线没有这个源。
2. **goal.md 变了**：work-folder MCP `fs_stat` 的 `content_revision` 与停牌快照
   不同。改 goal（走治理写门）就是叫醒它的正规方式。
3. **逃生口**：手动把计数文件里的 `parked_run_id` / `parked_at` /
   `parked_goal_revision` 三个字段清成 null——下一个 tick 立即可点火，且同一个
   terminal 不会被重新停牌（`park_considered_run_id` 留着就是干这个的）。
   删整个计数文件也放行（顺带清退避计数）。

三件要知道的事：

- **停牌是省钱优化，不是判决**。唤醒探测失败 **fail-open**：当作没有停牌条件，
  回到普通退避。坏掉的探针最多费钱，不能把线锁死。
- **两个唤醒源独立降级**（2026-08-27 热修，真机 403 教训）。inbox 探测挂了
  （token 无 `agent:*` 读 ACL 的 403、404、超时、terminal 时间戳解析不了）只废
  这一个源：停牌照常建立在 goal.md 锚上，park_event 记
  `established:inbox_unavailable:<类名[:状态码]>`，快照里 `parked_inbox_available:
  false`——停牌期内不再重试 inbox（403 是结构性缺口不是抖动，下一个 blocked
  terminal 建立时会重新评估）。只有 goal.md 锚（每条线都有的那个源）取不到才
  整体 fail-open 不停牌；停牌期内 goal 锚探测失败也照旧保守唤醒。
- **每个 terminal 至多停牌一次**。唤醒或放行之后同一个 run 不会再停；如果重新
  点火后又 blocked 在同一个裁决上，新 terminal 会自己再停一次。
- **停牌那个 tick 会尽力向 board 发一条 question note**（带幂等键）。已知契约
  缺口：`work.note.v1` 要求 ref 指向一个**已存在**的 board entity，而 goal 线
  没有卡——发不出去时降级为仅日志可见（`board_question` 字段记录结果），升报面
  的完整化归 R4。

## 验收执行步（R0d）

goal 线每个 worker turn 之后有一个**机械验收步**：进程内 subprocess 逐条执行
名册里声明的 argv，产出 `[{command, exit_code, duration_s, tail}]`，作为下一轮
coordinator 输入的 `last_acceptance` 字段。**执行归编排层，裁决仍归
coordinator**——这一步只报退出码和尾部输出，红了也不改任何路由；红意味着什么，
由 coordinator 在下一轮拿着事实裁。这是 counts-versus-prose 边界的又一次应用：
执行不是裁决。

**声明处 = 本仓 `config/ronin-lines.json`（PR-reviewed）**，字段：

```json
"acceptance": [["systemctl", "--user", "is-active", "loop-engine-jobd"]],
"acceptance_cwd": "/tmp",
"acceptance_timeout_seconds": 300
```

为什么不放 goal.md / work folder：**凡 agent 可写的面都是不当控制输入**
（wf-13ff9e findings §31c）——把「编排层会替你执行什么命令」交给 agent 可写的
文件，等于让被验收者自己写验收器。声明只认走 PR review 的名册。

信任锚与可见性：声明经 launcher 以 `--acceptance-json '<json>'` 一参传给线，
在 `systemctl --user` 的 argv 里**可见，且这是 acceptable 的**——信任锚是名册的
PR review，不是 argv 的保密性；argv 里没有任何秘密，也没有任何 agent 写的东西。

三件要知道的事：

- **`not_declared` 是显式事实，不是静默跳过**。没声明命令的线，coordinator 每轮
  照样收到 `{"status": "not_declared"}`——「没验收」和「验收过了」必须不可混淆
  （这正是三条在跑线每轮记 "NOT RUN" 的教训）。声明了命令但没写
  `acceptance_cwd` 的线，收到 `{"status": "skipped:no_cwd"}`：执行目录是声明的
  一部分，不许隐式继承引擎自己的 cwd。
- **超时/起不来记合成退出码**（同 supervise/audit.py 约定：124 超时、127 找不到
  命令），tail 是 stdout/stderr 各截末 2000 字符；每条命令都会执行，前一条红
  不吞掉后一条。命令只继承 PATH/HOME（白名单在 `acceptance.py` 的 `ENV_KEEP`，
  扩白名单走 PR）。
- **执行步自身炸了不 fault 线**：记 `{"status": "acceptance_error"}` 事实，
  coordinator 自己裁。坏掉的验收器最多损失可观测性，不能杀掉工作。

## 判断顺序

`decide()` 里名册排在最前：一条没进名册的线，refusal 是 `line_disabled` 而不是
`maintenance_stop`——否则运维会被支使去清一张根本不是元凶的 flag。

反过来，`enabled: true` 不等于放行：紧急停机、已在跑、terminal=done、停牌、
冷却/退避、总熔断、网关探针红，七道闸照常。停牌排在退避之前：一条等裁决的线
显示的是 `parked_awaiting_decision`，不是 `no_progress`。

## 现场怎么看

```bash
systemctl --user status fleet-graphd
journalctl --user -u fleet-graphd -f          # 每 60s 一批，每条线一行 JSON
```

每行形如 `{"folder_id": ..., "ignited": false, "refusal": "line_disabled", ...}`。
`refusal` 就是上面七道闸加名册的名字，一一对应。

## 线级换座（step 7：`line set-seat`）

名册座位是 SSoT，只走 PR/review/deploy 改。等不及发布的时候（座位订阅挂了、
家族分流、坏放量要立刻换道），用**运行时 override** 换座——**绝不动
`config/ronin-lines.json`**：

```bash
# 用法（从仓库根或部署目录跑；--lines-config 缺省 config/ronin-lines.json）
fleet-graph line set-seat wf-9b5931 opencode-gpt-terra \
  --reason "dsv4pro 订阅道故障，先切 terra 保线" \
  --who "$USER"
```

流程：**先探活预检目标座**（C4，probe 不健康直接拒绝并报因）→ 写一条
`who / when / from→to / reason` 四字段齐全的 override 到调度器持久面
（`<run_root>/.scheduler/seat-overrides.json`）→ 把持久 generation +1，下一个
tick 调度器以 override 座**新 generation 冷启动**、经 wf_resume 续上下文。
当前 generation 还在跑时新 generation 不会起（`already_running`）；要立刻交接
先停掉旧 unit（unit 名 = `fleet-graph-line-<folder>-g<generation>`，用
`systemctl --user list-units 'fleet-graph-line-*'` 查当前名）：

- **C1（审计字段）**：override 必带 `who/when/from→to/reason`，缺任一字段拒绝写入。
  换座是 B 类生产变更，override 本身就是审计痕。
- **C2（临时态）**：override 是运行时临时态。永久化仍走 PR 改名册；合入部署后
  reconcile 自动清掉与 roster 相等的 override（相等即不再是 override）。
- **C3（漂移不许静默）**：`line overrides` 是 reconcile/lint 巡检面——先折叠已
  收敛的 override，再**响亮列出**所有 `roster ≠ 生效座位` 的 override（含 diff
  事实），零漂移干净退出（exit 0），有漂移 exit 1。调度器每个 tick 也会在 stderr
  重打一遍漂移清单，长期漂移不可能静默：

```bash
fleet-graph line overrides                 # 人类读
fleet-graph line overrides --json          # 机器读（cleared + drift）
```

- **C4（三元可观测）**：每个 tick 的线状态带三元
  `seat_roster / seat_override / seat_effective`，一眼能看出线实际跑在哪个座、
  为什么。探活与启动都看**生效座位**，不是名册座位——探错面等于没探。

批量换座 = 逐线 `set-seat` 原语编排，没有第二条旁路。

## 监督面：supervisor 图与事件泵（R4-2/R4-3）

值守的机械化：四类**机械事件**唤醒一次短跑审计，审计报告落板（evidence note）
与 supervisor 自己的 run root。分类三种：`needs_human`、`recommend_reject`、
`preauth_release`（R4-3 第四道闸）。逐条拍板仍是人的：唯一的自动放行路径要求
**人先在板上签发机械预授权**，且结构上只够得到集成分支 merge（见下）。

### 开关

名册 config 里 `"supervisor_events": true`（默认 **off**）。改法同
`probe_via_runtime`：config PR → 发布 → 重启。观察器寄生 fleet-graphd 既有
60s tick——**没有第二个常驻循环，没有第二个调度器**；supervisor 图本身是被
`systemd-run` 拉起的又一种 transient unit（`fleet-graph-supervisor-<key>`）。

### 四类事件（去重键 = thread identity）

| 事件 | 机械信号 | 去重键 |
|---|---|---|
| E1 `board_question` | `board:work-notes` 上 question note 无 decision ref | `e1-<note_id>` |
| E2 `blocked_decision` | terminal `blocked` + `waiting_on: "decision"` | `e2-<run_id>` |
| E3 `line_fault` | terminal `fault` 或 `pump_fault: true` | `e3-<run_id>` |
| E4 `cap_breaker` | `TickResult.refusal == total_cap_reached` | `e4-cap-<时间桶>` |

thread_id = `supervisor:{key}:a{attempt}`（attempt = 观察器 cursor 里该键的
终身 launch 计数，从 1 起），checkpoint 在
`/data/fleet-graph/supervisor/checkpoint.sqlite3`。世代语义与 ronin 线的
`{folder_id}:g{n}` 同款：**每次 launch 是新 attempt、新 thread，checkpoint
天然隔离**——重跑一个事件不再需要对共享 sqlite 做外科手术；同一 attempt 内
kill-restart 照旧**精确 re-adopt 在飞审计 run**，不重派、不重付费（测试钉死）。
旧格式 thread（无 `:aN` 后缀）留在库里成为惰性行，无需迁移。receipt 路径
照旧按 `event.key`（一事件一 receipt，重跑覆盖写）。E2 与 R0c 停牌**共存**：
停牌照旧省钱，观察器只负责把事实递给审计。

### 预算与游标

- 每 tick 至多拉起 **2** 个 supervisor run；每事件键终身至多 **3** 次尝试
  （纯计数，落盘防重启清零）；审计已在飞（unit active）不烧预算。
- 板游标持久于 `<run_root>/.scheduler/supervisor-cursor.json`；**首次启用
  adopt-baseline**：游标落在当前 head，存量 pending 问题不回放（那是人已有的
  backlog，`fleet-graph inbox list` 看得到）。要回放，把 `board_seq` 改小。
- 事件审完出 receipt（`/data/fleet-graph/supervisor/reports/<key>.json`），
  同键永不再拉起。**要重审，用文档化重置命令**（幂等，只动 supervisor 自己的
  状态面）：

  ```bash
  fleet-graph supervisor reset e3-<run_id>          # 删 receipt + 清尝试计数
  fleet-graph supervisor reset e1-<note_id>         # 另外机械回拨 board_seq 到该问题之前
  fleet-graph supervisor reset e1-<note_id> --board-seq N   # 机械定位不了时显式指定
  ```

  三件套一次做完：删 receipt、清 cursor 里该键的 attempts、（仅 E1）回拨
  `board_seq`——E2/E3/E4 每 tick 从 terminal/tick 结果重推导，无游标可回拨。
  **不碰 checkpoint db**：重跑是新 attempt、新 thread，旧行天然惰性。
  **不需要重启 fleet-graphd**：观察器每 tick 从盘上重载 cursor 文件；唯一
  例外是 reset 恰与一个在飞 tick 竞态（该 tick 收尾覆写一次），重跑一遍
  reset 或 `systemctl --user restart fleet-graphd` 兜底。

### 一次 supervisor turn 的形状

七节点：`intake → gather_evidence → rerun_acceptance → audit(llm) → classify
→ act → receipt`。script 包夹唯一 llm 节点（`agent-run --role
supervisor_auditor`，read-only、structured、每条断言强制 command +
output_excerpt）；**分类闸门是 script 对机械谓词的判定**——llm 只建议，
`recommend_reject` 必须有机械复现依据（精确报错原文 + 最小复现 argv + exit
code），llm 喊 reject 而无机械红照样 `needs_human`。证据不全直接跳过 llm，
以事实升报，不猜。

goal 线无板卡时 evidence note 会被 bus 以 422 拒掉（既知契约缺口）——act 降级：
报告只落本地 receipt，degraded 原因写进报告。审计产物**绝不写被监督线的
work folder**（§38e 治理活锁的结构性消解）。

### 现场怎么看

```bash
journalctl --user -u fleet-graphd -f | grep supervisor_observer   # 观察器动作
ls /data/fleet-graph/supervisor/reports/                          # 审计 receipt
cat /data/fleet-graph/logs/supervisor-<key>.log                   # 单次 run 日志
# 手动补一次审计（事件词汇封闭，未知名字直接拒绝）：
fleet-graph supervisor run --event-json \
  '{"type":"line_fault","key":"e3-<run_id>","payload":{"folder_id":"wf-…","run_id":"<run_id>"}}'
```

### 预授权放行闸（R4-3 第四道闸）

前三道防代拍闸原样不动（Board 无 decision 发布方法、gate resume 值被丢弃每次
重读板、decision 只经 ref 图解析）。第四道 = **独立主体 + 机械预授权 + 凭证分离**：

- **preauth 是板上的一等对象**：人发一条 `work.decision.v1`，payload
  `kind: "preauth"`，机器可读字段 `{card_entity_id, allowed_actions:
  ["approve"], target_ref_allowlist: [<ref 前缀>], expires_at}`。
  `expires_at` **必填**（无期限 = 常开自批按钮，校验层拒收）；allowlist 用
  **前缀白名单**（不用正则），且**构造性不可能覆盖 main/master/production/
  release**——含它们的 preauth 在校验层被拒，负例测试钉死。
- **classify 三要素机械判定**（script，llm 的建议不是输入）：① 原文覆盖
  （动作 ∈ allowed_actions、卡 entity 精确匹配、now < expires_at）；② 目标
  ref 从 git 锚定现算（development id 出自 bootstrap commit 锚定的识别文件，
  ref = 控制面固定规则 `refs/heads/dd/<id>`，不采信任何 agent 自述）∈ 前缀
  白名单；③ 署名锚点齐备（question note id + preauth 消息 id）。另加前置：
  机械审计**全绿且无 gap**、该 question 尚无 decision。**缺一 → needs_human，
  不报错、不猜**。
- **唯一 decision publisher + 凭证分离**：`supervise/decision_publisher.py`
  是全仓唯一允许构造/发布 `work.decision.v1` 的模块；只发 `APPROVE` +
  `scope: "merge_only"`（**合入≠部署**），`decided_by` 固定为
  `"supervisor-graph (依预授权 <msg_id> 代行；非人逐条拍板)"`，refs 同时指向
  question note 与 preauth 消息。凭证走独立 env
  `FLEET_GRAPH_DECISION_TOKEN_FILE`，只在 act script 节点进程内读取；
  `executors/agent_run.py` 按 `FLEET_GRAPH_DECISION_` 前缀从一切 agent 子进程
  env 剥除，dd 控制面的 env 白名单从不转发它。
- **必须停人闸的封闭枚举**（`supervise/preauth.py` 的
  `HUMAN_ONLY_CATEGORIES`，测试钉死）：production main/release promotion、
  部署授权、判据/spec 改判、cancel 在跑 development、preauth 本身的签发与
  展期、REJECT（v1 不纳入 preauth，驳回只出建议）。
- decision 发布被拒（无凭证/bus 故障）只降级记录：板上不出现 decision，
  question 仍开着，gate 继续等人——这条分支的失败模式是 needs_human，
  永远不是静默放行。

守卫（`make conformance`，随 verify 跑）：supervisor 模块不 import
`scheduler.ignition`/`scheduler.launcher`；`work.decision.v1` 发布调用唯一
豁免 `supervise/decision_publisher.py`；publisher 的 import 白名单只有
`graphs/supervisor.py`（act script 节点），llm 执行路径（executors/、dd 图）
结构上够不到发布入口。三条都有 sabotage 自证测试。

## A2 只读仲裁（wf-7cd0a7 re-scope：只分诊只建议，永不裁决）

A2 arbiter（`src/fleet_graph/arbiter/`）是消费者/编排层的一次性分诊组件，
不是 agent-bus 传输层的东西。它读板上的不可变事实（open question note 及
其 refs、blocked/非终态 development 卡、显式 @arbiter 咨询），经
`executors/text_node`（进程内、只读、纯文本）调一次推理角色，产出
**recommendation envelope**（`subject_id`/`recommendation`/`evidence_refs`/
`consequence`/`needs_human`——输出契约**不用** decision/verdict/approve/reject/
gate_release 字段名）。

- **发布面构造受限**：唯一可写是 `arbiter/publisher.py`，只发 `work.note.v1`
  + `note_type ∈ {finding, progress}`，note 正文带 `[A2 suggestion — not a
  decision]` 前缀并 ref 指向 subject card/question。没有通用 publish 方法。
- **幂等**：idempotency key = subject identity + source revision；读到已引用
  该 subject 的 A2 note 即抑制重发（replay/restart 不重复建议）。
- **默认 dry-run**：`fleet-graph arbiter run` 只读板、只落建议到 stdout；
  显式 `--publish` 才落板。本 development 不在生产启用发布。
- **审计面**：`fleet_graph.arbiter.audit` 列出一轮 A2 发出的每条消息的
  kind / note_type / marker / message id / subject refs，零 decision 断言
  用真实 `work.decision.v1` fixture 做已知负例，证明它能区分裁决而非空板。
- **人闸不动**：A2 建议只是 `work.note.v1`，`Board.decision_for` 只认
  `work.decision.*`，故建议永不满足 gate / `wait_for_decision` / decision
  publisher——该等 decision 的仍等。

现场用法：

```bash
fleet-graph arbiter run --bus-url http://127.0.0.1:7490        # dry-run
fleet-graph arbiter run --alias arbiter --publish               # 显式发布
```

### A2 托管周期（managed path）

常驻托管形态是 `deploy/systemd/fleet-graph-arbiter.service` + `.timer`：
每个 tick 一次 oneshot `fleet-graph arbiter run --publish --alias arbiter`
（`arbiter` alias 映射到 inbox `agent:arbiter`），15 分钟一次有界节律，
`Type=oneshot` 且无 `Restart=`——tick 结束即退出，没有重启循环，再次触发
只由 timer 决定。

**前置条件（独立的部署闸，未激活）**：本 development 只交付代码，**不安装、
不启用、不启动**任何 unit/timer，不创建生产 principal/alias/token，不落任何
token 文件。真正的激活是监督面在后续已批准窗口里的独立决策。届时合规启用
的前提是：

- 生产上 arbiter principal 已存在且 `agent:arbiter` binding 已可读解析；
- 站点 env 文件 `~/.config/fleet-graph/arbiter.env` 存在且只含读凭证
  （`FLEET_GRAPH_BUS_TOKEN` 或 `FLEET_GRAPH_BUS_TOKEN_FILE`），**不含**决策
  发布凭证——`FLEET_GRAPH_DECISION_TOKEN_FILE` 属于 supervisor act 节点独有，
  arbiter 永不在 git/argv/stdout/stderr/receipt/journal 中引用它。

**身份 reconcile（发布前置）**：`--publish` 路径在模型工作与落板之前先做
只读 principal/alias reconcile（`src/fleet_graph/arbiter/reconcile.py`）：
认证 principal == 期望 arbiter principal（默认 `agent:arbiter`，可用
`FLEET_GRAPH_ARBITER_PRINCIPAL` 覆盖），且 alias binding 解析到
`agent:arbiter`。缺失/不匹配/被重绑/未授权任一状态报非秘密错误并以非零退出，
**先于任何模型工作与发布**；reconcile 模块没有 create/register/token-mint/
token-write 或其它 mutation 调用。

**终态工件与验收查询**：一次成功 tick 打印一条有界机读 receipt
（counts/kinds/refs），不含凭证。验收夹具
`scripts/a2_managed_path_acceptance.py` 打印有界 JSON 并断言
`referenced_note_or_suggestion_count >= 1`、`work.decision.v1 == 0`、
`work.decision.v2 == 0`、`decision_marked_chat == 0`。验收查询：

```bash
uv run pytest -q tests/test_arbiter.py tests/test_arbiter_managed_path.py tests/test_deploy_unit.py
uv run python scripts/check_supervisor_conformance.py
uv run python scripts/a2_managed_path_acceptance.py
```

**回滚（一行，仅供后续已批准窗口执行；本 development 不执行）**：

```bash
systemctl --user disable --now fleet-graph-arbiter.timer fleet-graph-arbiter.service
```

## dd 控制面（R1-b：MCP 服务即控制面）

`fleet-graph dd serve`（`:5610`，unit 模板 `deploy/systemd/fleet-graph-dd-mcp.service`）
就是 dev-dispatch 的控制面：7 个实工具（create/start/get/list/events/evidence/gate）
直驱进程内 `dd/control_plane.py`，背后没有第二个服务。

- **状态在哪**：git 祖先链 + `/data/fleet-graph/dd/<development_id>/` 下的
  durable checkpoint 与 run 工件（`record.json` / `events.jsonl` / `result.json` /
  `launches.jsonl`）。`status.json` 只是可重建缓存，删了会被 `rebuild_status`
  原样重算。**没有数据库。**
- **admission**：`development_create` 只收 `{repo_path, target_base, spec_text|spec_path}`；
  development id、H0 digest、durable ref、acceptance argv（spec 里的
  ```dd-acceptance 块）全部服务端推导。同一 admission 幂等返回同一个 id。
- **start**：transient systemd unit（`fleet-graph-dd-<dev>-r<n>`）。控制面重启
  不影响在跑 run；kill 后再 start 自动带 `--resume` 重入同一 thread
  （`<dev>:g1`），已封存 stage 不会重派。
- **gate 不收裁决**：`development_gate` 只报挂起的 question note，`resume=true`
  无值重入，图自己重读板。裁决只经 board `work.decision.v1`
  （带 `refs=[{"target_entity": <question_note_id>}]`）。
- **模型策略**：`dd serve --stage-model continuous_review=deepseek-v4-pro`
  是部署侧 flag，不进 client 词表。
- **审计**：`fleet-graph supervise audit <dev-id> --repo <clone>`——`--dd-root`
  下有 record 的 development 自动走进程内 `GraphEngineSource`，其余走老引擎。

### 失败三分出口（R1-c）

每个非 `complete` 终局在 `status.failure` / `evidence[].failure` 上带一条失败
记录：`{class, code, raw_error, retryable, exit}`——因果分类、单一机械成因码
（一码一因，禁垃圾桶码）、失败方原话、可重试位、开放的出口。分类在读侧从
run 工件现算，不落第二份真相。

| class | 判定（code） | 出口 | 语义 |
|---|---|---|---|
| `environment_contract` | 除下两类外的一切（taxonomy 环境/契约码、`ACCEPTANCE_FAILED` / `SETUP_FAILED` / `RUN_CONFIG_MISSING` 等验收上下文拒绝、fault） | `reconfigure`：`development_reconfigure` 改验收上下文 → `development_start` 起新 generation | 验收环境缺件 / acceptance argv / setup 契约错。老引擎 FAILED 后 reconfigure 恒 409 之痛在此消灭：FAILED 与一切非终态都可调 |
| `implementation` | `GATE_REJECTED` / `REWORK_LIMIT_REACHED` / `REVIEWER_GIT_MUTATION` / `UNDECLARED_ARTIFACT` / `SECRET_SENTINEL_DETECTED` | `rework`：图内 continuous REJECT → 新 attempt（原样保留）；终局后 start 新 generation 重做 | 工作本身被判不合格，换环境救不了 |
| `fabrication` | `UNVERIFIED_TEST_CLAIM` / `ARTIFACT_BLOB_MISMATCH`（seal 复跑与 actor 断言不符族） | `none`：终局。reconfigure 与 start 都以 `FABRICATION_FINAL` 拒绝并指明原因 | 撒谎的 actor 不配换考卷也不配重考 |

`development_reconfigure(development_id, acceptance_env?, acceptance_argv?,
setup?)` 的 schema 就是它的边界：只有验收上下文三个参数，没有 spec / 实现 /
role patch 参数——spec 冻在 bootstrap digest 下，改 spec 永远等于新 development。

### 重跑 generation

- thread id = `{dev}:g{n}`；`--generation` 进 argv，派生 run id
  （`g{n}-a{m}` tag）、gate 的 bus idempotency key（`dd-gate:{dev}:g{n}`）全部
  带 generation——同单重跑不再撞 `IDEMPOTENCY_CONFLICT`。
- `development_start`：非终态续跑本代 thread；可重试终局（或 reconfigure 后）
  起 g{n+1} 全新一代；`complete` 与伪造类拒绝。
- run 工件按代分目录：g1 在 `<dev>/`（存量布局不动），g{n>1} 在
  `<dev>/g{n}/`；checkpoint.sqlite3 全代共享（thread id 内含代号）。
- `development_evidence` 每代一个 entry，receipt 链跨代连续：g{n} 的链以
  g{n-1} 的尾 commit + 尾 digest 为种子，revision 跨代累计编号；
  `development_events` 默认读当前代，传 `generation` 读历史代。
