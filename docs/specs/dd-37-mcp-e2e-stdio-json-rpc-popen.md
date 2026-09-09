# MCP 控制面进程级 e2e：真 stdio JSON-RPC + 真 Popen spawn

## 背景
dd-22 给 `mcpserver.py` 加了 17 个单测，但都是 `handle_request` 纯函数级、spawner 是 stub；dd-26 的 e2e 直接调 `engine.run_engine`，整条 MCP 路径被绕过。GO-10「enroll 的 MCP 自己 spawn 就行」与 protocol §10「MCP 是唯一常驻服务、读只读 events.jsonl、写只做 spawn 或 append control.jsonl」在**真 stdio 管道 + 真进程 spawn** 上还没有一次端到端证据。

## 要改什么
只新增 `tests/test_minimal_mcpserver_e2e.py`。分两段，都不许起真 agent、不许联网：
1. **进程级传输 e2e**：用 `subprocess.Popen([sys.executable, "-c", "...mcpserver.main([...])"], stdin=PIPE, stdout=PIPE)` 起真服务器（`--engine-root <tmp_path>`），逐行喂 JSON-RPC：`initialize` → `tools/list`（断言工具名集合与 `mcptools.TOOL_NAMES` 逐一相等）→ 三个只读工具（`goal_list` 空根 → `[]`；`goal_status` 对预置的 `events.jsonl`；`goal_observations` 对预置的 `observations.jsonl`）→ 一行非法 JSON 与一个未知 method（断言 JSON-RPC error 形状，且服务器**不退出**、后续请求仍能应答）。只读工具不 spawn，安全。每次 readline 必须有超时上界，绝不允许挂死。
2. **真 spawn e2e（进程内）**：在测试进程里构造 `ServerContext(spawner=mcpserver.popen_spawner)`（真 spawner、真 `start_new_session`），monkeypatch `mcpserver.engine_argv` 返回一个 stub 命令（`[sys.executable, "-c", "<往 events.jsonl 追加一行的最小脚本>"]`），走一次 `goal_enroll`（真 bare origin + 真 clone + 真 worktree，remote 指本地 bare 路径，acceptance 用 `true`），断言：返回里 `spawned` 为真且带 pid、log_path 文件被创建、stub 写下的 event 可被随后的 `goal_list` / `goal_status` 读出来。

## 不改什么
- src 原则上零改动。**只有**发现真 bug 时才允许改 `mcpserver.py` 的 spawn / argv 相关函数（`engine_argv` / `popen_spawner` / `_execute_spawn`），并在 commit message 写清是什么 bug；不许改 `handle_request` 的返回结构、错误码、`tools/list` 输出，不许改 `mcptools.py`、`engine.py`。
- 不改 `Makefile` / `make verify` 组成、不加新依赖。
- 不改 `docs/specs/minimal/` 下任何文件（本 DD 是补证据，不是改设计）。

## 怎么验收（make verify 之外）
新测试在 `make verify` 与裸 `pytest` 下都要能跑（`mcpserver` 若确实不 import langgraph 就不要加 skip 门；若发现它间接 import 了，按 dd-18/dd-26 先例用 per-test skip）。测试必须无网络、git 全走本地 bare 仓、总耗时数秒内、无残留子进程（结束时确保 terminate/wait）。
