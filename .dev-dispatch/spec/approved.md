# R5：锚点核验——接入 anchor-check、产出 anchor-check.json、核验率 >90% 软闸门、报告头写 dr-anchor-rate

> development_id = <派单后回填>
> target_base = <R4 合入 main 后的 HEAD，派单时回填>
> spec_digest = <由 dd 冻结>

## 目标（宪法 P1「证据先于结论」/ 条3「产物极强可读性」）

把「机器可核验锚点 + 核验率」接入 `research_pipeline`：终验 run 的报告每条
conclusion/claim 的 `[anchor: …]` 引用必须可机器核验回 evidence，产出
`anchor-check.json`，核验率 >90% 为软闸门；`report.md` 报告头写 `dr-anchor-rate`。

## 现状

- `research_bus.finding_anchor`（`research_bus.py:95`）已把每条 finding 派生出
  带版本 URI 的 `anchor`（source + locator），evidence 有 locator 可锚定。
- 但**无 anchor-check**：报告里 `[anchor: …]` 的引用没有机器核验，核验率未计算；
  `report.md` 头没有 `dr-anchor-rate` 字段（宪法差距表 P1 / 条3 均判「缺」→ R5）。
- ⚠️ 历史 `ANCHOR_CHECK_BIN` 跨仓硬编码指向 katana 仓——重构时按「共性判别铁律」
  落位到本仓脚本，禁跨仓硬编码路径。

## 设计

在 `finalise` 之后接一个**零 LLM 纯脚本节点** `anchor_check`（统一落本仓
`scripts/check_research_anchor.py`，不硬编码跨仓 bin 路径）：

1. 输入：`report.md`（含 `[anchor: …]` 引用）+ `evidence.jsonl`（finding 形状
   `{anchor, quote, claim}`）。
2. 逐条核验：报告内每条 `[anchor: …]` 引用按 anchor 精确/派生匹配回 evidence；
   anchor 能命中 evidence ⇒ ok，命中不了 ⇒ failed，无 anchor 的 conclusion ⇒
   单列 unanchored（计入分母、不计 ok）。
3. 产出 `anchor-check.json`（落 `run_root/anchor-check.json`），至少含：
   - `claims`: 逐条 `{anchor, quote, claim, verdict∈{ok,failed,unanchored}}`；
   - `summary`: `{total, ok, failed, unanchored, rate, sums_ok}`；
   - `rate = ok / total`（软闸门 90%，即 `rate > 0.90`；
     `sums_ok = (ok + failed + unanchored == total)`）。
4. 报告头 `report.md` 顶部写 `dr-anchor-rate: <rate>`（例：`dr-anchor-rate: 0.962`）。

落地约定：

- anchor-check 是脚本节点，零 LLM、零外呼 IO；state 只装 id 与计数，正文/verdict
  一律落 `run_root/anchor-check.json`，不进 checkpoint。
- anchor 派生规则复用 `research_bus.finding_anchor`（同一条 finding 恒得同一条
  anchor，双源对账据此逐条匹配），不重写、不新造中间协议。
- 核验率 <90% 是**软闸门**：响亮记录（report 头 + anchor-check.json + events），
  不判红、不改 converge 路由；红绿判定由判据脚本独立执行，锚定终验 run。

## 边界（硬线）

- **不破坏 `converge()` 纯度**：anchor-check 在 finalise 侧、纯脚本，不影响
  converge 的路由语义。
- **不新造角色**：anchor-check 是本地脚本，无 agent-run、无新 route。
- **软闸门 ≠ 放行**：rate ≤90% 时 run 正常 finalise 但报告头/events 响亮记录未达标；
  判据脚本对终验 run 判红。
- **不跨仓硬编码**：`ANCHOR_CHECK_BIN` 等 katana 仓路径一律移除，改本仓脚本。

## 判据（机器可判）

① 终验 run 的 `anchor-check.json` 存在，且 `summary.rate > 0.90`；
② 同一份 `anchor-check.json` 的 `summary.sums_ok == true`；
③ `report.md` 报告头含 `dr-anchor-rate` 字段；
④ 判据脚本 `scripts/check_research_anchor.py` 自检：在**阴性 fixture**（无 anchor /
   核验率 ≤90% / sums 不平）上判红，在**阳性**（合法终验产物）上判绿——脚本自身
   无效即判据失败。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_anchor.py
```