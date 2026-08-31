# R1-返工：publish 委托头修 + 降级响亮化，使 clue/evidence/doc 真机落 bus

> development_id = <派单后回填>
> target_base = 13b91947f85a126fdc09afa3bf9c3ff13d5c955e（main HEAD）
> spec_digest = <由 dd 冻结>

## 根因（监督面真机取证，逐字，非推断）

生产 bus 上 `research:r-*` 频道数 = **0**（总频道 115，`research:agent-harness-*` 36 条全属老引擎）
⇒ V4 四个生产 run **一条消息都没发布出去**，P2「过程即产品、bus append-only 可回放」在生产中实际不存在。

根因链：`bus/client.py:97-100 _headers()` 逐字
`if self.agent_id: headers["X-Bus-On-Behalf-Of"] = self.agent_id`——**无条件加委托头**，
自己的 token 亦被当委托请求 ⇒ 每次 publish `403 DELEGATION_NOT_PERMITTED` ⇒ 被
best-effort **静默吞掉**（`research_bus.py:175` 仅 `log.warning`，注释「与 observe 同义」）。
凭证同源（探针与 fleet-graphd 共用 `~/.config/fleet-graph/env`）⇒ 非探针特有，线的正常路径同样命中。

## 修复要点（三项，实现方向留给单自行论证）

1. **修委托头**：token 即该 agent 自身时不应发 `X-Bus-On-Behalf-Of`（或改走非委托的自证路径）。
2. **publish 降级响亮化**：run 终局产物落 `publish_degraded: {count, first_error}`。
   best-effort 可继续吞异常，但**不许静默**——静默降级等于悄悄取消 P2。
3. **不得**用「吞 409」或放宽发布判据来绕过——内容寻址键下吞 409 = 数据分岔静默化（本卷硬约束）。

## 边界（硬线）

- 不碰 agent-runtime；不新造角色；state 只装 id 与计数。
- 首轮交付（PR #169）的 schema 单源（`research_bus.py:179-194` registry 派生）**有效保留，不返工**。
- publish 失败仍降级（不 fault 整图），但降级必须可观测（`publish_degraded` 非空，不得报绿）。

## 判据（机器可判，须随单冻结为可执行脚本）

- **R1-a**：一次真实 run 之后，`research:r-<id>.{index,evidence,docs}` 三个频道在 bus 上**存在且 `head_seq > 0`**；
- **R1-b**：受控 probe 令 publish 全程 403 时，run 必须以**可观测降级态**收尾（`publish_degraded` 非空），**不得报绿**；
- **阴性 fixture 现成**：`r-2193db185d0f`（bus 上零频道）——新判据必须在它上面判红，否则脚本无效。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_publish.py
```