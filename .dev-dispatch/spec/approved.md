# test_line_restart 重启-adopt liveness 竞速测试除颤 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：监督面（wf-216dc3 worker 取证，2026-08-31 19:49 独立 worktree 复验），U1 goal-mcp-surface-split（PR#193）acceptance 通过后 make verify 整箱负载下偶发红。
- 类别：测试确定性修复（de-flake），非判据变更。goal.md 不改。
- 依赖：main@835ac2b（#193）已含该测试（TestKillRestartContract）。

## 根因（已实读，非推断）

`tests/test_line_restart.py::TestKillRestartContract::test_restarted_line_adopts_the_in_flight_coordinator_run`
用真实 detached fake agent-run（`tests/fakes/fake_slow_coordinator_run.py`：阻塞到 `release` 文件出现才落
result.json 退出）+ 真实 SIGKILL + `threading.Timer(2.0, release.touch)` 构造「重启 adopt 在飞运行」。

竞速点：release 定时器先于 `resume_start()` 的 adopt 判定触发 → fake 先落 result.json 退出 →
`resume_start` 判无在飞运行可 adopt，返回 `{'round_no': 1}`（重放 round1）而非 None →
`assert start is None, "the restart must resume, not replay round 1"` 红。整箱负载下 resume_start
（checkpoint 读取 + graph compile）耗时 >2.0s 即触发。

真机阴性复现：独立 worktree @acceptance commit（PR#193 后）整箱 `make verify` 实测
`1 failed, 2012 passed, 1 skipped`，失败行 `assert {'round_no': 1} is None`；同测试单跑 3/3 全绿
→ 纯负载依赖时序竞速，非产品回归。

与 PR#190（tests/test_re_adopt.py de-flake）同族：「赌真实子进程退出时序」竞速；本单同样改
「构造时序取代真实竞速」。

## 交付 A：确定性化该测试（构造时序，不赌真实竞速）

`tests/test_line_restart.py::test_restarted_line_adopts_the_in_flight_coordinator_run`：

1. 消除 `threading.Timer(2.0, release.touch)` 与 `resume_start` 的墙钟竞速。改为显式确定性次序
   /同步点：先 `start = resume_start(compiled, invoke_config)` 并在 fake 仍 in-flight 时
   `assert start is None`（此刻尚未 signal release，fake 必在飞 → adopt 确定性命中）；随后才 signal
   `release`（touch release 文件）让 fake 落 result.json 退出；最后 `compiled.invoke(start, ...)`
   让 resumed wait() 收到 adopted 结果返回。保证「adopt 必先于 release」在任何负载下成立。
2. 语义零放宽：三条契约断言必须原样保留且仍强断言——
   `launches == [expected_run_id]`（线程身份稳定、单次派发）、`launches[0].adopted is True`
   （重启不重复派发）、`start is None`（resume 非 replay）；SIGKILL 后 detached fake 存活
   （`os.kill(fake_pid, 0)`）与 phase1 的 `dispatch_count()==1`/checkpoint 落盘断言一律保留。
3. 产品代码零改动：仅改 `tests/test_line_restart.py`（及 `tests/fakes/` 相依赖项，若必要可新增测试
   辅助），不碰 `src/`。

## 交付 B：已知阴性可复现路径（写入测试 docstring/注释）

注明：把 release 信号改回「先 release 后 resume_start」（或还原 `threading.Timer(2.0, ...)` 竞速）
即复现 `assert {'round_no': 1} is None`——该变异正是本测试钉死的重启-adopt 竞速缺陷，
供后续 reviewer 判断测试仍有牙齿（不是删光断言求绿）。

## 交付 C：验收

```dd-acceptance
make verify
bash -lc 'for i in $(seq 1 20); do uv run pytest tests/test_line_restart.py -q -x >/dev/null 2>&1 || { echo "iter $i FAILED"; exit 1; }; done; echo "20 iters green"'
```

## 铁律

- 只改 `tests/test_line_restart.py`（+ 必要测试辅助）；不碰 `src/` 产品代码、不碰判据（goal.md）、
  不碰 harvest 子图/allowlist 语义、不触 E1–E7 词表。
- 一切改动走本 development worktree + PR，不直改 main；生产主 checkout 仅 ff-only pull。
- 参考纪律（本线 de-flake 先例 dev-fg-312338c8e635 / PR#190）：构造时序取代真实竞速、产品零改动、
  附已知阴性、压力复证。