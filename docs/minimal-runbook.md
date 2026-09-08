# 最小系统 Runbook（fleet-graph-minimal）

怎么把一个 goal 送进最小系统、怎么观测它、怎么停 / 续。协议正本是
`docs/specs/minimal/protocol.md`（下称 §N），工具与字段的实现正本分别是
`src/fleet_graph/minimal/mcptools.py` 的 `TOOLS` 声明表与 `enroll.py`，本页只做转述。

## 1. 起 MCP（唯一常驻服务）

```bash
# 必须从仓 checkout 根运行（见下面第 6 节「为什么必须从仓根运行」）
uv run fleet-graph-minimal-mcp --engine-root /data/fleet/goals

# 等价写法：引擎根走环境变量（--engine-root 缺省回退它）
FLEET_ENGINE_ROOT=/data/fleet/goals uv run fleet-graph-minimal-mcp
```

- 传输：stdio 上的 JSON-RPC 2.0，一行一个 JSON；只实现 `initialize` / `tools/list`
  / `tools/call` 三个方法。
- 引擎进程**不是**人起的：`goal_enroll` / `goal_resume` 会让 MCP spawn
  `fleet-graph-minimal-engine --goal-id <id> --engine-root <root>`（GO-10）。
  人工直接起引擎只有在前两者不方便时才用。
- systemd 模板：`deploy/systemd/fleet-graph-minimal-mcp.service`（只是模板，
  本仓不 enable、不 start、不装安装脚本；引擎根经 `Environment=` 给）。

## 2. 九个 MCP 工具（名字与入参照 `mcptools.TOOLS` 声明表）

| 工具 | 读 / 写 | 入参（粗体必填） | 做什么 |
|---|---|---|---|
| `goal_enroll` | 写 | **`enroll`**（object，§1 的 goal.enroll/2 对象）；可选 `goal_id` | 校验 enroll → 程序化准备 → spawn 引擎进程 |
| `goal_list` | 读 | 无 | 每个 goal 一行：id / title / state / step / warnings / pid … |
| `goal_status` | 读 | **`goal_id`**（string）；`tail`（integer，默认 20） | 派生状态 + 最近 N 条 event |
| `goal_events` | 读 | **`goal_id`**（string）；`since_seq`（integer，默认 0） | 原始 event 流（seq 大于 since_seq） |
| `goal_message` | 写 | **`goal_id`**（string）、**`text`**（string） | 写 control `{op:"message"}`，进 Goal Agent 下一个 turn |
| `goal_steer` | 写 | **`goal_id`**（string）、**`patch`**（object） | 写 control `{op:"steer"}`，`goal_version` +1 |
| `goal_stop` | 写 | **`goal_id`**（string）、**`mode`**（string：`graceful` \| `kill`） | graceful 写 control `{op:"stop"}`；kill 见第 5 节 |
| `goal_resume` | 写 | **`goal_id`**（string） | 对 stopped / blocked / crashed 的 goal 重新 spawn 引擎 |
| `goal_observations` | 读 | **`goal_id`**（string）；`since_ts`（string）、`severity`（string） | 读书记员 L1（`observations.jsonl`） |

读只碰该 goal 的 `events.jsonl`（`goal_observations` 读 `observations.jsonl`）；
写只做两件事：往该 goal 的 `control.jsonl` 追加恰好一行，或 spawn 引擎进程。
MCP 与引擎之间没有别的通道（§10）。

## 3. 送一个 goal 进去：`goal.enroll/2` 样例

一个 `tools/call`（`goal_enroll`）的完整请求，enroll 对象的字段以 protocol §1 与
`enroll.py` 为准——顶层恰好六键，`goal_id` 不在请求里（MCP 生成 `g-` + 6 位十六进制）：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "goal_enroll",
    "arguments": {
      "enroll": {
        "schema": "goal.enroll/2",
        "work_folder": "wf-ab12cd",
        "title": "把 X 功能做出来",
        "goal_text": "……自然语言目标，含完成定义……",
        "source_branch": "release/loopx-minimal",
        "repos": [
          {
            "path": "/data/worktrees/foo",
            "remote": "git@github.com:org/foo.git",
            "target_branch": "main",
            "acceptance": ["make test"]
          }
        ]
      }
    }
  }
}
```

- `work_folder`：有就传 folder_id（`wf-` + 小写字母数字）；传 `null` 则 MCP 经
  work-folder MCP 新建，topic 用 title。
- `repos[]` 每项恰好四键：`path`（本地 worktree 路径）、`remote`（必有，GO-32）、
  `target_branch`（每 repo 可不同）、`acceptance`（该 repo 的验收命令，至少一条）。
- 已废弃键（`goal_path` / `sessions` / `warn` / 单 `repo`）会被字段级报错拒绝。
- 校验通过后 MCP 自己做程序化准备（建引擎根目录、写 `goal.enroll.json`、落首条
  event、准备 release 分支），再 spawn 引擎，返回 `goal_id` 与 `work_folder`。

## 4. 引擎根目录布局

引擎根（`--engine-root` / `FLEET_ENGINE_ROOT`，默认 `/data/fleet/goals`）下每个
goal 一个目录（`<goal_run_root>`）：

```
/data/fleet/goals/
└── g-7f3a2c/                 # <goal_run_root>，goal_id 命名
    ├── goal.enroll.json      # enroll 对象原样（引擎只核 schema 键）
    ├── events.jsonl          # 唯一状态来源；恢复 = 回放（§11）
    ├── control.jsonl         # MCP 写入的操作（message / steer / stop）
    ├── sessions/             # agent-runtime 的 session 文件（按 run_id）
    ├── worktrees/
    ├── dd/
    ├── observations.jsonl    # 书记员 L1
    └── engine.log            # 引擎进程的 stdout / stderr（spawn 时重定向）
```

## 5. 观测、stop 与 resume

**观测**（全是只读）：

- `goal_list`：所有 goal 一行一个，先看这个。
- `goal_status`：单个 goal 的派生状态 + 最近 N 条 event（`tail` 默认 20）。
- `goal_events`：增量拉原始 event 流（`since_seq`）；event 的 kind 全集见 §8。
- `goal_observations`：书记员 L1，可按 `since_ts` / `severity` 过滤。
- 直接 tail：`tail -f /data/fleet/goals/g-7f3a2c/events.jsonl`——MCP 读的和这
  是同一个文件，日志与真相不分叉（§11）。
- `state` 枚举（由 events 派生）：`running | stopped | blocked | done | crashed`；
  `crashed` = 最后一条 event 不是终态且引擎进程不在。

**stop**：

- `graceful`：写 control `{op:"stop"}`；当前 agent 跑完即 `engine.exiting(stop)`，
  不起下一个。
- `kill`：协议语义是对引擎进程组 SIGTERM、立即退出、在跑的 agent run 视为丢失
  （§11）。当前 stdio 服务器尚未实现发信号：工具层明确拒绝 `kill` 并返回
  -32602（信号属传输层职责），要强杀先直接对 pid 用 `kill`。

**resume**：

- `goal_resume` 对 `state ∈ {stopped, blocked, crashed}` 的 goal 重新 spawn 引擎；
  恢复 = 回放 `events.jsonl`（不存 checkpoint、不从 checkpointer 续跑，§11）。
  blocked 的 goal 通常先 `goal_message` 说明再 resume。
- **崩溃不自动 resume**（§10 末段）：MCP 自身重启时，扫所有 goal，`running` 但进程
  不在的按 `crashed` 处理，**不自动 resume**，只在 `goal_list` 里标出来，等人或
  外部观测面调 `goal_resume`。崩溃后自动重启会掩盖问题，与 GO-7.3「让外部发现
  问题」相反。

## 6. 为什么必须从仓根运行

六个角色 harness profile（`profiles/harness/minimal-{goal,impl,cr,fr,merge,scribe}.json`）
由 `harness.load_profile(name, root=...)` 按**仓内相对路径**
`<root>/profiles/harness/<name>.json` 定位；wheel 只打包 `src/fleet_graph`，不带
`profiles/`。所以 MCP / 引擎都必须从仓 checkout 根运行（或保证 `root` 指向
checkout），否则引擎起 agent 时找不到 profile。

## 7. 已知缺口（protocol §9，属 agent-runtime 外部依赖，非本仓可修）

按 §9 的需求清单，agent-runtime（`agent-run`）今天还缺三项，引擎侧只能等待上游：

1. **`--output-schema`**：runtime 现有 `--structured`、无 `--output-schema`。
   缺它则「输出符合 schema 的校验、喂错重试、失败非零退出」（§0.2）没有 runtime
   兜底层。
2. **profile 的 `hooks`**：harness profile 没有 `hooks` 字段；`--isolation full`
   是否已把宿主 hook（含 claude-mem 的 observation 写入 hook）全部隔离，待核。
3. **`--compact-at`**：session resume 没有 compact 阈值参数；缺它则 resume 时
   context 超量只能靠 runtime 内部策略。

这三项都在 agent-runtime 侧新增 / 修改，本仓（引擎与 MCP）改不了。
