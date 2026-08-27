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
        assert {line.folder_id for line in config.lines} == MIGRATED | OPENED

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
            assert line.max_rounds == 9999, line.folder_id

    def test_exactly_the_current_batch_is_switched_on(self) -> None:
        """P7 放量的当前批次，写在这里而不是写在某人的记忆里。

        放量下一批 = 改这个断言。改不动就说明有人在没改测试的情况下动了
        配置——那正是要拦的事。
        """
        enabled = {
            line.folder_id for line in SchedulerConfig.from_json(CONFIG).lines if line.enabled
        }
        assert enabled == BATCH_TWO | OPENED

    def test_the_canary_stays_on_through_later_batches(self) -> None:
        """放量是叠加不是替换。把金丝雀关掉换新线，会丢掉唯一一条已经有
        真机证据的线。"""
        enabled = {
            line.folder_id for line in SchedulerConfig.from_json(CONFIG).lines if line.enabled
        }
        assert CANARY in enabled

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
