# R4：synthesis 单节点 → advocate/opponent/judge → arbiter 对抗裁决子图

> development_id = <派单后回填>
> target_base = 13b91947f85a126fdc09afa3bf9c3ff13d5c955e（main HEAD）
> spec_digest = <由 dd 冻结>

## 目标（宪法 P3「对抗而非合成」/ 条5「多模型讨论」）

把 `research_pipeline.py` 的 `synthesis` 单节点（单一 `research_synth` 角色）重构为
对抗裁决子图 `advocate → opponent → judge → arbiter`，沿用 agent-runtime 已交付的四个
既有角色，**不新造角色**。裁决不了的分歧显式开放写进报告，不伪造共识。

## 现状

- `research_pipeline.py` 的 `_synthesis_node` 用单一 `research_synth` 角色
  （`research-synth.input.v1` → `research-synth.result.v1` 的 `report_markdown`），
  一次性产出报告，无对抗、无交叉验证。
- 四个目标角色已存在于 agent-runtime（roles 仓 SSoT，本单对 roles 零改动）：
  - `dr-debater-advocate`（glm-5.2）
  - `dr-debater-opponent`（gpt-5.6-sol）
  - `dr-debater-judge`（deepseek-v4-pro）——三方三条不同模型腿，满足条5「多模型讨论」
  - `dr-arbiter`（claude-opus-5）——整板裁决
- 契约（roles 仓 `profiles/roles/schemas/`，SSoT）：
  - debater input  `deep-research.debater-input/v1`：`{question, evidences[{anchor,quote,claim,clue_id?}], prior_arguments[]}`
  - debater output `dr-doc.result.v1`：`{body}`
  - arbiter input  `deep-research.arbiter-input/v1`：`{question, board_stats, clue_titles[], recent_claims[], recent_rounds}`
  - arbiter output `dr-arbiter.result.v1`：`{verdict∈{enough,continue}, rationale}`

## 设计

把 `synthesis` 节点替换为子图（节点序列）`debate`，四段结构化 run 顺序执行，全部
`write=False`、语料经 `--prompt-file` 投递、`--input` 只携带 manifest：

1. `advocate`（`dr-debater-advocate`）：`debater-input.v1`{question, evidences} →
   `{body}` 正面论证（每条 substantive claim 带 `[anchor: …]`）。
2. `opponent`（`dr-debater-opponent`）：`debater-input.v1`{question, evidences} →
   `{body}` 反驳/证伪路径（同带 anchor）。
3. `judge`（`dr-debater-judge`）：`debater-input.v1`{question, evidences,
   `prior_arguments=[advocate.body, opponent.body]`} → `{body}` 逐条分歧裁定：
   - 能被既有证据裁决的 → 给出裁决 + 该证据 anchor；
   - 不能裁决的 → 逐字记为 OPEN DISAGREEMENT，不得调和、不得伪造共识。
4. `arbiter`（`dr-arbiter`）：`arbiter-input.v1`{question, board_stats,
   clue_titles, recent_claims, recent_rounds} → `{verdict, rationale}` 对整板给一次
   enough/continue 裁决。

落地约定：
- 四角色 input manifest 与语料分别落 `inputs/` 下独立文件（`debate-advocate.json` /
  `debate-opponent.json` / `debate-judge.json` / `debate-arbiter.json`）。
- run id 用 `derive_run_id(thread_id, "debate/{role}", 1)` 派生（幂等：kill-restart 后
  同 id 重派 = re-adopt，绝不二次派发）。
- 四角色产出逐字落 `run_root/debate/` 下（`advocate.md` / `opponent.md` / `judge.md` /
  `arbiter.json`），供审计与 agent-run 记录对账。
- `report.md` 由脚本节点（零 LLM）从 judge 产出组装，**必须含** `## 分歧裁定` 段：
  - 「已裁定分歧」：judge 判定被既有证据裁决的项，附裁决 + anchor；
  - 「开放分歧」：judge 的 OPEN DISAGREEMENT 逐字保留（不调和、不删、不改写为共识）；
  - 「arbiter 裁决」：arbiter 的 verdict + rationale 逐字记录。
  judge 产出零条 open 分歧时，「开放分歧」段显式写「本轮无未决分歧」——该段不得省略。

## 边界（硬线）

- **不破坏 `converge()` 纯度**：`converge()` 仍是纯函数、零 LLM、零 IO。arbiter 的
  `verdict=continue` 仅**记录并响亮落盘**（进 report「分歧裁定」段 + events），不改动
  converge 的路由语义——循环继续/终止仍由 converge 决定，其哨兵化属 R7，本单不越界。
- **state 只装 id 与计数**（规格第 7 条）：debate 的 body/verdict 一律落 run root
  文件，不进 checkpoint。
- **不新造角色**：四角色逐字引用 agent-runtime 已交付角色名，改名/重注册即违约。
- evidences 复用现有 `evidence.jsonl` 的 finding 形状（anchor/quote/claim），不新增
  中间协议。
- 四角色 run 失败/信封不可解析 → `TERMINAL_FAULT`（响亮，不静默），沿用 synthesis
  现有失败语义。

## 判据（机器可判）

① 报告 `report.md` 含 `## 分歧裁定` 段（字符串级判据）；
② 至少一次真实产出 open 分歧：judge 的 OPEN DISAGREEMENT ≥1 条，且逐字出现在报告的
   开放分歧列表（未被调和/删除/改写）；
③ 三方角色 agent-run 记录在案：一次 run 中 `dr-debater-advocate` /
   `dr-debater-opponent` / `dr-debater-judge` 三角色各至少一次 agent-run（经 launcher
   角色记录 / run root 产物可对账）。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_debate.py
```