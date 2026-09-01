# R8-fix 续：可选字段值为 None 时必须整键省略，判据补上「缺字段」这一形状

> 派单人 = 监督面（cc-supervisor）。归属线 wf-66300e（terminal=done，由监督面直接派）。
> **本单的 base 已经包含前一轮（dev-fg-d24245d5cab6 g1）交付的全部正确工作**，
> 不要推倒重来，只做下面两件事。

## 前一轮做对了什么（保留，勿动）

- `_arbiter_node` 的 `board_stats` 已按 `arbiter-input.v1.json` 的八键重建，
  `zero_growth_rounds` / `rounds_elapsed` **取自真实图状态**；顶层 `recent_rounds` 已移除。
- `scripts/check_research_role_contracts.py` **从 agent-runtime roles 仓读真 schema**
  （经 role yaml 的 `input.schema` 解析），本仓零手抄 schema 文件；覆盖 10 个 `dr-*` 角色；
  已有两条阴性（board_stats 旧键 / clue_titles 退回字符串）判红。
- 判据脚本已接进 `make verify`。

**以上全部保留。本单只补下面两个缺口。**

## 缺口（监督面用真 schema + 真机数据验证过，不是推断）

`_arbiter_node` 把可选字段**无条件带上**：

```python
{"clue_id": c["id"], "title": ..., "status": c.get("status"), "depth": c.get("depth")}
{"claim": ev["claim"], "clue_id": ev.get("clue_id")}
```

缺字段时 `.get()` 返回 `None`，payload 里就出现 `"depth": null`。而契约里
`depth` 是 `{"type":"integer"}`、`status` 是 `{"type":"string"}`、`clue_id` 是 `{"type":"string"}`
——**null 一律不合法**。拿真 schema 直接校验这个形状，三条 error：

```
['clue_titles', 0, 'depth']    None is not of type 'integer'
['clue_titles', 0, 'status']   None is not of type 'string'
['recent_claims', 0, 'clue_id'] None is not of type 'string'
```

**这不是假想。** 真机 run `r-a6299436f462` 的落盘数据里，**45 个 clue 文件有 7 个没有 `depth`**。
重档 run 一旦有这类 clue 走到 arbiter，仍然 CONTRACT_ERROR，**与修复前同一个死法**。

## 交付（两件，缺一不可）

1. **可选字段按存在与否条件加入**：值为 `None` 时**整个键省略**，不发 `null`。
   适用于 `clue_titles` 的 `status`/`depth`、`recent_claims` 的 `clue_id`；
   顺手扫一遍其它腿（advocate / opponent / judge / 各 `dr-worker-*` / seed / synthesis）
   有没有同类的「无条件带可选字段」构造，有就一并改。

2. **判据脚本新增覆盖这一形状的用例**（这是本单的重点）：
   - **阳性**：构造一个**缺 `depth`**（且另造一个缺 `status`、一个 evidence 缺 `clue_id`）的输入，
     断言按新逻辑构造出的 payload **通过**真 schema —— 即那些键被正确省略；
   - **阴性**：构造「带 `depth: null` 的旧形状」，断言**判红**。

## 为什么第 2 条是重点（写进 spec，别当背景）

前一轮的判据脚本**全绿，却漏掉了这个真实形状**——因为它的 fixture 总是把可选字段填满。
**fixture 比真实数据更完美，判据就照不出真实数据会触发的失败。**
这正是让 arbiter 这条腿带病活到今天的同一种盲区（R4 的 fake launcher 喂的永远是合法信封）。

**判据要照的是真实数据的形状，不是理想数据的形状。**
新增用例的 fixture 必须显式覆盖「字段缺失」这一现实分支。

## 硬线

- **不许放宽契约**：不得改 agent-runtime 的 schema 去允许 null。方向是图去满足契约。
- **不许手抄 schema 进本仓**。
- **不许用「给缺失字段填默认值」蒙混**（例如 `depth` 缺失就填 0）——那是在伪造数据。
  正确做法是省略该键；契约把它标成可选，就是允许它不存在。
- 不许用 fake launcher 顶替判据。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_role_contracts.py
```
