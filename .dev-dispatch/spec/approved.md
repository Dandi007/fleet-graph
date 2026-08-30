# dd implement 围栏修复：超时重试必须重领养在飞 run + per-stage timeout 派单可配

## 背景（生产实证 2026-08-30，监督面立案）
dev-fg-ee3840dbc4f6（ledr c5fix5）implement 连续两代死于同一署名：
`PROVIDER_UNAVAILABLE: implement run <id> did not finish: run still running after 3720.8s; not terminal`
（events.jsonl 12:30:49Z 与 13:32:50Z 两条，run_id 分别 d586bb3e-… / 36423907-…）。

代码里的设计承诺与现实矛盾：
- `graphs/dd_actors.py` RunWaitTimeout 分支注释明言「the retry re-adopts the run
  in flight rather than paying for a second one」（run_id 派生自 stage+attempt）；
- 但重试路径 attempt 递增 → run_id 变化 → 实际是**弃养旧 run、重派新 run**：
  旧 run 继续烧 token 无人收割（本案两个 run 事后 state=unknown），大 spec
  （implement >62min）永远过不去 3600s 默认围栏。
- `DevelopmentConfig.timeouts`（per-stage dict）存在但零外部参数面：
  `development_create` 不收 timeouts，reconfigure 只管 acceptance 语境——
  任何单都改不了围栏。

## 要求
1. **超时重试重领养**：当 implement（及同通道 stage）因 RunWaitTimeout 产生
   retryable 失败后，后续重试必须**重领养同一个仍在飞的 run**（沿用原 run_id
   继续 wait），而不是派新 run。仅当被领养 run 已真正终局失败/丢失时才允许
   新派。实现方式自定（如超时重试不递增 run_id 派生因子、或 ticket 落
   checkpoint 后 re-adopt），但必须消灭「旧 run 在飞 + 新 run 并跑」的双烧窗口。
2. **per-stage timeout 派单可配**：`development_create`（service + MCP 工具 +
   HTTP 面）新增可选 `timeouts` 参数（dict[stage_id -> 秒]，正整数，未知
   stage id 4xx 拒绝），透传至 DevelopmentConfig.timeouts；不传 = 现行默认
   3600s，存量行为零变化。record.json 落档该参数以便审计。
3. 回归测试：
   - 模拟 launcher：首次 wait 超时、run 随后完成 → 重试重领养同 run_id，
     stage 成功且 launch 总数 = 1；
   - 被领养 run 已 lost → 才允许第二次 launch；
   - create 带 timeouts={"implement":7200} → 该单 implement 围栏 7200s、
     其它 stage 仍默认；未知 stage id 被拒；不传参单行为逐字节不变；
   - 存量套件零回归。

## 边界
- 只改 fleet-graph 仓：graphs/dd_actors.py、graphs/dd_runner.py、
  executors/agent_run.py（如需 re-adopt 支持）、dd/service.py 与 HTTP/MCP
  参数面、tests/；不改 roles/prompts/协议 schema，不动 supervise/、state/。
- 遵循仓 AGENTS.md 与引擎本体加严条款。

```dd-acceptance
make verify
```
