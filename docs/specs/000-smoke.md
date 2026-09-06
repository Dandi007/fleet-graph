# 000-smoke · runner handshake

> 本文是 loopx-line-runner（`lxr`）与各 peer agent 之间的**交接契约**的 SSoT。
> 它描述一次 agent turn 从派发到收尾的握手形态：peer 如何声明「做完了/做不了」，
> runner 又如何在收到声明后落地机械动作。后续每一张 DD 都在这份契约上运行。

## 1. 为什么需要握手

runner 本身不写代码、不做裁量。它只做两件事：

1. 派生（dispatch）一个 peer agent 到某个 todo 上，给一份含角色、材料、spec、历史句柄的 prompt；
2. 在 agent 结束后，读取其**最后一条输出**（Stop JSON），并按它执行机械动作（commit 残留、push、
   核 `HEAD == origin`、跑程序化验收、路由到下一阶段或打回）。

agent 的自由裁量（写什么码、怎么改、给什么 review 结论）与 runner 的机械动作之间，唯一的结构化接口
就是这条 Stop JSON。没有它，runner 无从判断「这一轮有没有产出、该不该验收、该把球传给谁」。

## 2. 管道与五个角色

一条 DD 走完四个阶段，每个阶段是一次有界 agent turn + 机械动作：

```
plan → implement → review → approve →（机械）merge
```

| 阶段 | 谁做（peer 角色） | agent 产物 | runner 收到 Stop 后 |
|---|---|---|---|
| `plan` | planner | 判断线是否完成 / 派下一批 DD | 派单落成 `[dd:]` todo + DD state；或标记 done/blocked |
| `implement` | implementer | 在 worktree 写码并 commit | 跑验收；通过 → review，失败 → 同 todo 带 feedback 续跑 |
| `review` | reviewer（只读） | pass / fail + findings | pass → approve；fail → implement rework（`claimed_by` 原 implementer） |
| `approve` | planner（Goal 审单） | approve / reject | 落后则 rebase + 复跑验收；`gh pr merge`；删 worktree/分支 |

## 3. Stop JSON 的形态

每个 agent turn 的最后一条输出**必须**是恰好一个 JSON 对象、单独一行、无代码围栏、无前后 prose：

```text
{"schema":"<role>/<version>","stop":"<verb>","...其余角色字段..."}
```

要点：

- `schema` 字段标明角色与协议版本（如 `impl/1`），是 runner 识别握手对象的唯一凭据。
- 除 `schema` 与 `stop` 外的字段随角色不同（下表给出）。
- runner 侧用 `extract_protocol_object(text, schema_prefix)` 提取：从 agent 的 stdout 里扫描所有
  `{...}` 候选，取**最后一个** `schema` 以该前缀开头且能 `json.loads` 通过的对象；无候选时回退取
  「最后一个能平衡解析的 JSON 对象」。agent 输出本就有大量工具轨迹与 prose，所以契约锚定的是
  「满足 schema 前缀的最后一个对象」，而不是「整段输出只有一个 JSON」。

### 3.1 四个 schema

**`impl/1`（implementer）**

```json
{"schema":"impl/1","stop":"committed","summary":"改了什么、为什么、怎么验证的"}
{"schema":"impl/1","stop":"failed","detail":"为什么做不了"}
```

**`review/1`（reviewer）**

```json
{"schema":"review/1","stop":"pass","summary":"…","findings":[{"severity":"note","file":"a.py","line":12,"detail":"…"}]}
{"schema":"review/1","stop":"fail","summary":"…","findings":[{"severity":"blocker|major|minor|note","file":"…","line":0,"detail":"…"}]}
```

约束：`stop == "fail"` 时 `findings` 至少一条 `blocker` 或 `major`，否则视同无理由打回（runner 按 pass 处理，
并在 summary 前缀注明）。`note` / `minor` 可以随 pass 一起给，implementer 下一轮会看到。

**`goal.review/1`（approve，Goal 审单）**

```json
{"schema":"goal.review/1","stop":"approve","summary":"…"}
{"schema":"goal.review/1","stop":"reject","message":"必填：要 Implementer 改什么"}
```

**`plan/1`（planner，Goal 派单）**

```json
{"schema":"plan/1","stop":"dispatch","summary":"…","todos":[{"title":"…","priority":"P0|P1|P2","spec":"…","acceptance_extra":["…"]}]}
{"schema":"plan/1","stop":"done","summary":"…"}
{"schema":"plan/1","stop":"blocked","summary":"…","blocked":{"kind":"needs_human|external|contradiction","detail":"…"}}
```

约束：`dispatch` 时 `todos` 非空且每条含 `title` + `spec`，最多 `max_plan_todos` 张；`priority` 只认 `P0|P1|P2`。

## 4. runner 收到 implement Stop 后的机械动作（顺序）

1. `commit_leftovers` + `push` + 核 `HEAD == origin`（`worktree_status`），记下轮后 head。
2. 若 agent 没正常结束或没给出 Stop（`res.ok == False` 或 `proto is None`）→ 写入 `pending_feedback`
   （来源 `runtime`），把上一轮运行时状态与 stderr 尾段交给 implementer 从当前 worktree 继续。
3. 若 `stop == "failed"` → DD 状态置 `failed`，PR 评论 + 关闭，删 worktree，todo 记为 `FAILED`，spend。
4. 若 `head == base`（分支上没有任何超出 spec 文件的提交）→ feedback：报 committed 但无提交，这一轮算 `no_progress`。
5. 否则跑程序化验收（`make verify` + `acceptance_extra`）。不通过 → feedback（来源 `acceptance`，带各命令 exit 与 tail），
   PR 评论，`no_progress`，下一轮同 todo 续跑。
6. 通过 → 状态 `awaiting_review`，todo 完成并派下一张 `review` todo（`next_excluded` 掉本 implementer）。

## 5. review / approve 的收尾

- review `fail`（带 blocker/major）→ 状态 `open`，`pending_feedback` 记 findings，下一张 `implement` rework
  todo，`claimed_by` 原 implementer。
- approve `reject` → `_rework`：走 `implement` rework。
- approve `approve` → 若 PR 落后 release：rebase，复跑验收（失败则打回 rework）；通过则 `gh pr merge`、
  删 worktree/分支、状态 `merged`。merge 机械失败 3 次 → 转人工 gate。

## 6. 活性规则（liveness）

长时间只调工具、不产出任何文字会被 runner 侧判定为「卡死/失活」。因此每个 agent turn 开工前必须先
**用一句话说明计划**，作为 runtime 活性的证据；随后才是工具调用与最终 Stop JSON。

## 7. 边界

- 只有 CR 一道 reviewer；FR 由 approve 前的程序化验收 + Goal 审单顶替。
- reviewer 只读：不能改代码、不能跑命令（`write=False`、`--write` 不传），验收由 runner 跑并注入。
- 引擎状态在 `.loopx/` `.codex/`（已 git exclude），peer 不直接读写这些文件。