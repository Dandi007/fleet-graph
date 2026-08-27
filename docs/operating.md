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

## 监督面：supervisor 图与事件泵（R4-2）

值守的机械化：四类**机械事件**唤醒一次短跑审计，审计报告落板（evidence note）
与 supervisor 自己的 run root。**本单不发任何 `work.decision.v1`——分类只有
`needs_human` 与 `recommend_reject`，人仍拍板**；依预授权代拍是 R4-3 的第四道闸，
现在的代码里那条分支是显式 `NotImplementedError`。

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

thread_id = `supervisor:{key}`，checkpoint 在
`/data/fleet-graph/supervisor/checkpoint.sqlite3`：同一事件 kill-restart 后
**re-adopt 在飞审计 run**，不重派、不重付费（测试钉死）。E2 与 R0c 停牌**共存**：
停牌照旧省钱，观察器只负责把事实递给审计。

### 预算与游标

- 每 tick 至多拉起 **2** 个 supervisor run；每事件键终身至多 **3** 次尝试
  （纯计数，落盘防重启清零）；审计已在飞（unit active）不烧预算。
- 板游标持久于 `<run_root>/.scheduler/supervisor-cursor.json`；**首次启用
  adopt-baseline**：游标落在当前 head，存量 pending 问题不回放（那是人已有的
  backlog，`fleet-graph inbox list` 看得到）。要回放，把 `board_seq` 改小。
- 事件审完出 receipt（`/data/fleet-graph/supervisor/reports/<key>.json`），
  同键永不再拉起。要重审：删对应 receipt 文件（尝试计数照旧生效）。

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

守卫（`make conformance`，随 verify 跑）：supervisor 模块不 import
`scheduler.ignition`/`scheduler.launcher`；全仓无 `work.decision.v1` 发布路径。
两条都有 sabotage 自证测试。

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
