# deep-research（统一入口 skill）

本 skill 是 deep-research 的**人可调 skill 面**（宪法条8「使用闭环」：MCP tool +
skill + CLI 三面）。它与 CLI 子命令 `fleet-graph research run`、MCP tool
`research_run` 指向**同一个路由**——`fleet_graph.research_entry.run_research_ticket`，
路由判定是确定性纯函数（`resolve_tier`）。三面只此一套入口，不各写各的入口
（宪法条6「入口唯一」）。

## 调用方式

用统一入口发起一次 deep-research：

```bash
# 轻档（显式）
fleet-graph research run --question "<问题>" --tier light

# 重档（显式）
fleet-graph research run --question "<问题>" --tier heavy

# 或让确定性规模判定分档（scale >= 4 -> heavy，否则 light）
fleet-graph research run --question "<问题>" --scale <规模>
```

编程调用面（等价路由）是 MCP tool `research_run`（`fleet-graph research serve`
:5612）。三面共享同一 runner：同输入恒得同档位，产物同 schema（report +
anchor 元数据），仅 bounds 不同。

## 产物归位

终验 report 落 wiki 域 `DeepThought/<topic>/`（遵 wf-3f87f3 命名纪律：
`<date>-<topic>.md` + `anchor-check.json`），run_root 仍保留中间态
（evidence.jsonl 等），归位在 finalise 侧、不破坏 R1 双源对账。wiki 根取
`FLEET_GRAPH_WIKI_ROOT`（缺省 `/data/vault`）。

## 边界

- 三面都是 surface，底层仍走既有 research runner + 12 个 `dr-*` 角色，不新造角色。
- 路由与归位只在入口/finalise 侧，不触碰 `converge()` 的路由语义。
- 不指向老 loop-engine 的 `bin/deep-research.sh` / drain 入口（那些路径已退役）。
