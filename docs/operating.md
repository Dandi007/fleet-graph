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

## 判断顺序

`decide()` 里名册排在最前：一条没进名册的线，refusal 是 `line_disabled` 而不是
`maintenance_stop`——否则运维会被支使去清一张根本不是元凶的 flag。

反过来，`enabled: true` 不等于放行：紧急停机、已在跑、terminal=done、冷却、
总熔断、网关探针红，六道闸照常。

## 现场怎么看

```bash
systemctl --user status fleet-graphd
journalctl --user -u fleet-graphd -f          # 每 60s 一批，每条线一行 JSON
```

每行形如 `{"folder_id": ..., "ignited": false, "refusal": "line_disabled", ...}`。
`refusal` 就是上面六道闸加名册的名字，一一对应。
