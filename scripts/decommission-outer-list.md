# 仓外删除清单（SSoT）· wf-4601c8 R6 / B-3 附件一 · 2026-09-05

> 判据锚：goal.md §二 R6 与 §四·一 B-3（仓外删除：本线出清单与验证脚本，批准后由监督面或 wf-3ffd90 执行）；wf-8d9737 design.md §7.1/§7.2；specs/r6-legacy-removal.md 交付物 3 与行为契约 3。
> **本清单只备不执行**。每项含：对象、类别、验证命令（只读，= decommission-outer-verify.sh 同判据）、执行命令（供监督面复核后执行，本线不跑）、风险注记。执行前必须先跑 `scripts/decommission-outer-verify.sh` 留底稿，执行后复跑对账（exit 由残留数归零）。
> 仓内对应物（dd-mcp 五 NOT_SUPPORTED 工具、--stage-model 键、/v1/lines.parked、status.json、skill/persona 引用面）走 R6 仓内单（specs/r6-legacy-removal.md），不在本清单。

## A. systemd user 单元与 unit 文件（~/.config/systemd/user）

| # | 对象 | 类别 | 验证命令（只读） | 执行命令（监督面） | 风险注记 |
|---|---|---|---|---|---|
| A1 | `agent-bus-test.service`、`agent-bus-staging.service`、`agent-bus-autodev-test.service` 及同名 socket/timer | unit 文件+运行实例 | `systemctl --user list-unit-files \| grep -E '^agent-bus-(test\|staging\|autodev-test)'`；`systemctl --user list-units 'agent-bus-*' --plain` | `systemctl --user disable --now <unit>` 逐个；`rm ~/.config/systemd/user/<unit>{,.d}`；`systemctl --user daemon-reload` | 与生产 agent-bus-server/mcp 同二进制不同实例；删前确认无活跃 listener（`ss -ltnp \| grep -E ':(749[13])'` 归属） |
| A2 | `wf-observe.service` | unit 文件 | `systemctl --user list-unit-files \| grep -F wf-observe` | 同上 | 观测旧路；替代=Prometheus 拉取（保留清单 §7.3） |
| A3 | 退役 unit 族 `loop-engine-*`、`loop-mcp*`、`ronin-auto-gate*`、`ronin-babysitter*`、`ronin-pump-*`（.service 与 .d 目录） | unit 文件 | `systemctl --user list-unit-files \| grep -E '^(loop-engine-\|loop-mcp\|ronin-auto-gate\|ronin-babysitter\|ronin-pump-)'` | 逐个 `rm` + `daemon-reload` | 均已 inactive 仅文件残留；`loop-mcp` 名下若有 socket 先 stop |
| A4 | Tempo（unit 与部署件） | unit+部署件 | `systemctl --user list-unit-files \| grep -i tempo`；`ls /data/apps \| grep -i tempo` | `systemctl --user disable --now tempo*`；部署件迁移归 wf-3ffd90 | 0 条 trace（监督卷 2026-09-02 15:36 实测）；Loki/Grafana 不在本清单（保留清单 §7.3） |
| A5 | `ronin-mcp` 门面 unit（若以 user unit 存在） | unit 文件 | `systemctl --user list-unit-files \| grep -E '^(ronin-mcp\|loop-mcp)'` | 同 A3 | 与 §7.2.2 配套；工具面已死（上游 :7460 与 /data/ronin/runs 已停） |
| A6 | X-3 废单遗留：`fleet-graph-dd-dev-fg-5af16702b3c4-r1` unit 残件 | transient unit | `systemctl --user list-units 'fleet-graph-dd-dev-fg-5af16702b3c4*' --plain; systemctl --user list-unit-files \| grep 5af16702b3c4` | `systemctl --user reset-failed`（若有 failed 残件）；unit 为 transient 无文件则无需 rm | goal §七 X-3：该单 2026-09-05 03:31 已 stop，勿再查询/adopt/重启；本项只清 unit 残件不动其 dd 目录（dd 目录归 D 类） |

## B. agent-bus 运行时对象（生产 :7490）

| # | 对象 | 类别 | 验证命令（只读） | 执行命令（监督面） | 风险注记 |
|---|---|---|---|---|---|
| B1 | 测试看板频道族：`gd:e2e-gdrun-*`（54 个）、`chat:testroom`、`chatgroup:livetest-*`、`coord:observability-successors-*`、`board:dd-talk-staging-*`、`board:agent-runtime-profile-schema-*` | bus 频道 | `curl -s -H "Authorization: Bearer $T" :7490/v1/channels?limit=1000 \| jq -r '.channels[]?' \| grep -cE '^(gd:e2e-gdrun-\|chat:testroom\|chatgroup:livetest-\|coord:observability-successors-\|board:dd-talk-staging-\|board:agent-runtime-profile-schema-)'` | 经 bus 管理面逐频道删除（监督面持 admin token；agent 无删除权） | /v1/channels 全量 220 条（2026-09-02 实测）；只删测试族，`board:work-notes` 等生产频道零触碰 |
| B2 | 死协议族：`coord.*`（v1/v2 十条）、`dd.plan.*`、`coordination.dispatch-request.v1`、`probe.reqtype.v1`、`research.smoke.v1`、`agent.run.started/exited` v1 与 v2 | bus 协议注册 | `curl -s -H "Authorization: Bearer $T" :7490/v1/protocols \| grep -oE '"kind": *"[^"]*"' \| grep -cE 'coord\\.\|dd\\.plan\\.\|coordination\\.dispatch-request\\|probe\\.reqtype\\|research\\.smoke\\|agent\\.run\\.(started\|exited)'` | 经 bus 管理面注销（同上） | 注册表是运行时数据（INSERT not code）；删除前确认无活跃发布者（遥测近 7 天零命中再删） |
| B3 | `work.card.v1` 协议与 `board:work-index` 频道 | bus 协议+频道 | 同上两探针加 `grep -c 'work.card.v1'`、`grep -c 'board:work-index'` | 先把 ~300 条卡外键改指向目标 id（R6 仓内单），再删协议与频道 | 顺序护栏：**钉在 B1/B2 之后最后批**（spec-m8 语义）；外键未迁先删会断看板溯源 |

## C. 部署与引用态

| # | 对象 | 类别 | 验证命令（只读） | 执行命令（监督面） | 风险注记 |
|---|---|---|---|---|---|
| C1 | `/data/ronin` 引用清理（**不删目录**）：alias token 41 个迁 `/data/fleet-graph/secrets`，入编闸 6 硬编码路径改配置 | 引用断言+token 迁移 | `grep -rn '/data/ronin' /data/apps/fleet-graph/current/config /data/apps/fleet-graph/current/deploy`；`ls /data/fleet-graph/secrets` | token 迁移与名册路径改动＝**B-2/B-3 双管辖**，逐 token 复制→改配置→重启→旧 token 撤销，全程监督面执行 | goal 铁律「不得删 /data/ronin」；本项只断言「不再被引用」与新路径存在（v2 21 项 §7.2.8 同判据）；迁移未完成前 secrets 目录存在性即读数 |
| C2 | 旧 release 目录滚动清理（`/data/apps/fleet-graph/releases/` 早于现部署位的快照） | 部署件 | `ls -t /data/apps/fleet-graph/releases \| tail -n +4` | 保留最近 3 个，其余归档后删（监督面） | 回滚保险；不阻塞任何判据，纯空间卫生 |

## D. 运行数据（/data/fleet-graph 下）

| # | 对象 | 类别 | 验证命令（只读） | 执行命令（监督面） | 风险注记 |
|---|---|---|---|---|---|
| D1 | X-3 废单 worktree：`/data/worktrees/fleet-graph-wf-4601c8-r1-testenv-20260905` | worktree | `git -C /data/code/self/fleet-graph worktree list \| grep r1-testenv-20260905` | `git -C /data/code/self/fleet-graph worktree remove --force <path>`（监督面或 wf-3ffd90） | goal §七 X-3 点名「待 R6 清单一并处理」；其 dd 目录 `/data/fleet-graph/dd/dev-fg-5af16702b3c4` 保留作废弃记录（勿删——审计追溯） |
| D2 | 废弃 dd 单运行数据归档候选：`state=refused`/废弃单（如 dev-fg-ed22cba56b2c）的 `state/`、`checkpoint.sqlite3`、`agent-runs/` 大件 | 运行数据 | `jq -r 'select(.state=="refused") \| .development_id' <(/data/fleet-graph/dd/*/status.json 逐个)`（只读枚举） | record.json/events.jsonl/result.json **永留**（准入与终态权威）；仅大件缓存可归档 | 账随事走（宪法第八条）：回执链三权威件不删；本项是空间卫生非判据 |

## 执行顺序（顺序护栏）

A1→A2→A3→A4→A5→A6 → B1→B2 → **B3（钉最后批，外键迁移完成后）** → C1（与 B-2 名册面协同）→ C2/D1/D2（卫生项任意序）。
乱序执行（B3 先于外键迁移、C1 先于 token 复制核验）→ 监督面必须拒绝并留痕。

## 只读边界自证

`scripts/decommission-outer-verify.sh` 只含：`systemctl --user list-units/list-unit-files/reset-failed 查询形态`（无 start/stop/disable）、`curl GET`、`git worktree list`、`ls`/`grep`/`jq`/`test`。无任何 rm/disable/stop/deleted 调用——删的权力不在本线（B-3）。
