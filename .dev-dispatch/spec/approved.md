# 缺陷⑬ · 闸权 authority 文本改 D5 口径 + 全仓清 S8 影子（wf-8d9737）

## 背景
golden-order D5（2026-09-03 用户拍板）已定：**「gate / decision 是 goal agent 来判断的……人只在入编放行、goal 级验收、答升报三处出现；DD 闸由派单线自判，decided_by 必须等于该单 dispatched_by」**。仓库里现存两处旧口径残留与 D5 相抵：
1. `src/fleet_graph/dd/self_gate.py` 模块 docstring 的 authority 段（当前 L3-7：After M1 … the dispatching line is its own gate … 段）叙述了自判事实但未锚 D5 拍板，且 L13 引用链写成「design.md §6.2/§6.3; goal.md §二 M3 + S8/S9/S10/S11/S12」——把已被 D5 取代的 S8（闸归监督面）与在用裁决混列。
2. 全仓仍存在 S8 字样引用（`git grep -n "S8"` 当前唯一命中即 self_gate.py:13 的「S8/S9/S10/S11/S12」串）。

## 交付
1. **self_gate.py docstring authority 段改写为 D5 口径**：L3-7 区域明确三条——闸由派单线自判（golden-order D5）；`decided_by` 必须等于该单 `record.json.dispatched_by`；人/监督面只在入编放行、goal 级验收、答升报三处出现。引用链更新为「golden-order D5；design.md §6.2/§6.3；goal.md §二 M3 + S9/S10/S11/S12」形状（去掉 S8，六项义务编号与内容逐字不动）。
2. **全仓清 S8 影子**：`git grep -n "S8"` 在 src/ tests/ docs/ scripts/ config/ deploy/ README.md 范围内零命中（`.dev-dispatch/`、`.dd-evidence/`、`.git/` 机器件豁免；历史提交不可改，只清工作树与新增文件）。若发现本 spec 未列的 S8 引用（注释、文档、变量名），一并清除或改写为 D5 口径，逐处在 commit message 里列出。
3. **机械断言**：新增 `tests/test_gate_authority_text.py`（或并入现有 gate 文本测试族）：
   - 阳性：self_gate.py docstring 含「D5」「decided_by」「dispatched_by」锚词，且不含「S8」；
   - 阳性：对仓根做受控扫描（排除 .git/.dev-dispatch/.dd-evidence/.venv/__pycache__）断言无 "S8" 字样；
   - 阴性：把 docstring 里 D5 锚词换成 S8 旧口径的临时变异 → 断言测试红（用例内以 fixture 复制文本变异，不改生产文件）。

## 边界
- 只改 `src/fleet_graph/dd/self_gate.py` 的 docstring/注释与新增测试文件；**六项义务的编号、语义、函数签名与行为逐字不动**；不改任何生产逻辑。
- 不动 `.dev-dispatch/`、`.dd-evidence/`；不做 M5/M6 面；不引入新依赖。
- 零测试删除（既有断言改写到新真值不算删除，但本单预期不需要）。

## 验收（dd-acceptance 围栏，逐字冻结）
```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_gate_authority_text.py'
bash -lc 'make verify'
```
