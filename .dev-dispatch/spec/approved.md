# M4 判定口真实落地 —— verify-mcp-only.sh M4 段用判定口实测，替代文本 grep

## 背景
`fleet_graph.mcp_availability.judge_mcp_availability`（M4 判定口）已合 main（harvest 3598fc2），并被
`tests/test_m4_availability.py` 覆盖（11 用例：阳性 不可达/只读调用失败→unavailable；阴性 正常面→available；
NOT_SUPPORTED 拒绝→not_supported 不计失败）。但该判定口至今无任何活体消费者接入——`scripts/verify-mcp-only.sh`
的 M4 段仍是「grep src/config 找 tools/list+告警关键词」的文本判断即判绿（本轮实机 grep -rln 确认 `judge_mcp_availability`
仅被 mcp_availability.py 自身与 test_m4_availability.py 引用）。故监督面 goal 级验收判 DoD4「判定口没有可指认的落地 /
tools/list 成功 + 一个只读工具真调用成功 这条判定口未证成立」是成立的。本单把判定口真实跑起来，使 M4「存在且能红」可被机械判定。

## 交付物
1. 改写 `scripts/verify-mcp-only.sh` 的 **M4 段**（内嵌 python heredoc），复用
   `fleet_graph.mcp_availability.judge_mcp_availability` + `FastMcpSurface`，替代当前文本 grep：
   - **M4 阳性（判定口能红）**：把判定口指向一个不存在/不可达地址（如 `http://127.0.0.1:1/mcp`），
     `judge_mcp_availability` 必须回 `unavailable` 且 `list_error` 非空——把「某 MCP 面的上游指向不存在地址 → 必须告警」
     落实为一次真实调用（不是 grep、不是自述）。若真实调用证明判定口能把不可达上游判为 unavailable → 阳性绿，否则红。
   - **M4 阴性（不恒亮 / NOT_SUPPORTED 不计失败）**：把判定口指向一个 live 且只读工具能真调用的面
     （dd `:5610`，read_only_tools=["development_list"]），`judge` 必须回 `available`（面正常不得开火）；
     且 dd 面显式 `NOT_SUPPORTED` 历史工具（`NOT_SUPPORTED_TOOLS` 里任一，如 `development_steer`）加入探针时其拒绝
     必须被 `is_not_supported_refusal` 记为 `not_supported` 而非 `error`、不得把整体判失败 → 阴性绿，否则红。
   - 面不可达（connection refused）且无法构造判定前提时，诚实报「不可判定 + connection refused 证据」计红，
     不伪造绿、不伪造「不存在判定口」。
2. **不复刻第二个判定口**（复用 `fleet_graph.mcp_availability`）；**不写任何告警规则**——告警规则归可观测线
   wf-6475fd，本线只交付判定口 + 结构化结论（`AvailabilityVerdict.as_dict()`：status/tools_listed/probes/list_error）。
3. 扩 `tests/test_mcp_only_scaffold.py` 钉一条回归：断言 verify-mcp-only.sh 的 M4 段真实调用 `judge_mcp_availability`
   （静态断言脚本文本引用 `judge_mcp_availability` 或 `FastMcpSurface`，而非仅 grep 关键词；可选再加对 M4 阳性/阴性口径
   的单元级守卫）。
4. 既有探测纪律不变：只读、只用本机通用命令（bash/python3/urllib/fastmcp 判定口）、不可判定计红、不硬编码红绿、
   不破坏 M0/M1/M2(a)/M2(b)/M3 段与既有红色计数语义；全部改动只进 worktree，生产主 checkout 只 ff-only。

## 双向判据（对齐 goal.md M4，不可弱）
- 阳性：把某 MCP 面的上游指向不存在地址 → 判定口判 unavailable（可被告警）。
- 阴性：正常面不得误报（判 available）；显式 NOT_SUPPORTED 历史工具不得算失败。

## 红线
- 不写告警规则（归 wf-6475fd）；不复刻判定口（复用 mcp_availability）；不碰 M3；不退役任何现役面；不越界部署。
- prod 主 checkout 只 ff-only；改动只进 worktree。

## 验收
```dd-acceptance
uv sync --frozen
uv run pytest -q tests/test_mcp_only_scaffold.py tests/test_m4_availability.py
```