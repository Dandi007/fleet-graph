"""The shipped ronin line config, checked against the loader that will read it.

A config file that no test loads is a config file that can rot silently: the
first thing to notice would be a scheduler that refuses to start.
"""

from __future__ import annotations

import json
from pathlib import Path

from fleet_graph.scheduler.daemon import SchedulerConfig

CONFIG = Path(__file__).resolve().parent.parent / "config" / "ronin-lines.json"

# The lines babysitter v28 carried that were still live when the fleet stopped
# (last terminal=killed at the maintenance-stop moment), minus the two whose
# subject really is the engine P4 retired. See wf-3f30cd findings §32/§33.
#
# wf-287e81 was excluded on a first pass and put back: its short title reads
# "loop-engine-fallback-and-goal-to-spec-plugin", but its goal.md is a
# Goal-to-Spec queue closeout whose work lives in repo-spec-forge-plugin and
# merger-plugin. dev-dispatch was only the vehicle, and fleet-graph is now
# that vehicle. Classifying by title instead of by goal is the same
# "name match is not semantics" trap this repo keeps hitting.
# P7 §5-D2：金丝雀爆炸半径最小（只改告警配置面），且产物人眼可验。
CANARY = "wf-40fa8d"

# 第二批：可观测 / 额度类，改面板与配置，不动核心服务。金丝雀已实证引擎
# 全链（25 轮无重复推进 → dd 四阶段封存 → 真实产品 diff → 在授权边界正确
# 停住），四处迁移等价缺口已补齐（执行器指向不可变 release、线的 PATH、
# 启动间隔、退避）。放量由 agent 依用户 2026-08-27 02:0x 全权委托代行。
BATCH_TWO = {
    CANARY,
    "wf-7bc4d1",  # llm-usage-dashboard
    "wf-6475fd",  # observability-onboarding
    "wf-386b2f",  # agent-work-cost-observability
    "wf-5664e5",  # quota-api 指标内建
    "wf-9b5931",  # agent-runtime-model-switch
}

# 监督面 2026-08-27 依用户「都派出去」新开的两条线。刻意与 MIGRATED 分开：
# 那个集合的含义是「P5 从 babysitter v28 迁过来的存量线」，把新开的线混进去
# 会让它不再能回答「迁移做完了没有」。
OPENED = {
    "wf-a08949",  # P3 收尾：把 dev-dispatch 真正切到 fleet-graph 引擎
    "wf-a87b04",  # work-folder 治理层健壮性（WORKTREE_DIRTY 活锁根修）
    "wf-197430",  # agent 权限声明式化（可配置 / 可审计 / 单点声明）
}

# 2026-08-28 监督面收编：三线使命完成，enabled=false 但留在名册里
# （名册是编成史实，收编靠开关不靠删行）。
# - wf-a08949：dd 切换目标已达成，老引擎退役由监督面亲自执行，线使命完成；
#   其基于 :7455/:7460 的 acceptance 判据随老引擎退役失效，整段删除。
# - wf-40fa8d：terminal=done（C1/C2/C3 五件套验收通过）。
# - wf-7bc4d1：terminal=done（真机全量验收通过）。
CONVERGED = {
    # wf-a8c7b5：步 7 演练 scratch 线，入编即停用（enabled:false），演练时手动开。
    "wf-a8c7b5",
    "wf-a08949",
    "wf-40fa8d",
    "wf-7bc4d1",
}

# 2026-09-02 监督面闭卷审计退役（#225）：三条 DoD 逐条真机核验后不复活，
# enabled=false 留在名册（名册是编成史实，退役靠开关不靠删行）。
#
# RETIRED 与 CONVERGED 是两种生命终点，严格互斥：CONVERGED 是「使命完成收敛」
# （enabled=false 留名册），RETIRED 是「闭卷审计退役、判定不复活」。退役线仍属
# ENROLLED（历史入编史实），故不进 loader 全集断言（语义同 CONVERGED/
# CLOSED_BY_SUPERVISOR）。任一条被塞回 CONVERGED →
# test_the_retired_lines_stay_in_the_roster_but_off 的互斥断言必红（语义污染守卫）。
RETIRED = {
    "wf-7cd0a7",  # ronin-bus-redesign
    "wf-c106b9",  # ronin-entry-unify
    "wf-e7b0dd",  # ronin-e7b0dd
    # 2026-09-02 第二批闭卷审计退役：同样逐条真机核验后不复活。
    "wf-66300e",  # ronin-dr-v4（deep-research V4；R8 判据脚本原样重跑 pass=true）
    "wf-e6560a",  # Claude Code 会话检索 MCP（生产 checkout 亲跑 ACCEPTANCE: PASS）
    # 2026-09-02 第三批：逐条核「产物是否真在生产分支上」后退役（用户口径：
    # 真做完了就应该能合并-部署-上线）。核法是把产物 diff 的特征行拿去生产分支
    # grep，而不是 merge-base --is-ancestor（squash merge 会让后者全面假阴）。
    "wf-386b2f",  # ronin-cost-observ（生产 Prometheus 上 cost_obs:*:ratio 五条全有值）
    "wf-fa53cb",  # ronin-ds-quota（quota-api main@8bd7caf4 含 ds-collect.sh；timer active）
    "wf-fdd6ac",  # ronin-cal-export（calendar-agent master 含 a1f6089d 与 web/export-image.ts）
    "wf-a6cfea",  # lexicon 术语锚定（lexicon main#9 含 S1 铺仓码，2 个 lexicon timer active）
    "wf-e313be",  # ronin-cgspec（chatgroup-daemon main 含 P1/P2/P3/P4 四批产物）
    #             ⚠️ 遗留：promotion 放行本身仍悬置等用户拍板（该线 terminal 原文
    #             「promotion 放行属用户单独拍板、不在本线 terminal 内」）。退役不代表已上线。
    # 2026-09-02 第四批：核法同第三批（特征行 grep 生产分支），但**先按 remote_url
    # 推 canonical**——上一轮我用 `git rev-parse --git-common-dir` 推，对「从 worktree
    # 再切出的 worktree」会指到中间那棵树，据此得出的「未上线」结论是假的。
    "wf-3f87f3",  # ronin-platform-pump。名下 4 单在 loop-engine-deep-research-plugin
    #             canonical 特征行各 8/8 命中。它 08-31 驻停等的那个 run 早已终态，
    #             09-02 13:19 复活后 38 分钟落完 C5 五件套并自行收线（13:57 terminal）。
    #             ——即第 170 行那条「落完五件套后再进 RETIRED」的预告已兑现。
    "wf-a87b04",  # ronin-wf-robust。3 单：2 单反向撤销干净应用，1 单特征行 6/6 命中。
    "wf-d002a6",  # ronin-eventify。6 单：2 单反向撤销干净应用、3 单特征行各 6/6 命中、
    #             1 单产品 diff 为空（纯协议单）。上一批注记的 dev-fg-ef0706e1962a
    #             已随 PR #233 收割，本轮复核确认在产。
}

# 2026-09-02 监督面明确【不】退役的四条，理由逐条写死，防下一班误收：
# - wf-197430 / wf-5664e5：它们的开发单派在 record.json 的 dispatched_by 字段上线之前，
#   归不到线上，**我没能核产物去向**。未审不入 RETIRED（该集合断言的是「闭卷审计通过」）。
# - wf-9b5931：**上一轮我把它写成「README 表格行未命中的文档漂移」，那句是错的。**
#   本轮按 remote_url 推真 canonical 重核，两笔产物是整体缺失、不是文档漂移：
#     dev-fg-81dbb77434fa（goal-agent，新增 253 行）→ 命中 0/8，**本轮已补收割**
#       （goal-agent PR #68，main ba103b1 -> 80455c3，亲跑 check-line-set-model GREEN）
#     dev-fg-b0ea914caf0e（agent-runtime，新增 102 行）→ 命中 2/8，补丁撞
#       src/dispatch.ts:2943（main 自其 base 已前进 84 个提交），已退回该线以新 main
#       为 base 重派，闸不重批。**这一笔落地并被收割前，本线保持 enabled。**
# - wf-c22907：2026-09-08 定期复活，**名册当前没有「定期复活」这种终点语义**，
#   按 CONVERGED 或 RETIRED 任一种退役都会把它错误固化。已立案补该语义。

# 2026-08-28 监督面第二波扩编收编：9 候选 8 close/fold、1 enroll。唯一入编
# wf-7cd0a7（agent-bus 通信层重设计，wf-7cd0a7/goal.md 2026-08-28 re-scope），
# seat opencode-gpt-sol——该座位首次跑 goal line。与 MIGRATED/OPENED 分开：
# 它既不是 babysitter 存量迁移，也不是 08-27「都派出去」那批新开线。
# 2026-08-28 第三波入编：用户 18:22 拍板「按 w-577103 方向对引擎/goal-driven/dd
# 深度重构，自举开发」。wf-d002a6 是事件化重构线（E1 决议事件桥 / E2 等待收敛
# 进图 / E3 terminal 视图化 / E4 协议收敛 / E5 开线 MCP 面），改造对象就是本
# 引擎自己——goal 线跑在 fleet-graph 上、改 fleet-graph 的代码、经 fleet-graph
# 的 dd 管线交付。方向 SSoT：wiki w-577103；执行卷：work folder wf-d002a6。
ENROLLED = {
    # 2026-08-29 步 7 换座真机演练 scratch 线（dev-fg-6925540673b0，決议 seq 678）：
    # wf-9b5931 的 DoD 演练载体，演练毕可退编。
    "wf-a8c7b5",
    "wf-7cd0a7",
    "wf-d002a6",
    # 2026-08-29 第四波（用户「Deep research，chat group…重构开始」+ /loop 端到
    # 端授权）：chatgroup-daemon 重构实施线复活入编，GLM 智能位（家族分流令）。
    "wf-e313be",
    # 2026-08-29 第五波（用户 15:4x 原话：New API 可观测 + Coding Plan 前端质量
    # 展示，goal-driven 端到端）：DS 供应商额度压测观测线。goal SSoT：wf-fa53cb。
    "wf-fa53cb",
    # 2026-08-30 第六波（用户令：监督面图化走自举，监督面监督其开发/部署/运行/
    # 监督）：supervisor 模式抽象落地线——传感层 read-model(:7494)、E5-E7 事件、
    # 收割反应器(allowlist 先行)、破障/人话汇报节点。goal/design SSoT：wf-216dc3。
    "wf-216dc3",
    # 2026-08-30 第七波（用户拍板 B：DR 重档在 fleet-graph research_pipeline 上
    # 彻底重构，R1-R8 严格依赖序为硬约束）：deep-research V4 线。
    # goal/宪法差距表 SSoT：wf-66300e；诊断：wf-b9be03。
    "wf-66300e",
    # 2026-08-31 第八波（用户拍板「要 goal driven agent 开发」）：calendar-agent
    # 日程页一键导出打码图片。名册里第一条**产品线**——此前每条线的题目都是
    # fleet-graph 自己或它的邻居基建，这条改的是一个独立产品仓
    # (Dandi007/calendar-agent)。验收 harness 先于实现存在（@2b0b8164 两个门
    # 实测正确变红）。goal/spec/golden-order SSoT：wf-fdd6ac。
    "wf-fdd6ac",
    # 2026-08-31 第九波（用户令「彻底全部退役 glm52」）：glm-5.2 退役收尾线。
    # 调用方全量切 glm-5.3 → 7 天观察窗零成功调用 → 网关摘别名与展开产物；
    # 互锁五步序不可颠倒，观察窗不得跳过。goal/golden-order/findings SSoT：wf-c22907。
    "wf-c22907",
    # 2026-08-31 第十波（用户令「把所有入口进行一个整体统一，然后 goal-driven 来
    # 自举这个工作」）：舰队入口整体统一线。U1 goal MCP 独立面(:5611) → U2 入册
    # 流水线（提交→queue→/v1/enrollments→E8→挂板，放行权留监督面）→ U3 入口总表
    # → U4 自举 e2e（本线为统一入口第一个 dogfood 用户）。原则：统一的是提交不是
    # 点火。goal/spec/golden-order SSoT：wf-c106b9。
    "wf-c106b9",
    # 2026-09-01 第十一波（用户令「两条都入编，别 hold」）：两条经 U2 入册流水线
    # 提交的候选线，同批放行。
    # wf-a6cfea = lexicon（agent 输出文本可读性治理）：入编时 M1 三条判据全红、
    #   判据脚本尚未存在——判据全红是 goal 线的正常起点，不是 hold 理由；
    #   首轮须把 verify-m1.sh 补成「存在且诚实报红」。
    # wf-e6560a = agent-session-mcp（Claude Code 会话检索 MCP）：M1 由 dd 单
    #   dev-fg-67bf15e27dd2 实现中，本线接手 M2/M3；判据脚本已存在且逐条报 FAIL，
    #   但打印 FAIL 却 exit 0，首轮修成用退出码说话。
    "wf-a6cfea",
    "wf-e6560a",
    # 2026-09-02 第十二波入编（用户开线令两条：「都走 MCP 不要走 native port，MCP 只需要
    # 保证工具对就好，native port 就可能调用错误」+「过时的基建都下线——比如老的 loop
    # engine 的」，以「开 line 来开发吧」收尾）。经 goal MCP 走完 goal_enroll -> goal_admit，
    # 放行裁决 msg_01M1GGC30C72MHYR5WXQ2WF82Q（板 seq 2053）。
    # wf-525fd4 = 全舰入口收敛到 MCP。立线证据：同一能力经 native 面恒 FORBIDDEN、
    #   经 MCP 网关一次通过，监督面曾据前者把 wf-818a89 误记为「唯一在等管理员」数日。
    # wf-3ffd90 = 过时基建下线。⚠️ 其 goal 已钉住依赖陷阱：/data/ronin/secrets/ 是现役
    #   令牌根（入编门禁 6 硬编码指向它），不可随 /data/ronin 整目录删除。
    "wf-525fd4",
    "wf-3ffd90",
    # 2026-09-01 U4 放行（用户令「把舰队恢复到全速运转」）：wf-c106b9 的自举 e2e
    # 载体，统一入册流水线第一个 dogfood 用户。该线驻停 16h 等的就是这一条。
    # max_rounds=3 沿用入册申请声明（演练线，非常驻），故与 wf-a8c7b5 同列例外。
    "wf-e7b0dd",
    # 2026-09-02 第十三波（用户原话：「从『模型可用性』的角度去增强建设比较好」/
    # 「模型-链路 可用性 消耗 一系列的」/「派发一条 goal 线，就参考他，但是是
    # new-api-gateway 整体的，而不是之前 scnet only 的」/「impl use dsv4pro」）：
    # New API 网关**整体**的 model x channel 可用性与消耗建设线。
    # 立线事故 = 09-02 deepseek-v4-flash 网关侧 7098 成功/0 失败（请求级 0.0%）、
    # 面板全绿，而 TTFT P90 67.7s / P99 444.7s 使其对 agentic 负载事实不可用
    # ——「请求成功率 100% 的模型可以事实上不可用」。
    # 判据已先行冻结在 token-plan-report PR #1（合 main，当前诚实报红）。
    "wf-c8bce9",
    # 2026-09-03 第十四波（用户令「给 goal agent 端到端完成整体重构的开发工作」）：
    # 舰队 less-is-more 重构线 —— 把 wf-8d9737/design.md 落成生产事实。
    # 判据 SSoT = design.md §8 十六条（用户拍板 D1-D7 在 §1.2，原文在 golden-order.md），
    # 本线只实现、不得改判据。入编时 scripts/verify-lim.sh 尚不存在（liveness exit=127），
    # 首轮 M0 把它补成「存在且逐项诚实报红、退出码=失败项数」——同 wf-a6cfea / wf-e6560a 先例。
    # 放行裁决 msg_01M1HFDDBJFPVY387TP1D9YF1D（board:work-notes seq 2325）。
    # 座位 opencode-glm53（入编前该座全空，遵家族分流令；probe.py:92 已注册功能探针）。
    "wf-8d9737",
    # 2026-09-05 第十五波（用户 2026-09-04 23:5x 令「开新的 Workboard，核心是端到端地把
    # 旧引擎全部替换成新的开发方式，最终以新的形态落地」）：引擎重建线 ronin-rebuild，
    # wf-8d9737 的后继。正本 = 宪法页 v2 与目标架构页（D1-D20），验收标准 v2 二十一项在
    # wf-4601c8/design.md §4；改动单目标分支 release/wf-4601c8（自 release/wf-8d9737 切出）。
    # 入编时 scripts/verify-rebuild.sh 尚不存在（liveness 诚实红），R0 补齐。
    # 座位 opencode-glm53；implement glm-5.3-flash、两道 review glm-5.3。
    "wf-4601c8",
    # 2026-09-05 第十六波（用户令「给 goal-agent 去端到端实现吧，等他实现完了你来验收」）：
    # ADHD 多模型化线。改的是一个 fork 来的第三方仓（Dandi007/adhd），把它唯一的 LLM 出口从
    # Claude Agent SDK 换成 agent-run——名册里第二条题目对象不是本机基建的线（第一条是
    # wf-fdd6ac 的 calendar-agent）。四批验收命令 S1-S4 在入编前已真机空跑：S2 现红于
    # "No default chain for qwen3.8-max@opencode"，那正是它要补的东西，红得其所。
    "wf-194310",
}

# 2026-08-29 复活：曾在 MIGRATED 里 enabled=false 停摆的线被用户令重新点亮。
# 与 ENROLLED 分开——它们不是新线，folder 与考卷都是存量，只是驱动引擎从
# ronin pump 换成本引擎、座位按家族分流令改派。
# 2026-08-31 监督面收线：C5 五次冷启动终验失败取证完备，B 拍板由 wf-66300e 承接，
# 本线历史使命结束（progress 终局代账 + wf_save 归档在案）。成员资格保留（全集断言），
# enabled 期望移除。
CLOSED_BY_SUPERVISOR: set[str] = {
    # 2026-09-05 监督面结案 wf-8d9737（ronin-lim，用户 2026-09-04 23:5x 令「停掉重开」）：
    # D18-D20 与宪法 v2 取代了它 goal/design 里的派单与 gate 机制，继续叠更正等于补丁
    # 叠补丁。既不是 CONVERGED（使命未完成）也不是 RETIRED（产物未闭卷审计），是监督面
    # 收线。产物保留在 release/wf-8d9737（tip 29f2130），后继线 wf-4601c8 自该 tip 切分支。
    "wf-8d9737",
}
# 2026-09-02 监督面推翻上一任的收口判断：wf-3f87f3 移出本集合，故此处为空
# （集合本身保留，语义仍在，供日后再有需要时使用）。
#
# 推翻理由（机械可核，不是改主意）：收口时该线并非「历史使命结束」，而是还剩
# 一项交付未落——其 goal 第 20-22 行写明「C5 五件套逐字落 progress …这是本线
# 最后一项交付」，而它 2026-08-31 03:17:20 驻停，等 C5 终验 run（pid 802798）
# 自然跑完。真机核验：
#   ps -p 802798                → 进程不存在
#   .../08b7fe38/drain.json     → {"status":"done","ended":1788118289592}
#                                 （= 2026-08-31 03:31:29 CST）
#   .../loop-events.jsonl 末条  → {"kind":"round_end","round":68,"errors":0}
# 即它驻停 14 分钟后那个 run 就终态了，此后 2 天 10 小时无人唤醒；而目前没有
# 任何告警覆盖「blocked 等一个已消失的外部事件」这一类（已立案 wf-6475fd）。
# 唤醒依据：板 seq 2022 / msg_01M1G8MGYTKYKC5F8EBCHPNQTA。
# 该线落完五件套后按 done 纪律自行收线，届时再进 RETIRED，不是提前塞回本集合。

REVIVED = {
    "wf-3f87f3",  # deep-research 平台化收口（C1-C6），DS 智能位
}

MIGRATED = {
    "wf-287e81",
    "wf-5664e5",
    "wf-386b2f",
    "wf-7bc4d1",
    "wf-6475fd",
    "wf-9b5931",
    "wf-541832",
    "wf-3f87f3",
    "wf-40fa8d",
}


class TestTheShippedConfigLoads:
    def test_the_real_loader_accepts_it(self) -> None:
        config = SchedulerConfig.from_json(CONFIG)
        assert {line.folder_id for line in config.lines} == MIGRATED | OPENED | ENROLLED | REVIVED

    def test_every_line_names_a_seat_and_an_alias(self) -> None:
        for line in SchedulerConfig.from_json(CONFIG).lines:
            assert line.seat, line.folder_id
            assert line.alias, line.folder_id

    def test_it_no_longer_points_at_the_retired_stacks_gate(self) -> None:
        """The shipped config used to name /data/ronin/maintenance-stop. That
        whole gate was retired on the 2026-08-26 ruling; the roster below is
        what holds lines now, and the emergency stop lives at a fleet-graph
        path. A config still naming the old file would make the new scheduler
        depend on a retired stack's directory."""
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        assert "maintenance_stop" not in raw
        # Not a substring check on the file: `_provenance` legitimately names
        # /data/ronin/babysitter-20260822.sh as where these values came from,
        # and a grep would have failed on the one line that should say it.
        assert not any(str(v).startswith("/data/ronin") for v in raw.values() if isinstance(v, str))
        config = SchedulerConfig.from_json(CONFIG)
        assert str(config.maintenance_stop_path) == "/data/fleet-graph/maintenance-stop"

    def test_no_line_carries_the_retired_mcp(self) -> None:
        """babysitter passed --session-mcp-allow loop-engine-development to
        every line. That MCP is retired (P4); a migrated config that still
        named it would hand every seat a dead tool."""
        assert "loop-engine-development" not in CONFIG.read_text(encoding="utf-8")

    def test_the_bounds_are_the_ones_the_old_pump_ran_with(self) -> None:
        """9999, not the LineSpec default of 10. Migration is equivalence, and
        silently tightening a bound would end lines that used to keep going."""
        for line in SchedulerConfig.from_json(CONFIG).lines:
            if line.folder_id in ("wf-a8c7b5", "wf-e7b0dd"):
                # 步 7 演练 scratch 线：单轮即够（换座→取证→完），非迁移线。
                assert line.max_rounds in (1, 3), line.folder_id
                continue
            assert line.max_rounds == 9999, line.folder_id

    def test_exactly_the_current_batch_is_switched_on(self) -> None:
        """P7 放量的当前批次，写在这里而不是写在某人的记忆里。

        放量下一批 = 改这个断言。改不动就说明有人在没改测试的情况下动了
        配置——那正是要拦的事。
        """
        enabled = {
            line.folder_id for line in SchedulerConfig.from_json(CONFIG).lines if line.enabled
        }
        expected = (BATCH_TWO | OPENED | ENROLLED | REVIVED) - CONVERGED - RETIRED
        assert enabled == expected - CLOSED_BY_SUPERVISOR

    def test_the_converged_lines_stay_in_the_roster_but_off(self) -> None:
        """收编（2026-08-28）不是删除：金丝雀 wf-40fa8d 等三线 terminal=done /
        使命完成后 enabled=false，但留在名册里——名册同时是编成史实。原
        「金丝雀在后续批次保持在线」的断言随金丝雀本身收编而退役。"""
        lines = {line.folder_id: line for line in SchedulerConfig.from_json(CONFIG).lines}
        for folder_id in CONVERGED:
            assert folder_id in lines, folder_id
            assert not lines[folder_id].enabled, folder_id

    def test_the_retired_lines_stay_in_the_roster_but_off(self) -> None:
        """退役（2026-09-02 监督面闭卷审计 #225）不是删除：wf-7cd0a7 / wf-c106b9 /
        wf-e7b0dd 三条 DoD 逐条真机核验后判定不复活，enabled=false 但留在名册里
        ——名册同时是编成史实。RETIRED 与 CONVERGED 是两种生命终点，严格互斥：
        任一条退役线被塞回 CONVERGED，本断言必红（语义污染守卫）。"""
        assert RETIRED.isdisjoint(CONVERGED)
        lines = {line.folder_id: line for line in SchedulerConfig.from_json(CONFIG).lines}
        for folder_id in RETIRED:
            assert folder_id in lines, folder_id
            assert not lines[folder_id].enabled, folder_id

    def test_the_archived_lines_stay_out(self) -> None:
        """wf-d726aa / wf-8c8ae3 已归档（题目对象随 loop-engine 退役，或诉求
        已被 fleet-graph 的不变量兑现）。它们本就不在名册里，这条断言防的是
        「批量打开」时把它们顺手带进来。"""
        folders = {line.folder_id for line in SchedulerConfig.from_json(CONFIG).lines}
        assert "wf-d726aa" not in folders
        assert "wf-8c8ae3" not in folders

    def test_every_line_states_its_rollout_position(self) -> None:
        """`enabled` 默认是 False，所以漏写等于不跑——不会误起线，但会静默
        不跑。逐条写出来，让「这条到底该不该跑」是文件里的事实。"""
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        for entry in raw["lines"]:
            assert isinstance(entry.get("enabled"), bool), entry["folder_id"]

    def test_folder_ids_are_well_formed(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        for entry in raw["lines"]:
            assert entry["folder_id"].startswith("wf-")
            assert len(entry["folder_id"]) == 9


class TestTheRunbookMatchesTheCode:
    """事故里没人会翻 docstring。

    紧急停机口的地址写在 docs/operating.md 上，运维会照抄。抄到一个和代码
    默认值对不上的路径，命令会安静地什么都不做——而那正是最不该安静的时刻。
    """

    RUNBOOK = Path(__file__).resolve().parent.parent / "docs" / "operating.md"

    def test_every_copyable_command_names_the_path_the_code_reads(self) -> None:
        """按代码块查，不按全文查。

        第一版只断言「正确路径在文中出现过」——文档里同时留着一句「路径曾是
        /data/ronin/...」的历史说明，于是一条 `cat > /data/ronin/...` 的错命令
        照样通过。出现过不等于抄下来是对的；要查的是可复制的那几行。"""
        from fleet_graph.scheduler.daemon import DEFAULT_MAINTENANCE_STOP

        current = str(DEFAULT_MAINTENANCE_STOP)
        in_code, checked = False, 0
        for line in self.RUNBOOK.read_text(encoding="utf-8").splitlines():
            if line.startswith("```"):
                in_code = not in_code
                continue
            if in_code and "maintenance-stop" in line:
                checked += 1
                assert current in line, line
        assert checked >= 2, "runbook lost its stop/release commands"

    def test_the_runbook_names_the_canary_currently_switched_on(self) -> None:
        assert CANARY in self.RUNBOOK.read_text(encoding="utf-8")


class TestTheAcceptanceDeclarations:
    """R0d 首批声明曾是 wf-a08949 一条线（:7455/:7460 端口探测，目的是让
    last_acceptance 事实链首次真机跑通）。2026-08-28 老引擎退役、端口释放，
    `grep -c` 匹配 0 行 exit 1，该判据恒失败——随线收编整段删除。此后名册里
    不该再有任何线携带指向老引擎端口的判据。"""

    def test_the_retired_probes_are_gone(self) -> None:
        line = {c.folder_id: c for c in SchedulerConfig.from_json(CONFIG).lines}["wf-a08949"]
        assert not line.acceptance
        raw = CONFIG.read_text(encoding="utf-8")
        assert ":7455" not in raw
        assert ":7460" not in raw

    def test_every_declared_line_also_declares_a_cwd(self) -> None:
        for line in SchedulerConfig.from_json(CONFIG).lines:
            if line.acceptance:
                assert line.acceptance_cwd, f"{line.folder_id} declares commands but no cwd"
                for argv in line.acceptance:
                    assert argv and all(isinstance(part, str) for part in argv)

    def test_no_line_currently_declares_acceptance(self) -> None:
        declared = {
            line.folder_id for line in SchedulerConfig.from_json(CONFIG).lines if line.acceptance
        }
        assert declared == set()
