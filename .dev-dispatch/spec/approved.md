# 验收不得在生产 systemd user manager 拉起 transient unit（P1）

## 缺陷

`make verify`（验收 = lint + test + conformance）跑 pytest 时，
`tests/test_launcher.py` 的 `TestSystemdRunActuallyAcceptsIt` 对真实 `systemd-run
--user --unit fleet-graph-launcher-*` 发起调用，在生产的 systemd user manager 里
拉起 `fleet-graph-*` transient unit。验收本应自包含/只读，却在生产 user manager
产生副作用。

## 要什么

验收期（`make verify`，含 `pytest` 与 conformance 脚本）在生产 systemd user
manager 不得新增任何 `fleet-graph-*` transient unit。真实 systemd-run 的集成断言
必须被 stub/mock 替代，或 gate 到显式 opt-in/独立环境，默认 CI/真机验收不真拉起。

## 判据（验收）

- 阳性：跑 `make verify`（至少 `pytest tests/test_launcher.py`）前后用机械读面
  （`systemctl --user list-units 'fleet-graph-*'` 或等价）比对，验收期间不出现新的
  `fleet-graph-*` transient unit。
- 阴性/回归：launcher 启动契约（`argv` 前两段 == `["systemd-run","--user"]`、
  unit 命名、属性拼写、无 shell、`append:` 目录语义）仍以 stub/mock 全绿钉住；
  对真实 systemd-run 的解析断言不因不真拉起而失去覆盖（mock 断言 argv，或 opt-in
  门控真机用例）。未实现（仍在普通验收中真拉起 transient unit）必红。

## 交付约束

- 只改 `tests/`（test_launcher.py 及任何真实 systemd-run/systemctl 的测试）与
  必要的测试工具/fixtures；不改生产 `src/fleet_graph/scheduler/launcher.py` 的
  启动语义与 unit 命名。
- 不部署、不重启、不触碰生产 checkout。

```dd-acceptance
uv sync --frozen
make verify
```