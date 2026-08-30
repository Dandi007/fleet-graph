# M1 传感层薄版——fleet-state read-model（`127.0.0.1:7494`）spec

- 目标仓：`/data/code/self/fleet-graph`（Dandi007/fleet-graph；本 development 在 `/data/worktrees/` 下独立 worktree）。
- 里程碑归属：goal.md M1「传感层薄版」。本轮取 M1 前三条验收断言（`make verify` + `/v1/lines`、`/v1/decisions` 各返回带 `schema_version` 的列表）。
- 交付面：一个**只读** HTTP read-model 服务，bind `127.0.0.1:7494`，随 fleet-graph 部署常驻（systemd **user** unit）；两张视图 `/v1/lines`、`/v1/decisions`；schema 带版本字段；数据源 pull 现有工件（heartbeat / dd 工件 / bus / bridge journal），实现集中在服务内。

## 数据源（pull 现有工件，不新造生产者，不写任何被观察工件）

- 线心跳：`/data/fleet-graph/runs/<folder_id>/heartbeat.json`（字段见 `fleet_graph/state/run_artifacts.py` 的 `HEARTBEAT_FIELDS`：`run_id/folder_id/round/phase/pid/started_at/phase_started_at/updated_at/log_path`；`phase` ∈ `coordinator|worker|acceptance`）。
- 线终态/park：`/data/fleet-graph/runs/<folder_id>/terminal.json`（`terminal`/`pump_fault`/`rounds`/`waiting_on` 等；`waiting_on=="decision"` 即停牌 parked 语义，见 `normalize_waiting_on`）。
- dd 发展单：`/data/fleet-graph/dd/<development_id>/status.json`（`state`/`stage`/`terminal`/`failure` 等）与 `record.json`。
- 裁决送达链：agent-bus（`work.decision.v1`，只读）+ `decision-bridge` 的 `bridge.sqlite3` receipt（`published→bridged→consumed | swallowed(reason)`）。

## 依赖（已有，不重建）

- `fleet_graph/state/run_artifacts.py`（磁盘契约与字段名）；`fleet_graph/decision_bridge/`（送达链语义）；名册 `config/ronin-lines.json`（决定 `/v1/lines` 覆盖哪些线）。

## 交付（代码与评审全委 dev-dispatch；worker 不写业务代码）

### A. 服务本体（建议新模块 `src/fleet_graph/state/fleet_state.py`，接口面即契约）

1. 只读 HTTP 服务，bind `127.0.0.1:7494`（host/port 可配），实现集中在单一服务进程内；对数据源只 pull、只读。
2. `GET /v1/lines` → `{"schema_version": <str>, "lines": [<line_obj>...]}`：
   - `line_obj` 字段：`folder_id`、`generation`、`round`、`phase`、`heartbeat_age_s`、`terminal`、`parked`、`wake_facts`。
   - `heartbeat_age_s` = 现在 − heartbeat.json 的 `updated_at`（机械信号，不可伪造）；`wake_facts` 至少含 `waiting_on` 等机械事实。
3. `GET /v1/decisions` → `{"schema_version": <str>, "decisions": [<decision_obj>...]}`：
   - `decision_obj` 字段：`source_message_id`、送达链态 `state ∈ {published, bridged, consumed, swallowed}`、`swallowed` 态带 `reason`（如 `receipt_exists`）、`owner` 等只读事实。
4. `schema_version` 必填；两视图除版本字段外的主键列表字段名严格为 `lines` / `decisions`。
5. 读失败降级不 5xx 全链：单工件缺失/解析失败对该条目标记 absent/unknown，不影响整表返回（对齐「漏报即缺口」）；`/v1/*` 保持机械事实只读。

### B. CLI 入口 + 部署 unit

1. `fleet-graph state serve --host 127.0.0.1 --port 7494`（`src/fleet_graph/cli.py` 新增 `state` 子命令 + `serve`，本地验收与真机运行同一入口）。
2. `deploy/systemd/fleet-graph-state.service`（systemd **user** unit），随 fleet-graph 部署常驻；restart/enabled 语义对齐 `deploy/systemd/fleet-graphd.service` 既有约定。

### C. 测试（务实：只读、schema、字段名、两视图）

1. 新增 `tests/test_fleet_state_readmodel.py`，用合成工件（临时 run_root heartbeat/terminal + 临时 dd status + bridge fixture）断言：
   - `GET /v1/lines` 返回 `schema_version` 且 `lines` 为 list，`line_obj` 字段名与 A.2 一致；
   - `GET /v1/decisions` 返回 `schema_version` 且 `decisions` 为 list，`swallowed` 条目带 `reason`；
   - `heartbeat_age_s` 与合成 heartbeat `updated_at` 差值符合预期；
   - 缺失/坏工件不 5xx 全链、条目降级。
2. 使 `make verify`（lint+test+conformance，含上述新单测）通过。

## 可复现验收

```dd-acceptance
make verify
bash -lc "n=0; uv run fleet-graph state serve --host 127.0.0.1 --port 7494 & srv=$!; trap 'kill $srv 2>/dev/null' EXIT; until env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy curl -sf http://127.0.0.1:7494/v1/lines >/dev/null 2>&1; do n=$((n+1)); if [ $n -gt 40 ]; then exit 1; fi; sleep 0.25; done; env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy curl -sf http://127.0.0.1:7494/v1/lines | grep -q schema_version && env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy curl -sf http://127.0.0.1:7494/v1/decisions | grep -q schema_version"
```

## 量化判据（部署后真机；本轮取前三条）

1. `cd /data/code/self/fleet-graph && make verify` 通过。
2. `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy curl -sf http://127.0.0.1:7494/v1/lines` 返回带 `schema_version` 的 `lines` 列表。
3. `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy curl -sf http://127.0.0.1:7494/v1/decisions` 返回带 `schema_version` 的 `decisions` 列表。
4. 真实性抽检（次轮）：任选一条活线，`/v1/lines` 报的 `heartbeat_age_s` 与真机 heartbeat.json mtime 差 <60s。

## 铁律

- 代码与评审全委 dev-dispatch；worker 不写业务代码；一切改动走 PR 进 fleet-graph（本 development worktree），不直改 main。
- 生产主 checkout（`/data/apps/fleet-graph/current` 与 `/data/code/self/fleet-graph`）只允许 ff-only pull，严禁 checkout/switch/reset/切分支。
- H0/bootstrap 构造一律 `git worktree add` 到 `/data/worktrees/` 下独立路径。
- 本服务只读：不获得任何写权限、不发布 `work.decision.v1`、不调任何可写 MCP/git；harvest 写权限属后续里程碑，本单不触碰、不越界。