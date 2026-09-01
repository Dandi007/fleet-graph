# fleet-graph dd 读模型补 `dispatched_by`（line↔development 归属，A 类可观测缺口）spec

- 目标仓：`/data/code/self/fleet-graph`（本仓，https://github.com/Dandi007/fleet-graph）
- 分类：A 类可观测缺口（dd read-model 不暴露「这张单是哪条线派的」；= r1-r7-seeds-inventory.md「仍待后续派单」R7① `dev_fleet_line_dev_mapping_01`「line↔development 归属未打通」）
- 立案：2026-09-02 常驻线 fresh 扫描（r1-r7-seeds-inventory「仍待后续派单」清单择最高价值项）

## 1. 现象与真因

- 现象（本轮 fresh 实回显）：Alertmanager `api/v2/alerts?active=true` 现有 34 条 `DevelopmentTerminalFault`（warn）在烧；Prometheus `fleet_dd_dev_state` 772 个 series 仅 `{development_id, state, instance=127.0.0.1:9105, job=fleet}` 标签，**无 `line`/`folder_id` 标签**。任何一张 dd 单都无法归因到「哪条线派的」，`development_list` 里 281 个 dev 目录、34 条终态 fault 全成无主孤单，做不了「每线任务视图」，也无法按线定位该堵谁。
- 真因：dd 引擎 admission `record.json` 已存权威 `dispatched_by`（实测 `dev-fg-f5f271c90e0e` 的 record.json 含 `"dispatched_by": "wf-6475fd"`，`control_plane.py` 写 record 时含此字段），但 `rebuild_status()`（`src/fleet_graph/dd/control_plane.py:1073`）构造 `status.json` 时**未把 `dispatched_by` 落入 status dict**；而 `development_list`/`development_get` 的行、以及 fleet-sentinel 的 `_collect_dd`（读 `<dd_root>/dev-*/status.json` 派生 `fleet_dd_dev_state`）全读这个 `status.json`，于是 `dispatched_by` 在 read-model 层被埋没。

## 2. 修复方向（观测契约；实现细节交 dev-dispatch）

1. `rebuild_status()` 的 status dict 增字段 `"dispatched_by": str(record.get("dispatched_by") or "")`；缺失/不可解析 → 空字符串（fail-soft，绝不 crash）。
2. `status.json` 由此含 `dispatched_by`；其它既有字段一律不变（additive、向后兼容：fleet-sentinel 只按键读，多一字段无害）。
3. `development_list` / `development_get` 返回行随之携带 `dispatched_by`（该单所派线，如 `wf-6475fd`；被人/外部无无线主体派发的单为空串）。
4. 既有单：`status.json` 在下次 `rebuild_status`（非 terminal 每次读即重建；list fast-path 逐行重建）时从 `record.json` 回填 `dispatched_by`；新建单自 create 起即写。
5. 一致性阴性：`status.json` 的 `dispatched_by` 必须恒等于 `record.json` 同名字段；不得另存一份、不得从 worker-run `argv.json` 的 `--label dispatched_by=…` 现算（那只是 label 投影，非权威）。

字段名固定 `dispatched_by`，字符串型（可为空串），每行必带。

## 3. 真机判据

1. `development_get(dev-fg-f7c84e1edb98).dispatched_by == "wf-6475fd"`（该单正是本线所派，其 record.json 已含该值）。
2. `development_list` 各返回行均含 `dispatched_by` 字段（非缺失、非 null）。
3. 下游：fleet-sentinel 可据此给 `fleet_dd_dev_state` 增 `line` 标签（后续 B 段平台侧，本 spec 只补 read-model 数据源，是 B 段的 DoD 前置，不在本 spec 范围内改动）。

## 4. 验收（dd-acceptance，代码级）

```dd-acceptance
uv run pytest -q tests/test_dd_control_plane.py tests/test_dd_service.py
make verify
```

## 5. 铁律

- 代码编写与 review 一律交 dev-dispatch（本 spec 只定义契约与判据）。
- git 一律 `git worktree add` 到 `/data/worktrees`；生产主 checkout（`/data/code/self/fleet-graph`）仅 `git pull --ff-only`。
- 测试需覆盖：`status.json` 含 `dispatched_by`；`development_list`/`development_get` 行携带；缺省空串；既有单回填；`record.json`↔`status.json` 一致（阴性不漂移）。