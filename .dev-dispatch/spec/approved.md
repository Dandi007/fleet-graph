# 案A改写 ④：无可解析 verify 的仓必须在任何写动作之前 escalate（钉成契约）

来源：监督面直写 goal.md 2026-09-02 15:0x「🔴 案 A 改写」第六节 + 第八节任务 4。
监督面已逐条真机证实现状（取证锚点），实现据此，不重证。

## 取证锚点（机械事实）
1. H9 之后 `resolve_verify_argv` 按目标仓解析 verify argv（真机实测）：
   - fleet-graph / fleet-harvest-sandbox / agent-runtime → `['make','verify']`
   - fleet-sentinel / agent-session-mcp → `['uv','run','pytest','-q']`
   - lexicon / wiki-v3 / goal-agent → `None`（无可解析 verify command）。
2. 解析不到的仓目前在 `verify` 步 `ok:false + escalated`，且**发生在任何写步骤
   （pr_squash_merge / ff_only_pull / deploy）之前**——但这是实现副作用，**未被断言契约保护**。

## 要什么
把上面这条「副作用」钉成**显式契约**：无可解析 verify 的仓，收割必须在**任何写步骤
（`pr_squash_merge` / `ff_only_pull` / `deploy`）之前** escalate——
`verify`/`verify_real` 解析不到可执行指令时，`ok:false` + 机器可读 detail（指名缺 verify 的目标仓）
+ `outcome=escalated` + `writes_skipped` 覆盖全部写步骤，绝不继续往下走产生任何写原语。

## 判据（可红）
- **变异（核心）**：让 verify 解析失败时**继续往下走**（越过 escalate 放行后续写节点）→
  必须有用例转红，断言点钉住：escalate 早退 + `writes_skipped` 覆盖全部写步 + **无任何写发生**。
- 回归：有可解析 verify 的仓行为不变（照常进写步）；`make verify` 全绿无回归。

## 交付约束
- 只改 `src/fleet_graph/supervise/harvest.py`（verify/routing 早退与 writes_skipped 记账）、
  `src/fleet_graph/supervise/harvest_ops.py`（`resolve_verify_argv` 判空→escalate 契约化）、
  与 `tests/test_harvest.py` 及必要 fixture；
- **不得改动、不得签发** `/data/fleet-graph/supervisor/harvest-allowlist.json`；
- 不改 E5/E6/E7 事件词表；不部署、不重启。

```dd-acceptance
uv sync --frozen
make verify
```