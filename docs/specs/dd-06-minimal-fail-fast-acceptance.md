# minimal 验收命令跑批：fail-fast 执行 + acceptance_results/event payload

## 背景
每个 goal 必带程序化验收命令，Impl commit 后引擎跑一遍，必须全部 exit 0 才进 CR，不过就回 Impl（GO-6.1、design.md §3.2）。派单时 Goal Agent 可给 `acceptance_extra` 叠加（protocol §2）。验收结果要以 `[{cmd, exit, tail}]` 进 review 输入与 DD 结果对象（protocol §5、§7），每条命令落一条 `dd.acceptance` event（§8）。已合并的 `enroll.py` 只做 enroll 时的 `bash -n` 静态检查，不负责执行。

## 要改什么
新增 `src/fleet_graph/minimal/acceptance.py` 与 `tests/test_minimal_acceptance.py`：

1. `CommandResult` frozen dataclass：`cmd`、`exit_code`、`tail`、`duration_s`、`timed_out`。`tail` 由 `tail_of(text, limit=4000)` 产出：只留尾部，按行边界切，被截断时前置一行截断标记（如 `…[truncated N chars]`），保证长度上界。
2. `combine_acceptance(base, extra) -> list[str]`：goal 的 `acceptance` 后叠加 DD 的 `acceptance_extra`，保序去重，跳过空串；`base` 为空抛 `ValueError`（protocol §1 要求至少一条）。
3. `run_acceptance(cmds, *, cwd, runner, timeout_s=1800, env=None) -> AcceptanceRun`：`AcceptanceRun` frozen dataclass(`results: tuple[CommandResult, ...]`, `passed: bool`, `failed_cmd: str|None`)。**fail-fast**：按顺序跑，第一条非零或超时即停，后续命令不执行也不出现在 results 里；`passed` 仅当全部命令 exit 0 且命令数 ≥1。`cmds` 为空抛 `ValueError`。
4. `runner` 用注入式 `Protocol`（`run(cmd: str, cwd: str, timeout_s: int, env: dict|None) -> Completed(exit_code, output, timed_out)`），风格照 `gitgate.py`；并给默认实现 `BashRunner`：`bash -lc <cmd>`，`cwd` 为 worktree，stdout/stderr 合并，`start_new_session=True` 且超时时杀整个进程组，超时记 `timed_out=True` 且 `exit_code` 为负或 124（自选但需在 docstring 写明并测到）。
5. 投射函数：`acceptance_results(run) -> list[dict]` 产出 protocol §5/§7 的 `[{cmd, exit, tail}]`（键名就是 `exit`，不是 `exit_code`）；`acceptance_event_payload(result, *, index, total) -> dict` 产出 `dd.acceptance` 的 payload（含 `cmd`、`exit`、`tail`、`duration_s`、`timed_out`、`index`、`total`）。

## 不改什么
- 不动 `src/fleet_graph/minimal/__init__.py`。
- 不 import 其他 minimal 模块（本模块自足）。
- 不写 events.jsonl、不做 git、不实现引擎图与重试策略（回 Impl 的决策属另一张 DD 的状态机）。
- 不加第三方依赖。

## 怎么验收
`make verify` 全绿。`tests/test_minimal_acceptance.py` 至少覆盖：注入 runner 下的 fail-fast（第二条失败则第三条不被调用，用调用记录断言）；`passed` 真/假；`combine_acceptance` 去重保序与空 base 报错；`tail_of` 的行边界与长度上界；`acceptance_results` 的键名与顺序；`acceptance_event_payload` 字段齐全；用真实 `BashRunner` 跑 `true` / `false` / `echo` 三条轻量命令与一条 `sleep` 超时（timeout_s 设 1 秒级），确认超时被杀且 `timed_out=True`。
