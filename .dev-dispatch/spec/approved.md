# R6：入口与产物归位——CLI/MCP/skill 三面统一路由 + 轻/重档分级 + 产物落 DeepThought/wiki 域 + 老路径退役

> development_id = <派单后回填>
> target_base = <R5 合入 main 后的 HEAD，派单时回填>
> spec_digest = <由 dd 冻结>

## 目标（宪法 条6「可及性」/ 条8「使用闭环」/ 条9「分级」）

把 deep-research 的调用入口收敛到唯一一套路由（CLI + MCP tool + skill 三面），按规模统一
分轻/重档；终验产物（report）落 wiki 域 `DeepThought/<topic>/`，可被别的域编程检索；
老 loop-engine 入口（`bin/deep-research.sh` / drain CLI）退役——双系统并存违条6。

## 现状

- 唯一能用的入口是 CLI `fleet-graph research run --question ...`（`cli.py:1158`）；
  MCP 面无、skill 面无——三面只通一面，且**无轻/重档路由**（没有按规模的统一分级）。
- 产物落本仓 run root `/data/fleet-graph/research/<id>/`，不在 wiki 域 `DeepThought/<topic>/`
  （物理 `/data/vault/DeepThought/`）——其他域（chatgroup/dd）不能按 wiki 纪律检索或编程调用。
- 老引擎 loop-engine 入口仍现役：`bin/deep-research.sh`（`--tier heavy --profile ...`）、
  `bin/deep-research-loop.sh`、loop-engine drain CLI——**新旧两套并存，违条6「入口唯一」**。

## 设计

1. **三面统一路由**：CLI 子命令 / MCP tool / skill 三个 surface 全部落到同一个 research
   runner（同一入口、同一路由判定），不各写各的入口。
2. **轻/重档分级**：同一入口按规模标定轻/重档（`--tier light|heavy` 或等价的确定性规模判定），
   两档产物同 shape（report + anchor 元数据），仅 bounds 不同
   （`max_clues/max_depth/concurrency/max_rounds`），不派生出两套产物 schema。
3. **产物归位**：终验 report 落 `DeepThought/<topic>/`（wiki 域，遵 `wf-3f87f3` 先例的命名纪律），
   run_root 仍保留中间态（`evidence.jsonl` 等）；归位在 finalise 侧，不破坏 R1 双源对账。
4. **老路径退役**：`bin/deep-research.sh`、`bin/deep-research-loop.sh` 与 loop-engine drain 入口
   降级为历史引用或删除；代码库内不存在指向老引擎 drain 的现役入口。

落地约定：

- 三面共享同一 runner，路由判定是**纯函数/确定性**，可机器核验（同输入恒得同档位）。
- 轻/重档差异只体现在 bounds，不派生两套产物 schema（条9「格式对齐」）。
- wiki 域落点按共性判别铁律落位到本仓，禁止跨仓硬编码老 `DeepThought`/katana 路径。

## 边界（硬线）

- **不破坏 `converge()` 纯度**：路由与产物归位在入口/finalise 侧，不动 converge 的路由语义。
- **不新造角色**：三面都是 surface，底层仍走既有 research runner + 12 个 `dr-*` 角色，无新 route。
- **不跨仓硬编码**：`bin/deep-research.sh` 等老跨仓/跨引擎路径一律移除，改本仓入口。
- **退役即退役**：双系统并存违条6，老入口不得保留为与三面并行的第二套现役路径。

## 判据（机器可判）

① 三面调用成立：CLI 子命令、MCP tool、skill 三个入口都存在，且指向同一路由（判据脚本对三面探测，缺一判红）；
② 轻/重档统一路由成立：同一入口可发起 light/heavy 两档，两档产物格式对齐（判据脚本校验路由判定纯函数 + 两档产物 schema 一致）；
③ 入口唯一 + 产物归位：代码库中无现役 `bin/deep-research.sh` / `bin/deep-research-loop.sh` / loop-engine drain 入口，且终验 run 报告落 `DeepThought/<topic>/`（wiki 域可检索）。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_entry_home.py
```