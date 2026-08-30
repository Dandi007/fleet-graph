# R2：deep-research 多源 worker 矩阵 —— dispatch 按 clue 的 source 路由到 6 个 dr-worker 角色

## Goal

把 research 图的 worker 侧从单一角色 `research_worker_local` 升级为多源矩阵：
dispatch 节点按 clue 的 `source` 路由到 agent-runtime 已交付的 6 个 `dr-worker-*`
角色。显式修复诊断发现的缺陷「**dr-worker-web 派发数恒为 0**」：此前 dispatch
从不读 `clue.source`、恒用单一 worker 角色，导致 web（以及默认源以外的所有源）
永远不被派发。

R1 已交付的 bus 协议（`research.clue.v2` / `research.evidence.v2` /
`research.doc.v2` 三 kind，幂等键内容寻址派生）是 R2 的依赖底座，本开发
**沿用而不重定义**：不新增、不重注册任何 bus kind；不发明任何新 role。

## Source 词汇与角色映射（沿用，不新造角色）

`source` 取值仅限 6 个，与 agent-runtime `profiles/roles/` 里已交付的 12 个
`dr-*` 中的 6 个 worker 子集一一对应：

| source        | role                    |
| ------------- | ----------------------- |
| `code-local`  | `dr-worker-code-local`  |
| `code-remote` | `dr-worker-code-remote` |
| `wiki`        | `dr-worker-wiki`        |
| `feishu`      | `dr-worker-feishu`      |
| `content`     | `dr-worker-content`     |
| `web`         | `dr-worker-web`         |

约束：

- 映射落地为一个纯 `SOURCE_ROLE` dict（库函数常量），dispatch 只读它，不做内联 if。
- **绝不新造、改名、重注册角色**：`SOURCE_ROLE` 的 value 必须逐字等于上表 6 个已存在 role 名。
- 未知 / 缺失 `source` 的 clue 属 clue 级降级（回填默认源），**绝不 fault 整图**。

## 契约接线（沿用 dr-worker 既有协议，不改 agent-runtime）

6 个 dr-worker 角色共用同一对协议（agent-runtime profile 已声明，本单不动）：

- input：`deep-research.worker-input/v1`（`schemas/worker-input.v1.json`）
  必填 `clue_id`、`clue_text`；可选 `depth`、`sources`（string array）、
  `revision`、`allowed_root`。
- output：`worker.result.v1`（`schemas/worker-result.v1.json`）
  `evidences[{quote,claim,source,locator,revision,...}]`、
  `proposed_clues[{clue,reason}]`、`materials[{uri,digest}]`。

dispatch 为每个 worker run 落 input 文件（`deep-research.worker-input/v1` 形状）：
含 `clue_id` / `clue_text` / `depth`，并 `sources: [<clue.source>]`。collect 消费
`worker.result.v1`，把 `evidences[]` 归一化为 R1 evidence（finding 保留
`claim` / `source` / `quote` / `locator`），逐条 append `evidence.jsonl` 并经
`research.evidence.v2` best-effort 发布（复用 R1 的 `_append_evidence` /
`_publish_evidence`，不改变 research_bus.py 语义）；`proposed_clues[{clue,reason}]`
进入 harvest 生成子线索。synthesis 维持 `research_synth` 不变（R2 不动）。

## clue 源归属与身份

- clue 板每项新增 `source` 字段（∈ 6 词汇），dispatch / collect / harvest 全程携带。
- `derive_clue_id(text, source=None)`：`source=None` 时按 `text` 内容寻址
  （向后兼容，不破 R1）；有 `source` 时按 `text|source` 内容寻址 —— 同一题面
  从不同源探查，clue id 不互相顶撞。
- seed 节点输出含 `source` 标注的 clue：接受纯字符串（回填默认源）或
  `{"text","source"}` 对象数组；seed prompt 枚举 6 源词汇并要求标注。
- `ResearchConfig` / `ResearchDeps` 新增 `sources: list[str]`（矩阵词汇，
  默认固定顺序 `["code-local","code-remote","wiki","feishu","content","web"]`），
  取矩阵首元素为默认源，供回填与 seed prompt 使用。

## 修复「web 派发数恒为 0」

根因：dispatch 用单一 `WORKER_ROLE`，从不读 `clue.source`，故 web 及默认源之外的
所有源派发数恒为 0。修复即按上节契约按 source 分流；验收强制一次 run 内 web ≥ 1。

## 验收判据（机器可判定）

一次 run 的 agent-runs 覆盖 ≥ 3 种 source，且其中 web ≥ 1 次。验收命令必须把
覆盖 source 集合与 web 计数打印出来。

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_source_coverage.py
```

`scripts/check_research_source_coverage.py`（新脚本）必须是可机器判定的覆盖检查：
用 fake seed 产出含 `source` 标注、跨 ≥ 3 源（必须含 `web`）的 clue，用记录每次
派发 `spec.role` 的 fake launcher 跑完一次 `run_research`，打印两行可解析行：

```
sources={code-local,wiki,web}
web=1
```

第一行是本次 run **实际派发到的去重 source 集合**（按字母序）；第二行是 web 的
派发次数。exit 0 当且仅当 `去重数 ≥ 3 且 web ≥ 1`，否则 exit 1。

配套单测（`make verify` 覆盖）必须包含：

1. `SOURCE_ROLE` 逐字等于 6 个已交付 role 名（不新造角色）;
2. dispatch 按 `clue.source` 选 role（fake launcher 记录 `spec.role`）;
3. collect 正确消费 `worker.result.v1` 的 evidences / proposed_clues，并断言
   worker input 文件是 `deep-research.worker-input/v1` 形状（含 `sources`）;
4. `derive_clue_id(text, source)`：同 text 不同 source 得不同 id，`source=None`
   与 R1 一致;
5. 未知 source 回填默认源、不 fault 整图。

## 非目标 / 边界

- 不建新 role、不注册新 kind、不改 agent-runtime 任何角色 / 协议 / schema。
  实现者若认定 agent-runtime 必须改动，先经 board:dd-talk 知会，不直接动手。
- 不改 ingest / spool / 转写链路：web「发现 URI」与 content「读转写」之间的
  分工超出本单，本单只做 dispatch 路由。
- 不改 synthesis、不改 R1 的 clue / evidence / doc 发布语义与幂等键。

## Delivery 约束

业务代码与 review 全部交给 dev-dispatch。git 检查、H0 构造、acceptance 执行全部
在独立 `/data/worktrees/` 工作树中进行；生产主 checkout 不 checkout / switch /
reset / 建分支。