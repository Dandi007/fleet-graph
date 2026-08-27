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

- **停牌是省钱优化，不是判决**。唤醒探测失败（bus 不通、MCP 超时、时间戳解析
  不了）一律 **fail-open**：当作没有停牌条件，回到普通退避。坏掉的探针最多费
  钱，不能把线锁死。
- **每个 terminal 至多停牌一次**。唤醒或放行之后同一个 run 不会再停；如果重新
  点火后又 blocked 在同一个裁决上，新 terminal 会自己再停一次。
- **停牌那个 tick 会尽力向 board 发一条 question note**（带幂等键）。已知契约
  缺口：`work.note.v1` 要求 ref 指向一个**已存在**的 board entity，而 goal 线
  没有卡——发不出去时降级为仅日志可见（`board_question` 字段记录结果），升报面
  的完整化归 R4。

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
