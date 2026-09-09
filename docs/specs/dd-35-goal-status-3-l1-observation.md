# goal_status 返回顶部带最近 3 条 L1 observation（protocol §12 末行）

## 背景
protocol.md §12 最后一行：「MCP 增一个只读工具 `goal_observations(...)`；`goal_status` 的返回顶部带最近 3 条 L1。」前半已实现（`mcptools.goal_observations` + `mcpserver._call_goal_observations`），后半没做：`src/fleet_graph/minimal/mcptools.py:181` 的 `goal_status` 只返回 `control.goal_status_view(events, ...)`，从不读 `observations.jsonl`。

## 要改什么
1. `mcptools.goal_status` 的返回 dict 增加键 `recent_observations`：读 `<run_root>/observations.jsonl`，取**最后 3 条**（按写入顺序，末尾三行，每条原样 dict）。复用已有的 `default_observations_reader`（mcptools.py:209），并像 `goal_observations` 一样提供可注入 `reader` seam（默认 None = 用本地读取器）。
2. 健壮性：文件不存在、为空、含半行坏 JSON → 该键为 `[]` 或跳过坏行，**绝不抛**（与 `default_observations_reader` 现有语义一致）。
3. 「顶部」落法：dict 本身无序，但实现上先插入该键，使 `json.dumps` 的序列化里它在前；在 docstring 里写明这一点。
4. `mcpserver._call_goal_status`（mcpserver.py:160）保持返回结构透传即可；如需要把新 seam 接上，只改这一个 `_call_*` 函数。
5. `docs/specs/minimal/context.md` 的「已实现」补一行。

## 不改什么
- 不改 `control.goal_status_view` 的签名与语义（它是纯 events 投射，L1 不进去）。
- 不改 `goal_observations` 的过滤语义（since_ts / severity）与 `mcptools.TOOLS` 声明表。
- 不改 `docs/specs/minimal/protocol.md`（§12 已写明该行，无需回写）。
- 不动 `handle_request` 的 JSON-RPC 形状、错误码与 `tools/list` 输出。

## 怎么验收（make verify 之外）
新增测试：无 `observations.jsonl` → `recent_observations == []`；写 5 条 → 返回后 3 条且顺序为写入顺序；夹一行坏 JSON → 被跳过且不抛；注入 reader seam 生效；`mcpserver` 经 `handle_request` 调 `goal_status` 时该键出现在结果里。
