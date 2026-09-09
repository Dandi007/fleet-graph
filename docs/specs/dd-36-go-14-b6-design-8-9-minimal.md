# 旧部件下线盘点（GO-14 B6 / design §8-§9）+ 「minimal 不依赖旧包」守卫测试

## 背景
GO-14 B6 用户原话：「新的写好了的话 重复的全都下线」；design.md §9 落地路径第三条同义，§8 第二条给出重复清单：scheduler/wake/parked、decision MCP、decision-bridge、arbiter、看板投票、harvest、supervisor 图、goal_interrupt。最小系统主体（dd-01..33）已在 release 分支落地，但这些旧包**仍全部在树上**：`src/fleet_graph/{scheduler,arbiter,goal_interrupt,decision_bridge,supervise,graphs,dd,goal,goal_enroll,executors,bus,state,cost_obs}/` 与 `src/fleet_graph/decision_mcp.py`、`line_state_mcp.py` 等。直接删是几千行、跨 pyproject console scripts 与 systemd 单元的动作，一张 DD 装不下也不安全。本 DD **只产盘点与机械守卫，不删任何生产代码**，为后续分批删除铺路。

## 要改什么
1. 新增 `docs/specs/minimal/decommission.md`。文档头先写判定标准：只有同时满足「design §8 第二条明确列为重复」「不被 `fleet_graph.minimal` 包 import」「不被 minimal 的测试依赖」的单元才进可删批次。正文一张表，每行一个候选单元，列：`路径`（第一列，用反引号包住真实路径）、约 py 行数、对外入口（pyproject console script / systemd 单元 / MCP 工具，逐个列名）、仓内引用者（谁 import 它）、被 minimal 的哪个模块取代（design §8 逐条对齐；无对应物则写「无对应物，非 minimal 范围，保留」）、判定（`batch-1 可删` / `需先解依赖` / `保留`）、批次顺序与理由。必须覆盖 design §8 第二条清单的每一项，并逐项给出它在本树上的现存路径（或写明「本分支已不存在」）。
2. 新增 `tests/test_minimal_decommission.py`，两条机械守卫：
   - (a) 用 `ast` 解析 `src/fleet_graph/minimal/**/*.py` 的全部 import，断言除 `fleet_graph.minimal` 自身（含相对 import）外不 import 任何其它 `fleet_graph` 子包/模块——最小系统必须自足（dd-04 已为此拆掉 `fleet_graph.dd` 依赖，本条防回归）。不要用正则，要用 ast。
   - (b) 断言 `decommission.md` 表格第一列列出的每个路径在树上真实存在（文档不许写幽灵路径）。解析规则在测试里写死并在文档头注明：以 `| ` 开头的数据行、第一列是反引号包住的仓库相对路径。
3. `docs/specs/minimal/context.md` 加一行指向 `decommission.md`，并写明「分批删除待后续 DD；release→main 合并是人工闸，删除批次由人过目」。

## 不改什么
- **不删除、不移动、不修改** `src/fleet_graph/` 下任何非 `minimal/` 的现有代码及其测试。
- 不改 `pyproject.toml` 的 console scripts / 依赖、不动任何 systemd 单元文件。
- 不改 `docs/specs/minimal/{design,protocol,golden-order,README}.md`。
- 不改 `make verify` 的组成与 `Makefile`。

## 怎么验收（make verify 之外）
`docs/specs/minimal/decommission.md` 存在，且 design §8 第二条 8 项逐项可在文中查到判定；两条守卫测试通过；`git diff --stat` 只含新增两个文件 + `context.md` 一行。
