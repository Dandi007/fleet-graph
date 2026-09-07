# scribe.py：L1 observation 存储 + §12 证据闸

## 背景

GO-21（golden-order.md:92-110）设了第六个 agent「书记员」：只读，读 L0（events.jsonl + agent session 文件 + 每个 Stop 输出），产 L1 结构化 observation，每条带指回 L0 的证据指针。protocol.md §12 定了输入 `scribe.in/1`、输出 `scribe/1`、触发时机、存放位置与证据校验规则。

现状缺口：`prompts.build_scribe_in` 已能造输入，`protocol` 已能校验 `scribe/1` 信封，但 **observations.jsonl 的读写、以及 §12 那条证据闸都没有**。§12 明确要求：`evidence` 至少一条且每条能定位到 L0 的一个具体位置；引擎校验 `event_seq` 在区间内、`session` 路径存在；缺证据的 observation **整条丢弃**并落 `agent.failed(detail: observation_without_evidence)`；`observations` 为空数组是**合法**输出。

## 要改什么

只新增 `src/fleet_graph/minimal/scribe.py`：

1. `SCRIBE_TRIGGERS = ("goal.turn.finished", "dd.merged", "dd.failed", "goal.done", "goal.blocked", "goal.warning")` —— §12 只在 **goal 级**边界起书记员，不在 DD 内每个 stage 起（避免噪声）。
2. `ObservationLog`：写读 `<goal_run_root>/observations.jsonl`。写盘纪律**照抄 events.py 现有那套**（append + flush + `os.fsync`，一行一个 JSON，`ensure_ascii=False`），`read()` 要容忍被截断的最后一行（崩溃点）。`append(obs, *, trigger, seq_range, ts=None)` 按 §12「存放」段给每条补上 `ts` / `trigger` / `seq_range`。
3. `validate_observation(obs, *, since_seq, until_seq, session_exists) -> list[str]`：`evidence` 非空；每条 evidence 恰好是 `event_seq` / `session` / `stop_of` 三种指针之一；`event_seq` 落在 `[since_seq, until_seq]` 内；`session` 路径过注入的 `session_exists` 探针；`kind` 与 `severity` 在 §12 枚举内。
4. `partition_observations(...) -> (kept, dropped)`：`dropped` 每条带原因，供调用方落 `agent.failed(detail: observation_without_evidence)`。空 `observations` 合法（返回两个空列表，不报错）。
5. `findings_subset(kept) -> list[str]`：`severity in {warn, high}` 的子集，让 WF 的 findings 天然是 L1 高严重度子集（§12）。**只返回内容，不调 work-folder MCP。**
6. `new_runs_from_events(events, since_seq, until_seq, sessions_dir) -> list[dict]`：从 `agent.spawned` / `agent.exited` / `*.finished` 的 payload 造 §12 的 `new_runs`（run_id / role / session_dir / stop）。

## 不要改什么

- 不调 agent、不调 work-folder MCP、不写 events.jsonl、不 `import langgraph`。
- 书记员**不参与流程、不改任何状态**（GO-21）：本模块不得返回任何会影响 DD/goal 走向的判定。
- 不改 `prompts.build_scribe_in` 或 `protocol` 的签名。
- `__init__.py` 只在 docstring 模块清单加 `scribe`。

## 验收要点

fsync 写读往返；截断的最后一行被容忍；`event_seq` 越界被拒；`session` 不存在被拒；空 observations 合法；kept/dropped 的原因正确；`findings_subset` 只留 warn/high。
