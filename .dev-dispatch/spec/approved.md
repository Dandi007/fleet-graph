# R1 — 协议与过程可回放：clue / evidence / doc 中间态落 agent-bus append-only

## Goal

把 deep-research 图（`fleet_graph/graphs/research_pipeline.py`）的 **clue / evidence / doc**
三类中间态落到 agent-bus append-only（**沿用已注册协议** `research.clue.v2` /
`research.evidence.v2` / `research.doc.v2`），`evidence.jsonl` 降为**本地镜像**。
consumer 侧 payload schema 必须**从 bus registry 派生或机械校验**，严禁手抄 allowlist。

## Authoritative Facts

1. 三种协议已在 bus registry 注册（`registered_by=uther-tui`）：
   - `research.clue.v2`：**root entity，版本链**（`supersedes`），字段
     `{text, status, depth, sources, parent?, assignee?, run_id?, rationale?}`，status
     状态机 `proposed|open|in_flight|explored|dropped|blocked`。
   - `research.evidence.v2`：**leaf**，`{clue_id, anchor, quote, claim}`（anchor 为带版本 URI）。
   - `research.doc.v2`：**leaf**，`{doc_kind, digest, body, origin}`（digest 为全局去重键）。
   - 本开发**沿用**，不重注册新 kind。仅当机械发现现协议不可用时才考虑新底座重注册，
     且必须走公示流程（board:dd-talk 公示 + 异议窗口），不得静默注册。
2. schema 的 SSoT 是 bus registry（`GET /v1/protocols`）。consumer 对 payload 的校验
   必须**在运行时从 registry 响应派生**（用 `jsonschema` 校验 `payload_schema`），或对
   registry 返回的 `schema_digest` 做机械核验比对。**禁止**在仓库里手抄一份 schema /
   allowlist 再与之比对（继承 wf-3f87f3 C4 §5j 教训）。
3. 现状差距：`research_pipeline.py` 的中间态只落 run root 本地文件
   （`clues/<id>.json`、`evidence.jsonl`、`report.md`），不进 bus。`ResearchDeps` 已有
   `observe` 注入接缝，测试用 fake `text_node` / `launcher` / `observe` 全端口注入。

## Change

1. `src/fleet_graph/bus/client.py`：加 registry 读取接口
   （`GET /v1/protocols` → `{kind: {payload_schema, schema_digest, entity_role}}`），
   暴露 `protocols()` / `get_protocol(kind)`；复用现有 `transport` 接缝，测试注入 fake。
2. `ResearchDeps` 增一个 `publisher` 端口（协议上等价 `BusClient.publish`，含
   `entity_id` / `supersedes` / `idempotency_key`）；`research_runner.build_research`
   生产装配真实 `BusClient`，测试注入 fake transport。
3. `research_pipeline.py` 节点在**写本地文件的同时**发布三类实体到一个研究专用 channel，
   沿用老引擎 channel 约定：
   - clue 状态迁移（open → dispatched → done | blocked）→ `research.clue.v2`
     发布到 `research:{research_id}.index`（root，同 clue 用稳定 `entity_id` 版本链）。
   - 每条 finding → `research.evidence.v2` 发布到 `research:{research_id}.evidence`
     （leaf，`clue_id` 指 clue 的 entity_id）。
   - synthesis 报告 → `research.doc.v2` 发布到 `research:{research_id}.docs`
     （leaf，`doc_kind=report`，`origin=research_id`，`digest` = 正文内容寻址）。
4. 发布**幂等**：同一 run 同一中间态用确定性 `idempotency_key`（由 run/clue/finding 内容
   寻址派生），kill-restart 重派不产生重复实体。发布失败**只降级记录、绝不 fault 整图**
   （与 `observe` 同义，可观测性不能拖垮它观测的工作）。
5. `evidence.jsonl` / `clues/*.json` / `report.md` 继续落盘作为**本地镜像**；新增
   **双源 diff 检查**：读本地镜像 + 读 bus 实体（clue / evidence / doc 三类各一），逐条对账，
   机器可跑（库函数 + 测试），全绿 = 两边一致。
6. 新增**从 bus 回放**能力：给定 `research_id`，从 bus 读 clue 版本链 + evidence + doc，
   重建完整过程轨迹（clue 状态迁移、每轮 dispatch、每条 evidence、最终 doc），供
   kill-restart 后与本地镜像 / `result.json` 核对。

## Acceptance

```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_research_bus.py -q
```

Acceptance 测试（`tests/test_research_bus.py`，一律用 fake transport 注入、**不写真实 channel**）须证明：

1. **三类中间态确实发布**：一次 fake run 后，fake transport 记录里出现
   `research.clue.v2` / `research.evidence.v2` / `research.doc.v2`，kind / 字段 /
   channel 命名与本 spec 约定一致；clue 的版本链（`supersedes`）正确。
2. **双源 diff 检查可跑且绿**：本地 mirror（`evidence.jsonl` / `clues/*.json` / `report.md`）
   与 bus 实体逐条对账一致，diff 检查器 exit 0；人为制造不一致（删一条 evidence）时 exit 非零。
3. **consumer schema 从 registry 派生**：payload 校验用的 schema 来自 registry 读取结果
   （fake transport 的 `GET /v1/protocols` 响应）；测试改写 registry 返回后校验行为随之改变
   （证明未手抄固定 schema / allowlist）；若检测到硬编码 schema 则 fail。
4. **kill-restart 从 bus 完整回放一次 run**：部分节点完成后中断 → 从 checkpoint resume →
   经 bus 回放器重建完整过程轨迹，与本地镜像 / 最终 `result.json` 一致，且无重复实体（幂等）。

## Non-goals

不创建 / 不注册新 protocol kind；不在真实 channel 写测试脏物（测试一律 fake transport，遵循
元宪法「bus append-only 敬畏」）；channel 置备（create-or-reuse）的部署面归 R7 preflight；
不做 R2–R8；不部署、不启动生产 unit；不碰他线泵 / 生产网关 / 生产主 checkout。