# Spec X-1（wf-4601c8）· goal MCP line_message 投递必失败修复（sink 缺省 base_url）

> 状态：定稿（2026-09-05，监督面 2026-09-05 02:52 实发复现后撰写）。判据锚：goal §七 X-1 监督面定性「优先级高于 R2」；goal §四「弱化验收＝B-4 升报线，禁止」。
> 本单只修一件事：`BusLineMessageSink` 缺省 `base_url=None` 原样下传 `BusClient`，击穿 client 的 `DEFAULT_BUS_URL` 缺省，使监督面 `line_message` 投递必失败。

## 交付物（恰好两个文件，其余零改动）

1. `src/fleet_graph/goal/line_message.py`（修改：仅 `BusLineMessageSink` 的缺省 base_url 解析——`base_url is None` 时从 `FLEET_GRAPH_BUS_URL` 环境变量取值、未设时用 `fleet_graph.bus.client.DEFAULT_BUS_URL`；显式非 None 入参原样生效。模块内其余一切函数/类语义不动）
2. `tests/test_x1_line_message_baseurl.py`（新增：本单全部新用例）

零改动清单：`src/fleet_graph/bus/client.py`、`src/fleet_graph/goal/service.py`、`Makefile`、`tests/` 既有全部测试文件（零测试删除、既有断言零弱化——B-4 红线）、其余一切文件。

## 一、缺陷事实（基线 9c83eb3 上已复现，报错原文）

- 机理（行号指 9c83eb3）：
  - `goal/service.py:465` `line_message_sink=BusLineMessageSink()`——不传 base_url；
  - `goal/line_message.py:224-231` `__init__(*, base_url: str | None = None)` → `self._base_url = None`；`_client()`（行 233-240）把它原样传给 `BusClient(base_url=self._base_url, …)`；
  - `bus/client.py:105` `base_url: str = DEFAULT_BUS_URL` 的缺省值被显式 `None` 击穿；行 111 `self.base_url = base_url.rstrip("/")` 对 None 炸；
  - `deliver_line_message`（line_message.py:196-203）把非 LineMessageError 异常包成 `LINE_MESSAGE_DELIVERY_FAILED`。
- 基线复现原文（一次性 worktree @ 9c83eb3，`uv run python`，2026-09-05 06:4xZ）：
  - 崩点：`AttributeError: 'NoneType' object has no attribute 'rstrip'`
  - 端到端（监督面 02:52 所见同形）：`LineMessageError: LINE_MESSAGE_DELIVERY_FAILED: delivering to agent:wf-4601c8 failed: 'NoneType' object has no attribute 'rstrip'`
- 影响面：凡走 `BusLineMessageSink()` 缺省构造的 line_message 投递 100% 失败（构造即炸，与 token 有无、目标行有无无关）；带显式 base_url 的调用不受影响。

## 二、修复硬性要求

- `BusLineMessageSink.__init__`：`base_url` 为 None 时，取 `os.environ.get("FLEET_GRAPH_BUS_URL", DEFAULT_BUS_URL)`（`DEFAULT_BUS_URL` 自 `fleet_graph.bus.client` 导入）；非 None 时原样使用、优先于环境变量。解析发生在 `__init__`（构造时点），使测试可先 `monkeypatch.setenv` 再构造。
- 语义不变面（不许动）：`_client()` 的 token 解析与两条构造分支；`publish()` 的 channel/kind/idempotency 形状；`deliver_line_message` 全部拒绝码与顺序（先身份后 roster 后 sink）；payload 闭合字段集（无 decision 字量）；`bus/client.py` 一行不动（`DEFAULT_BUS_URL` 缺省与 `rstrip` 行为保持——修复在 sink 侧，不给 client 加 None 容忍）。
- 边界：不新增任何网络调用面；不对生产 127.0.0.1:7490 产生任何请求（测试全部走自起临时 HTTP 服务或离线构造）；不引入新依赖。

## 三、新测试文件内容（全部落在 tests/test_x1_line_message_baseurl.py）

1. 绿侧·缺省构造：`BusLineMessageSink()`（无 base_url）后 `_client(alias)` 成功返回 `BusClient`，且 `client.base_url` == `FLEET_GRAPH_BUS_URL`（setenv 后构造）或 `DEFAULT_BUS_URL`（未设时）；显式 `base_url="http://…" `原样生效且压过环境变量；`BusClient()` 裸构造仍可（client 既有缺省不被破坏）。
2. 端到端（`-k end_to_end` 命中、恰一用例）：测试内起临时 HTTP 服务（空闲端口、线程内应答 publish 200 JSON `{"message_id":"msg_test…","entity_id":"…","channel_seq":1,"deduplicated":false}`），`monkeypatch.setenv("FLEET_GRAPH_BUS_URL", 该地址)`，用生产 `BusLineMessageSink()`（缺省构造）走 `deliver_line_message`（或 goal/service 工具层同链）投递 info 消息：返回 `delivered=True` 且 `message_id` 非空；临时服务实际收到 ≥1 次 POST；断言零生产触碰（请求只落在临时端口；全程无对 7490 的连接）。该用例最后 print 一行固定回显（供 cmd3 grep）：`e2e delivered=1 code=- message_id=<id> http_hits=<n> prod_touch=0`。
3. 变异红锚（注入翻转：把 sink 的缺省解析表达式替换回 `None` 直传——即恢复缺陷形状；变异副本落 tmp、按源码字符串替换后以 importlib 装载，不触碰真模块）：断言变异副本在「无 base_url 构造 + `_client`」处抛 `AttributeError: 'NoneType' object has no attribute 'rstrip'`，且 `deliver_line_message` 将其包为 `LINE_MESSAGE_DELIVERY_FAILED`（与 §一 复现原文同形）——缺省解析被摘除时必须可检出地红；同时真模块同路径绿（成对背书）。
4. 零测试删除断言：`tests/test_m4_line_message_seats.py` 与 `tests/test_line_message_ack_evidence.py`（及 `tests/test_r1_testenv.py`、`tests/test_r0_verify_rebuild.py`）的 git blob 与基线 `9c83eb3bcdff4aa4c534f4ba914021b18d5b7819` 侧逐字节相同（照 `test_zero_test_deletion_r0_file_unchanged` 的既有写法）。
5. 既有用例一行不动：本单不改任何既有测试文件；`make verify` 全量绿即既有 M4/ack 语义背书。

## 四、验收判据（阳/阴）

- a) 修复侧：不传 base_url 也能构造 client（§三.1 绿）；line_message 端到端投递成功（§三.2 delivered=1、http_hits≥1、prod_touch=0）。
- b) 变异侧：sink 缺省解析被移除的变异必须红（§三.3）。
- c) `uv run pytest -q tests/test_x1_line_message_baseurl.py` 连跑 ≥3 次与 `make verify` 连跑 ≥3 次全绿、零测试删除。
- d) 基线红可复现：本 spec §一 报错原文即基线取证（gate 侧保留重放权利）。

## dd-acceptance

```dd-acceptance
bash -lc 'uv sync --frozen && for i in 1 2 3; do uv run pytest -q tests/test_x1_line_message_baseurl.py || exit 9; done'
bash -lc 'for i in 1 2 3; do env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify || exit 8; done'
bash -lc 'env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy bash -c '\''O=$(uv run pytest -q -s tests/test_x1_line_message_baseurl.py -k end_to_end 2>&1); rc=$?; line=$(printf "%s\n" "$O" | grep -cE "^e2e delivered=1 code=- message_id=\S+ http_hits=[1-9][0-9]* prod_touch=0$"); echo "e2e_rc=$rc e2e_lines=$line"; test "$rc" -eq 0 -a "$line" -ge 1'\'''
```

（cmd1/cmd2 与 R1-fix 同形（连跑三遍＋exit 9/8 守护）；cmd3 端到端：跑 `-k end_to_end` 用例、grep 固定回显行 `e2e delivered=1 code=- message_id=… http_hits=… prod_touch=0`，rc=0 且行数 ≥1 才过——投递成功与生产零触碰同时被数字判定。）

## 派单参数备忘（coordinator 用，非 spec 正文）

- target_base＝9c83eb3bcdff4aa4c534f4ba914021b18d5b7819（origin/release/wf-4601c8 现头，fetch 已核）。
- stage_models＝{"implement":"glm-5.3-flash","continuous_review":"glm-5.3","final_review":"glm-5.3"}；dispatched_by＝wf-4601c8。
- repo_path＝独立 worktree（/data/worktrees/fleet-graph-wf-4601c8-x1-20260905T064150，detach 于 9c83eb3）；派单前置探测按 goal §四（三间隔≥20s 的 ls-remote＋push --dry-run 六连通）。
