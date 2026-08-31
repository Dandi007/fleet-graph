# R8：冷启动终验（DoD）——全新题目一条命令无人搀扶，端到端出带锚点可冷读报告

> development_id = <派单后回填>
> target_base = <R7 合入 main 后的 HEAD，派单时回填>
> spec_digest = <由 dd 冻结>

## 目标（DoD = 用户原话「真机部署 + 端到端拿到结果」/ P4「冷读可用」）

deep-research V4 的终点终验：全新题目、一条命令、无人搀扶冷启动，跑到一份带锚点、
可冷读的报告；并证明新底座不劣于老引擎最好成绩（对照基线：报告 55,114 字节、
anchor 核验率 96.2% → V4 须「有报告 + rate>90% + 冷读 PASS」，不拿老引擎最差一次当及格线）。

## 现状

- R1–R7 全部 DONE 且已合 main（R7 preflight/哨兵已收割 #202）。
- 新底座能力已齐：多源 worker 矩阵(R2)、并发 fan-out(R3)、对抗裁决(R4)、anchor-check(R5)、
  三面入口+轻重档+wiki 归位(R6)、preflight+失败语义哨兵(R7)。
- 缺 DoD 这一仗：真机冷启动终验，兑现「一条命令无人搀扶」的终点判据。

## 设计

1. **一条命令冷启动**：经唯一入口 `fleet-graph research run --question "<全新题目>"` 冷启动，
   全程无人搀扶（判据脚本只发这一条命令，不注入任何预设/提示/历史线索）。
2. **全新题目**：不得复用任何历史 run 的题目或证据；题目须真需要多源（含 web），防假阴。
3. **终验产物**：report 落位 `DeepThought/<topic>/`（复用 R6 wiki 归位）+ `anchor-check.json`
   （rate>90% + sums_ok，复用 R5 锚点核验）。
4. **冷读 subagent**：一个无上下文的 subagent 冷读报告并给 PASS/FAIL verdict（P4「第三方无需
   解释可读可复用」的可机器化近似）。
5. **五件套落 progress**：①发起命令原文 ②run 证据链 ③报告路径与字节 ④anchor 数字 ⑤冷读 verdict。

落地约定：

- 终验 run 身份唯一、可复现；判据脚本只读 run 产物，不搀扶、不改图。
- anchor 数字读 `anchor-check.json`；冷读 verdict 由独立 subagent 产出，机器可判绿红。

## 边界（硬线）

- **无人搀扶**：判据脚本不得向 run 注入任何题目相关提示/预设线索，否则 DoD 失效。
- **全新题目**：复用历史证据/题目 = 判红（假阴防制）。
- **不新造角色**：冷读 subagent 复用既有 role 或纯脚本读报告，无新 route。
- **基线不糊弄**：anchor >90% 与冷读 PASS 为硬杠，不得拿「有报告」代「可冷读」。

## 判据（机器可判，五件套）

① 发起命令原文在案（判据脚本机械记录 CLI argv）；
② run 证据链完整（dispatch/collect/agent-runs + evidence.jsonl 存在、可回放、coverage>0）；
③ 报告存在且非空（`DeepThought/<topic>/report.md` 落位，字节数机械核 >0）；
④ anchor 核验率 > 90%（`anchor-check.json` 的 `summary.rate > 0.90` 且 `sums_ok==true`）；
⑤ 冷读 subagent verdict == PASS。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_coldstart.py
```