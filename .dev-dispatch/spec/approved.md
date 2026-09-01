# R8 冷启动终验的两个前置修复：重档 clue 预算可达性 + 判据①真实 argv

## 背景（真机实测，不是推测）

R8 交付物 B 的冷启动 run `r-eb92da8f3974`（2026-09-01 07:34–07:57Z）机器全链跑通
（seed → 8 dispatch → 8 collect → harvest → 4 debate → debate_report → finalise → anchor_check），
但 `terminal="capped"`、`terminal_reason="max_clues 24 触顶（total=29）"`、`rounds=1`、`coverage=3`。
anchor 核验率 0.85 < 0.90，R8 不达标。

根因是算术，不是题目：

- `research_entry.TIER_BOUNDS[heavy].max_clues = 24`，`concurrency = 8`；
- 该 run 的 `seed` 事件记录 **`clues: 17`** —— 光 seed 一步就吃掉 24 的预算里的 17；
- `converge()`（`graphs/research_pipeline.py:401-403`）**触顶判定先行**：
  `if total >= bounds.max_clues: return TERMINAL_CAPPED`，在「线索树耗尽 / 零增长 / 轮次预算」
  之前无条件短路；
- 于是第 1 波 8 个 collect 只要合计提出 ≥7 个子线索，total 就越过 24，整个 run 在
  **只探完 3 条线索**时直接判 capped 收尾。

结论：**重档档位在当前 bounds 下结构性无法收敛**——任何真需要多源的题目，seed 的
扇出量级都在 15–20，留给子线索的余量不足一波。报告因此只剩 debate 骨架，
anchor 分母里结构行（裁定前言、P3 强制的 OPEN DISAGREEMENT、仲裁者控制面字段）占比被抬高。
这不是判据太严，是预算太小。

第二个问题（已由监督面记账，一并修）：判据 ① 目前**自证**。
`research_entry.py:204` 写的是 `record_launch_argv(run_root, canonical_launch_argv(question))`
—— 落盘的是**由题目重建的 canonical argv**，不是进程真实 argv。因此不论实际怎么发起，
`launch.json` 永远与 canonical 形状 exact 相等，判据 ① 在真实 run 上恒绿、零鉴别力。

## 目标

1. 让重档冷启动 run 在**不改任何验收判据**（anchor 阈值 0.90、五件套、冷读 PASS 全部不动）
   的前提下，具备真正跑到 `converged` / 多轮 `capped` 的可达性。
2. 让判据 ① 变成真判据：核的是进程真实 argv。

## 交付物

### D1 —— 重档 clue 预算可达性

- `research_entry.TIER_BOUNDS[TIER_HEAVY]` 的 `max_clues` 由 `24` 提到 **`96`**。
  定值依据写进代码注释：实测 seed 扇出 17，重档 `concurrency=8`，
  96 ≈ seed 上界 20 + 至少 8 波各 8–10 条子线索的余量，
  使「轮次预算 / 零增长 / 线索树耗尽」三条正常收敛路径有机会先于 clue 触顶生效。
  `max_depth` / `zero_growth_rounds` / `max_rounds` / `concurrency` **一律不动**。
- 轻档 bounds **一律不动**。
- `converge()` 的判定语义、优先级、`capped 绝不报成 converged` 的硬性要求 **一律不动**
  —— 本单只调预算数值，不碰规格语义。

**回归测试（新增，必须能红）**：

- 阳性：构造 seed 产出 17 条 clue、随后两波各新增 8 条子线索的 state，
  在重档 bounds 下 `converge()` **不得**返回 `capped`（应为 `continue`）。
- 阴性（变异枪）：把 `max_clues` 临时改回 24 跑同一 fixture，该用例**必须转红**——
  证明测试真的钉住的是预算而不是别的东西。阴性用参数化或 `monkeypatch` 表达，
  不得靠注释声称。
- 保留一条既有语义的守护用例：`total >= max_clues` 时仍**必须**返回 `capped`
  （证明本单没有把触顶语义改坏）。

### D2 —— 判据 ① 记录真实 argv

- `record_launch_argv` 的调用点改为记录**进程真实 argv**（`sys.argv`，或入口层拿到的
  真实参数列表），而不是 `canonical_launch_argv(question)` 重建值。
  三面入口（CLI / MCP / 程序内调用）都要落到「这次实际是怎么被发起的」这一事实上；
  非 CLI 入口没有 `sys.argv` 语义时，落该入口的真实调用签名，并在 `launch.json` 里
  用显式字段标明入口种类，**不得**再用重建值冒充。
- `judge_launch_command` / `check_research_coldstart.py` 的 ① 判定相应更新：
  比对的是真实 argv 与「唯一入口、一条命令、无题目相关注入」的形状约束。
  **判据只准变严，不准变松**：原来恒绿的地方现在必须能因「argv 与入口形状不符」判红。
- 既有的五种阴性派生（坏 argv / 缺 launch / 断证据链 / 缺归位 / …）全部保留且继续绿。

## 边界（硬线）

- **不得改动任何验收判据的阈值与口径**：anchor 阈值 0.90、`sums_ok`、冷读 PASS、
  五件套的定义，一个字都不许动。本单的授权前提就是「不改判据」。
- 不得改 `converge()` 的判定优先级与终态命名。
- 不得改 anchor-check 的分母口径（OPEN DISAGREEMENT / 仲裁者控制面字段是否计入，
  属另一议题，本单不碰）。
- 不得为了让测试变绿而放宽既有断言；既有测试一行不许删。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run pytest -q tests/test_research.py tests/test_research_entry.py tests/test_research_coldstart.py
```
