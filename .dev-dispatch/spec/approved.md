# R8-fix：research 图的角色入参必须照真 schema 构造，并由判据机械核验

> 派单人 = 监督面（cc-supervisor）。归属线 wf-66300e（该线 terminal=done，本单由监督面直接派）。
> target_base = origin/main（f808161），派单时由 dd 冻结。

## 触发事实（真机取证，不是推断）

R8 交付物 B 的真机冷启动 run（`/data/fleet-graph/research/r-a6299436f462`）以
`terminal: fault` 收尾，`terminal_reason: "debate/arbiter run 3f00ccb9 结束于 lost"`，
`wiki.placed: false / reason: "no report.md"`，进程 exit 1。

arbiter 的 `agent-runs/3f00ccb9-.../launcher.stdout` 逐字：

```
state=failed  exit_code=91  exit_reason=config_error  duration_seconds=0  role=dr-arbiter
AGENT_RUN_ERROR code=CONTRACT_ERROR detail=
  board_stats: missing required field: zero_growth_rounds;
  clue_titles: [0..6]: expected object, got string;
  recent_claims: [0..68]: expected object, got string
```

`duration_seconds: 0` —— **一次 LLM 都没调**，在 agent-run 的入参校验就被拒。

逐字段对照（契约 = agent-runtime `profiles/roles/schemas/arbiter-input.v1.json`；
实发 = `src/fleet_graph/graphs/research_pipeline.py` 的 `_arbiter_node`，约 1206-1221 行）：

| 字段 | 契约 | 实发 |
|---|---|---|
| `board_stats` | `required: [zero_growth_rounds]`，`additionalProperties: false`，允许键 = `clues_total` / `clues_explored` / `clues_pending` / `clues_dropped` / `evidence_total` / `evidence_added_last_round` / `zero_growth_rounds` / `rounds_elapsed` | `{total, done, blocked, open}` —— 必需键缺失，且四个键无一在允许集内 |
| `clue_titles[]` | `object{clue_id, title, status?, depth?}` | 纯字符串数组 |
| `recent_claims[]` | `object{claim, clue_id?, round?}` | 纯字符串数组 |
| `recent_rounds` | 顶层 `additionalProperties: false`；轮次在契约里的位置是 `board_stats.rounds_elapsed` | 作为顶层键发出 |

`research_pipeline.py:111-112` 有一段注释写着这个 payload 的形状——**代码照着自己的注释写，
从未照真 schema 校验过**。这不是偶发：**确定性缺陷，每一次走到 arbiter 的重档 run 都会死在这里。**

## 为什么判据没拦住（这才是要根治的部分）

R4 的判据 `scripts/check_research_debate.py` 用 **fake launcher** 回放四角色，
fake 喂进去的永远是合法信封，所以「roles={advocate,arbiter,judge,opponent} 齐」判绿，
而四个角色的**入参从未被 agent-run 按契约校验过**。
R8 交付物 A（`check_research_coldstart.py` 自检）是同一个盲区：hermetic、FakeLauncher。

**两个已宣告 DONE 的里程碑栽在同一处：判据在图内自说自话，不碰座位层的契约。**

## 交付（三项，缺一不可）

1. **修 `_arbiter_node` 的入参构造**，逐字段对齐 `arbiter-input.v1.json`：
   - `board_stats` 用契约的键；`zero_growth_rounds` 必须真实取自图状态（不是填 0 应付校验）；
     现有的 total/done/blocked/open 语义映射到 `clues_total` / `clues_explored` / `clues_pending` /
     `clues_dropped`，`recent_rounds` 并入 `board_stats.rounds_elapsed`；
   - `clue_titles` 改为 `{clue_id, title}` 对象数组（有 status/depth 就带上）；
   - `recent_claims` 改为 `{claim, clue_id?, round?}` 对象数组。
2. **把同族的其它腿全部核一遍**：advocate / opponent / judge / 各 `dr-worker-*` / seed / synthesis
   —— 凡是本图构造、交给 agent-run 的入参，逐个对照其 role 声明的 input schema。
   这次只是 arbiter 先撞上；**不许只修 arbiter 就交卷**。发现的其它不符一并修。
3. **新增判据脚本 `scripts/check_research_role_contracts.py`**，并接进 `make verify`：
   - 对本图会构造的**每一种**角色入参，用**真 schema** 做校验；
   - **schema 必须从 agent-runtime 的 roles 仓读取**（经 role yaml 的 `input.schema` 解析路径），
     **严禁在本仓手抄一份**——手抄即制造第二个 SSoT，正是本缺陷的同族病根；
   - 脚本自带自检：至少一条**阴性 fixture**（例如把 `clue_titles` 退回字符串数组），
     必须判红。判据脚本不能被证明会红 = 判据无效。

## 硬线

- **不许放宽契约**：不得修改 agent-runtime 的 schema 去迁就本图的现有形状。
  方向是图去满足契约，不是契约来迁就图。
- **不许用 fake launcher 顶替交付 3**：fake 喂什么都合法，正是它让这个 bug 活到今天。
  交付 3 的价值全在于「校验的是真 schema」。
- **不许手抄 schema 进本仓**。若跨仓读取路径不方便，把「怎么定位 roles 仓」做成可配置，
  而不是复制文件。
- `zero_growth_rounds` 等字段要**取真实值**；用常量占位骗过校验属于第九式，判红。

## 验收

```dd-acceptance
uv sync --frozen
make verify
uv run python scripts/check_research_role_contracts.py
```

## 备注

修完合入后，监督面会重跑 R8 交付物 B（真机冷启动 run）作为最终验证——
**本单不负责跑那次 run**，只负责让 arbiter 这条腿在真实契约下能起来、并且此后有判据守住。
