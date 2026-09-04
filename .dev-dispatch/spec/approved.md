# Spec ⑫-b（wf-8d9737）· 机械审计/board note 渲染切具名升级目标（缺陷⑫ 收尾）

> 状态：落卷即派（监督面 2026-09-04 10:15 指令；协调面已亲证两处在位）。
> base 钉死：**origin/release/wf-8d9737 @ 7f20b340a69bb8e2ed29964c9abff5a54419cd09**（派单前 git fetch 复核无漂移；=部署执行位）。
> 判据锚：缺陷⑫ 本体（a2.py ESCALATION_TARGETS 已修好的 schema 面）、board 2813（⑮ 半生效同族方法论）、监督面 10:15 指令原文（⑪/⑮/⑫ 今日第三次同型——交付须全仓枚举，禁只修点名两处而漏同族）。
> 现状普查（base 7f20b340 亲测，全仓 src+tests+config+scripts）：`人仍拍板` 恰好 **2 处**——
> `src/fleet_graph/graphs/supervisor.py:361`（board-facing 审计 note 头，非 preauth 分支）与 `src/fleet_graph/supervise/audit.py:1084`（render_note 头行）；
> tests 现无任何对旧措辞的断言（`grep 人仍拍板 tests/` 零命中）。

## 要交付的行为（全部在 fleet-graph 仓）

1. **两处渲染切具名（指令 a 项）**：上述两处渲染改为从 `arbiter/a2.py:58` 的 `ESCALATION_TARGETS` 词表（`dispatching_line` / `supervisor_escalation` / `needs_evidence`）**按主体形态具名指路**：
   - dd 单已过 acceptance/在闸 ⇒ 正常路径是**派单线自判（D5）**，指向 `dispatching_line`；
   - B 类升报（方向/生产动作裁定）⇒ `supervisor_escalation`；
   - 现有证据不足以判 ⇒ `needs_evidence`（回补证据，缺什么点名什么）。
   - 复用 a2.py 词表常量（import 复用，禁止复制出第二份词表造成漂移）；渲染文本须含具名目标；
   - **不得再广播 `needs_human`/「人仍拍板」作为去向**——审计 note 可以说明「本单不发 work.decision.v1」，但去向必须具名。
2. **同族枚举交付（指令 b 项，监督面方法论）**：交付证据必须列**全仓同族出现点枚举清单＋逐项处置**（渲染面：grep `人仍拍板`；同义遗留：以 `needs_human` 作为**输出去向**的渲染；a2.py 的 back-compat **解析**不属渲染、不在处置面）。以本 spec 头部普查（2 处）为起点，实现者须在交付时重新枚举并给出逐项处置（改/不改+理由）。
3. **红靶做成测试（指令 c 项）**：新增 `tests/test_d12b_audit_note_targets.py`：
   - 全仓 grep 断言 `人仍拍板` = **0**（若实现需要白名单，白名单须显式列出且逐项断言其存在与理由）；
   - 两条渲染路径各给**具名目标正例**（按主体形态断言渲染文本含正确的 ESCALATION_TARGETS 成员）；
   - **旧措辞阴性用例**：构造渲染，断言输出不含 `人仍拍板`/`needs_human` 作去向（防回归）；
   - 主体形态→目标的路由断言至少覆盖三类目标各一条正例。
4. **边界（指令 d 项）**：不动 `a2.py` 的 schema/词表本体（已修好）；不改 classify 语义本身；只动上述两处渲染与措辞绑定及其测试。
5. **验收冻结（指令 e 项，S6 双清代理口径）**：

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_d12b_audit_note_targets.py'
bash -lc 'make verify'
```

> 零测试删除；既有断言随语义改写更新不算删除。座位（D8）：implement=glm-5.3-flash，continuous_review=final_review=glm-5.3，经 stage_models 传入。
