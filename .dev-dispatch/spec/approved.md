# R2-fix：collect 节点续约 worker.result.v1 —— 去掉不存在的 verdict 门

## 背景与根因

R2（#171 = `281a0a4`）已交付多源 worker 矩阵并部署（release `856e21c6`），但运行时判据
「一次 run 的 agent-runs 覆盖 ≥3 种 source 且 web ≥1」仍未兑现。真机探针 run
`r-641961813565`（seed 正确产出 web/wiki/code-local 三源种子）暴露三个联动症状：

1. **code-local 种子（`c-f58c190f8a27`）自始至终未被 dispatch**：三轮 coverage 恒 0。
2. **web/wiki 各自返回 `state=succeeded` + 非空 `structured_result.evidences`**
   （web r0 6 条证据+2 子线索、web r1 4 条+3 子线索、wiki r0 8 条+4 子线索），但
   全部被 collect 判为 clue 失败 → retry → blocked（terminal_reason =
   「1 个 clue retry 耗尽 blocked，其余 0 个 done」）。
3. **synthesis 收到空 findings**：`inputs/synthesis.json` 的 `clue_ids: []`，
   `synthesis.json` report 自述「manifest 中 clue_ids:[]、prompt 不含任何 finding 条目」。

根因（已在已部署 release `856e21c6` 与 agent-runtime SSoT `ed18eead2ef0` 两侧取到原文）：

`fleet_graph/graphs/research_pipeline.py` 的 `collect` 节点把「调查完成」的判据写死成

```python
parsed.get("verdict") in WORKER_VERDICTS_DONE   # {"found", "not_found"}
```

但 agent-runtime roles 仓 SSoT `profiles/roles/schemas/worker-result.v1.json`
（`dr-worker-*` 六个角色统一输出的契约）**根本没有 `verdict` 字段**：

- 顶层 `required = ["evidences", "proposed_clues", "materials"]`；
- 无 `verdict`、无 `clue_id`。

`verdict`（`found/not_found/blocked` 三值）是旧角色 `research-worker.result.v1`
（`research-worker-result.v1.json`）的字段。R2 从旧 `research_worker_local` 迁到
`dr-worker-*` 时，实现侧把 collect 的完成判据仍留在旧契约的 `verdict` 上，
`research_pipeline.py:79-80` 的契约注释也一并把 `clue_id, verdict` 误写进
worker.result.v1；`tests/test_research.py`、`tests/test_research_sources.py` 与
`scripts/check_research_source_coverage.py` 的 fake payload 同样编造了
`verdict`/`clue_id`，才让 `make verify` 全绿而真机失败。

后果链：每个成功返回非空 evidences 的 worker run 都被当成 clue 失败 →
`_append_evidence` / `_publish_evidence` 从不执行 → `evidence.jsonl` 不产生、synthesis
语料空；无 clue 变 done → coverage 恒 0 → `zero_growth_rounds` 打到 3 → converge 提前
partial（web retry 耗尽 blocked），把排在后面的 code-local 挤掉。空 `clue_ids` 与
empty findings 是**流水线缺陷**（collect 完成判据与实际契约脱节），**不是**探针措辞副
作用——web/wiki 已各自返回非空 evidences，证得 worker 侧取证与收敛都在正常产，卡点在
collect 的消费侧。

## 修复（仅改 fleet-graph 仓，不碰 agent-runtime / agent-bus）

1. `collect` 节点：把完成判据从
   `parsed.get("verdict") in WORKER_VERDICTS_DONE` 改为「信封是合法 worker.result.v1」：
   `isinstance(parsed, dict) and isinstance(parsed.get("evidences"), list)`。
   - evidences 非空 = found；evidences 空 = not_found。二者都属「调查完成」→ clue
     `done`，皆不触发 retry，coverage 随 done 数增长，zero-growth 不再被误触。
   - 「工具面不可用 / blocked」不再依赖 worker 自报字段（新契约无此字段）：改由
     run 失败（`status.ok` 为假）、信封解析失败、或 wait 超时触发，仍走现有
     retry/block 路径（`retry >= MAX_RETRIES → blocked`），语义不变。
2. 删除 `WORKER_VERDICTS_DONE` 常量、其注释（`research_pipeline.py:86-87`）与
   `__all__` 导出；把 `:77-80` 的 worker.result.v1 契约注释更正为实际 SSoT 形状：
   `{evidences[{quote,claim,source,locator,revision,range?,uri?,digest?}],
   proposed_clues[{clue,reason}], materials[{uri,digest?}]}`（无 verdict / clue_id）。
3. 修正测试与验收脚本的 fake payload，产出**真实** worker.result.v1 形状（去掉
   `verdict`、`clue_id`）：
   - `tests/test_research.py`、`tests/test_research_sources.py`：`worker_payload`
     不再编 `verdict`/`clue_id`；把 `blocked_worker_result`（编 `verdict=blocked`）
     改为经「信封解析失败 / run 失败（status.ok 为假）」触发 blocked 的用例；新增
     回归测试——**无 verdict 的 worker.result.v1（evidences 非空）必须判 done、
     coverage 增长、evidences 落 `evidence.jsonl` 并随 synthesis 投递（synthesis
     input `clue_ids` 非空）**。
   - `scripts/check_research_source_coverage.py`：`worker_payload` 同样去掉
     `verdict`/`clue_id`，使覆盖检查真走修复后的 collect 路径。

## 验收判据（机器可判定）

一次 run（fake seed + fake launcher，无 verdict 的合法 worker.result.v1）覆盖 ≥3 种
source 且 web ≥1；无 verdict 的 evidences 被判 done 并流入 synthesis。

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_source_coverage.py
```

## 非目标 / 边界

- 不改 agent-runtime 的 `worker.result.v1` schema / 角色 / persona（SSoT 在 roles 仓，
  本单不跨仓；实现者若认定 SSoT 必须改，先 board:dd-talk 知会，不直接动手）。
- 不改 research_bus.py 的发布语义与幂等键；不改 synthesis / seed / harvest / converge
  的既有语义。
- 不新增 role、不注册新 kind。

## Delivery 约束

业务代码与 review 全部走 dev-dispatch。git 检查与 acceptance 执行全部在独立
`/data/worktrees/` 工作树；生产主 checkout 不 checkout / switch / reset / 建分支。