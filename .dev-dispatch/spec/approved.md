# R7：preflight 与失败语义——部署契约机器可判 + 哨兵静默死亡/checkpoint 卡死响亮 + agent-runtime 违约立案跟踪

> development_id = <派单后回填>
> target_base = <R6 合入 main 后的 HEAD，派单时回填>
> spec_digest = <由 dd 冻结>

## 目标（宪法 条7「常绿可判」/ 条10「失败语义」/ P5「判断驱动终止」）

给 deep-research 补「部署契约 preflight」与「失败语义」两层：
- 部署契约 preflight（检出对齐 / 依赖齐 / role 可派 / channel 可建）机器可判绿红；
- fleet-graph 侧哨兵语义核实补齐——进程静默死亡 / checkpoint 卡死必须响亮，不得报 succeeded/exit 0；
- agent-runtime 座位层「契约违约报 succeeded/exit 0」不本图修，立案跟踪（dev-fg-67feadc91821）在案。

## 现状

- 无 preflight：部署契约四面（检出/依赖/role/channel）没有机器可判的绿红工装，「常绿可判」缺位。
- 哨兵语义未核实：缺陷族已见多起静默形态（C5 drain 静默死亡、line 静默不重拉、worker 无产出仍报成功）。
- agent-runtime 把契约违约报成 succeeded/exit 0 已由 #480 修（exit 97），但座位层另有立案单
  dev-fg-67feadc91821 在跟踪；本卷不在图里做容忍式补丁。

## 设计

1. **preflight 脚本**（本仓 `scripts/check_research_preflight.py`，零 LLM、只读探测）：
   机械核四面——检出对齐（构建 == 期望 origin/main head）、依赖齐（.venv/uv + 12 个 dr-* 与
   research synth 的 role yaml 在位可解析）、role 可派（route/runtime 声明可解析）、
   channel 可建（bus 探测待建 channel 是否可创建）。绿/红机器可判。
2. **失败语义哨兵**：对「worker 无产出」与「哨兵被杀 / checkpoint 卡死」两种受控形态，
   run 必须以响亮终态收尾——不得报 succeeded/exit 0、不得把「全空」判成 converged、
   不得让 retryable=false 的终局错误被磨成循环。
3. **上游跟踪**：agent-runtime 座位契约违约不本图修，判据只求立案号 dev-fg-67feadc91821 在案，
   本图不做容忍式补丁（不得把「succeeded/exit0」当合法）。

落地约定：

- preflight 是脚本节点，零 LLM、零外呼 IO；绿红判定由判据脚本独立自检（阳性判绿/阴性判红）。
- 哨兵响亮覆盖两侧：既「该响必响」（静默失败必出响亮终态），又「响后重试尊重 retryable=false」。
- 每条判据须能回答「什么情况下这条会红」——答不上来不得入闸（缺陷族第九式解药）。

## 边界（硬线）

- **不破坏 `converge()` 纯度**：preflight 在入口/node 侧只读探测，不动 converge 路由语义。
- **不新造角色**：preflight 是脚本工装，无 agent-run、无新 route。
- **不在图里修 agent-runtime**：座位契约违约是 agent-runtime 层，跟踪即可，禁容忍式补丁。
- **响亮 ≠ 标错**：哨兵响亮的退出口要真实（非 null 终态），不得伪装成 succeeded。

## 判据（机器可判）

① preflight 脚本对已知好/坏 fixture 判绿/红（四面：检出对齐 / 依赖齐 / role 可派 / channel 可建，缺一面判红）；
② 受控 probe 复现「worker 无产出」→ 响亮终态（非 succeeded/exit 0、不判 converged，判据脚本对阴性 fixture 判红）；
③ 受控 probe 复现「哨兵被杀 / checkpoint 卡死」→ 响亮终态；且 agent-runtime 立案号 dev-fg-67feadc91821 在案。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_preflight.py
```