# Goal Agent done 的后置机械核：有在飞未收口的 DD 就打回，不许 done

## 背景

protocol.md §0.10「每个 agent 节点前后引擎核对什么」表里，Goal Agent 的 Stop 后核对有两条：

- `dispatch`：每个 repo 过 §2 的 GO-36 五条核 —— **已实现**（`goalgraph.validate_dispatch` + `dispatch.check_dispatch_ready`）。
- `done` 时无未合并的 DD —— **未实现**。

现状：`goalgraph._route_after_goal_turn`（约 205-213 行）在 `stop == "done"` 时直接路由到 `final_merge`，中间没有任何闸。这与 GO-25 的机械化原则（能机械化确定的都机械化、对不上就打回）不一致。

## 要改什么

1. `goalgraph.py` 在 `done` → `final_merge` 之间加一个**程序节点**（建议命名 `validate_done`），从 `deps.event_log.read()` fold 出「已有 `dd.dispatched`、但没有对应 `dd.merged` / `dd.failed` 终态事件」的 DD id 列表（即在飞未收口的 DD）。
2. 列表**非空** → 落一条 `goal.warning`（payload 带 `reason: "done_with_inflight_dd"` 与在飞的 `dd_ids` 列表），清掉 `stop`，把字段级说明作为下一个 turn 的 `warnings` 交接内容路由回 `goal_turn`——与 `validate_dispatch` 打回 dispatch 完全同一种「打回，不猜」语义。
3. 列表**为空** → 原样进 `final_merge`，该路径行为逐字节不变。
4. 在节点 docstring 里把语义写死：`failed` 的 DD 是**终态**，不算「未合并」，**不阻塞** done —— Goal Agent 已经在 `dd_summary` 里看到它并自行判断过了；只有派出去但没收口的在飞 DD 才拦。

## 不改什么

- 不新增 event kind：复用 `events.py` 已注册的 `goal.warning`。
- 不改 `final_merge_node` / `finish_blocked` / `validate_dispatch` / `run_dd_node` 的既有逻辑。
- 不改 `docs/specs/minimal/` 下任何文件。
- 不改 `ddgraph.py` / `engine.py`，特别是不动 §11 的 resume 路径（`resume_initial_state` / `ddgraph.resume_entry`）。
- 不改 `events.fold` 的派生逻辑；在飞判定在本节点内自己算，不改公共 fold 的返回结构。

## 怎么验收

`make verify` 全绿。另在 `tests/test_minimal_goalgraph.py` 补测试：① 有在飞 DD（只有 `dd.dispatched`、无终态）时 Goal 报 done 被打回，回到 `goal_turn`，且 `final_merge` 调用次数为 0，下一轮 turn 的输入 `warnings` 含该说明；② 无在飞 DD 时 done 照常走 `final_merge` → `goal.done`；③ 历史里有 `dd.failed` 的 DD 不阻塞 done。
