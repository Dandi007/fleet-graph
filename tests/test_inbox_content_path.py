"""E1 gap #4: the line-process inbox content path (alias pass-through + credential
convergence).

The coordinator's inbox content never reached the coordinator because (A) the
scheduler never threaded ``--alias`` into the line process and (B) the line's
inbox drain authenticated with the service token, which structurally 403s on
``agent:*``. This module pins the three regression criteria from
design-e1-gap4-inbox-content-path.md:

1. **alias pass-through**: ``LaunchSpec(alias=...)`` argv carries ``--alias``;
   the scheduler's ``spec_for`` threads the roster alias into the launch; a
   ``LineConfig(alias=...)`` built by ``build_line`` yields a real ``Inbox``
   (not the null inbox), and its ``drain_then_ack`` receives and persists a
   controlled message.
2. **credential mismatch negative**: a faithful fake models the real ACL
   (service-token auth on ``agent:*`` -> 403, line-token -> 200) and proves the
   line inbox authenticates with the line's own token, never the service token;
   the service-token 403 is asserted as the pre-fix failure mode and does not
   occur on the fixed path.
3. **degradation**: a missing line token degrades the drain explicitly
   (recorded under the run root) and never faults the line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from fleet_graph.bus.client import BusClient
from fleet_graph.bus.inbox import Inbox, InboxForbidden
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.graphs.runner import LineConfig, build_line
from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig
from fleet_graph.scheduler.launcher import LaunchSpec


def _line_token_env(monkeypatch: Any, tmp_path: Path) -> None:
    """Point the shared line-token template at tmp_path, never the real host."""
    monkeypatch.setenv("FLEET_GRAPH_LINE_TOKEN_PATH", str(tmp_path / "secrets" / "{alias}.token"))


def _write_line_token(tmp_path: Path, alias: str, token: str = "line-token-abc") -> None:
    path = tmp_path / "secrets" / f"{alias}.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")


class FakeAclTransport:
    """Faithful to the real channel ACL.

    The ``agent:{alias}`` channel is owner-only readable and the owner is the
    line's pump agent. Presenting the fleet-graph service token is a structural
    403; presenting the line's own token is 200. Only the line's token is ever
    accepted, and every request's bearer token is recorded so a test can name
    the exact credential that was used.
    """

    def __init__(
        self,
        line_token: str,
        delivery: dict[str, Any] | None = None,
    ) -> None:
        self.line_token = line_token
        self.delivery = delivery
        self.seen_tokens: list[str] = []

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        token = str(headers.get("Authorization", "")).removeprefix("Bearer ")
        self.seen_tokens.append(token)
        if token != self.line_token:
            return 403, {"code": "FORBIDDEN", "message": "no read ACL on agent:*"}
        if url.endswith("/consume"):
            return 200, {"deliveries": [self.delivery] if self.delivery else []}
        if url.endswith("/ack"):
            return 200, {}
        return 404, "not found"


def controlled_delivery(message_id: str = "msg-controlled-1") -> dict[str, Any]:
    return {
        "delivery_id": f"del-{message_id}",
        "lease_token": f"lease-{message_id}",
        "attempt": 0,
        "message": {
            "message_id": message_id,
            "sender_agent_id": "drill-agent",
            "created_at": "2026-08-29T10:00:00.000Z",
            "payload": {
                "body": "a controlled message",
                "depth": 1,
                "from_alias": "drill",
                "from_agent_id": "drill-agent",
                "thread_id": "t-1",
                "sent_at": "2026-08-29T10:00:00Z",
            },
        },
    }


class TestAliasPassthrough:
    def test_launch_spec_argv_carries_alias(self) -> None:
        spec = LaunchSpec(folder_id="wf-1", seat="s", alias="ronin-eventify")
        argv = spec.argv()
        assert argv[argv.index("--alias") + 1] == "ronin-eventify"

    def test_launch_spec_without_alias_omits_the_flag(self) -> None:
        spec = LaunchSpec(folder_id="wf-1", seat="s")
        assert "--alias" not in spec.argv()

    def test_spec_for_threads_the_roster_alias(self, tmp_path: Path) -> None:
        scheduler = Scheduler(
            SchedulerConfig(
                lines=[LineSpec(folder_id="wf-1", seat="s", alias="ronin-eventify", enabled=True)],
                run_root=tmp_path / "runs",
            )
        )
        spec = scheduler.spec_for(scheduler.config.lines[0])
        assert spec.alias == "ronin-eventify"
        assert spec.argv()[spec.argv().index("--alias") + 1] == "ronin-eventify"

    def test_build_line_yields_a_real_inbox_for_an_alias(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """The rollback contrast, pinned: without the alias threading the line
        used ``_NullInbox`` and the coordinator's inbox content was structurally
        empty (wf-d002a6 coord/round-{1,2,3}-input.json)."""
        _line_token_env(monkeypatch, tmp_path)
        _write_line_token(tmp_path, "drill")
        transport = FakeAclTransport("line-token-abc", delivery=controlled_delivery())

        def bus_factory(token: str) -> BusClient:
            return BusClient(token=token, transport=transport)

        monkeypatch.setattr("fleet_graph.graphs.runner.BusClient", bus_factory)
        _, deps = build_line(
            LineConfig(folder_id="wf-1", seat="s", run_root=tmp_path / "runs", alias="drill")
        )
        assert isinstance(deps.inbox, Inbox), "the line must run a real Inbox, not _NullInbox"

    def test_drain_receives_and_persists_a_controlled_message(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _line_token_env(monkeypatch, tmp_path)
        _write_line_token(tmp_path, "drill")
        transport = FakeAclTransport("line-token-abc", delivery=controlled_delivery())
        monkeypatch.setattr(
            "fleet_graph.graphs.runner.BusClient",
            lambda token: BusClient(token=token, transport=transport),
        )
        _, deps = build_line(
            LineConfig(folder_id="wf-1", seat="s", run_root=tmp_path / "runs", alias="drill")
        )

        persisted: list[Any] = []
        deps.inbox.drain_then_ack(persisted.extend)

        assert persisted, "inbox_messages must be non-empty on the fixed path"
        assert persisted[0]["message_id"] == "msg-controlled-1"


class TestCredentialMismatch:
    def test_the_line_inbox_uses_the_line_token_not_the_service_token(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """The service token is present in the environment and the channel ACL
        would 403 it; the fixed path must build the inbox client with the line's
        own token and never touch the service token for `agent:*`."""
        _line_token_env(monkeypatch, tmp_path)
        _write_line_token(tmp_path, "drill")
        monkeypatch.setenv("FLEET_GRAPH_BUS_TOKEN", "service-token-xyz")
        transport = FakeAclTransport("line-token-abc", delivery=controlled_delivery())

        built_with: list[str] = []
        monkeypatch.setattr(
            "fleet_graph.graphs.runner.BusClient",
            lambda token: built_with.append(token) or BusClient(token=token, transport=transport),
        )
        _, deps = build_line(
            LineConfig(folder_id="wf-1", seat="s", run_root=tmp_path / "runs", alias="drill")
        )

        drained: list[Any] = []
        deps.inbox.drain_then_ack(drained.extend)
        assert drained, "the line-token 200 path must actually drain"
        assert built_with == ["line-token-abc"], (
            "the inbox client was built with the wrong credential family"
        )
        assert set(transport.seen_tokens) == {"line-token-abc"}, (
            "the service token must never touch the agent:* channel on the fixed path"
        )

    def test_service_token_403_is_the_pre_fix_failure_mode(self, tmp_path: Path) -> None:
        """Model the real ACL on the agent:* channel: a service-token client is
        structurally 403'd on consume. This is the pre-fix failure mode (and is
        by design -- the channel ACL is deliberately not widened), and it is
        asserted to be absent on the fixed path in the test above."""
        transport = FakeAclTransport("line-token-abc", delivery=controlled_delivery())
        service_client = BusClient(token="service-token-xyz", transport=transport)
        inbox = Inbox(service_client, "drill")

        try:
            inbox.drain_then_ack(lambda messages: None)
        except InboxForbidden:
            pass
        else:
            raise AssertionError("the service token must structurally 403 on agent:*")


class TestDegradation:
    def test_missing_line_token_degrades_and_records_explicitly(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _line_token_env(monkeypatch, tmp_path)
        _, deps = build_line(
            LineConfig(folder_id="wf-deg", seat="s", run_root=tmp_path / "runs", alias="drill")
        )

        assert not isinstance(deps.inbox, Inbox), "no token -> no real Inbox"
        persisted: list[Any] = []
        result = deps.inbox.drain_then_ack(persisted.extend)  # must not raise
        assert persisted == []
        assert result == ([], [])

        record = json.loads((tmp_path / "runs" / "inbox-degraded.json").read_text())
        assert record["alias"] == "drill"
        assert record["reason"] == "missing"

    def test_an_empty_token_file_degrades_as_empty(self, tmp_path: Path, monkeypatch: Any) -> None:
        _line_token_env(monkeypatch, tmp_path)
        (tmp_path / "secrets").mkdir(parents=True, exist_ok=True)
        (tmp_path / "secrets" / "drill.token").write_text("   \n", encoding="utf-8")
        _, deps = build_line(
            LineConfig(folder_id="wf-deg", seat="s", run_root=tmp_path / "runs", alias="drill")
        )
        deps.inbox.drain_then_ack(lambda messages: None)
        record = json.loads((tmp_path / "runs" / "inbox-degraded.json").read_text())
        assert record["reason"] == "empty"

    def test_an_unsafe_alias_degrades_without_touching_the_filesystem(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _line_token_env(monkeypatch, tmp_path)
        _, deps = build_line(
            LineConfig(
                folder_id="wf-deg", seat="s", run_root=tmp_path / "runs", alias="../../etc/passwd"
            )
        )
        deps.inbox.drain_then_ack(lambda messages: None)
        record = json.loads((tmp_path / "runs" / "inbox-degraded.json").read_text())
        assert record["reason"] == "unsafe"

    def test_a_missing_token_never_faults_the_line(self, tmp_path: Path, monkeypatch: Any) -> None:
        """The degraded inbox feeds the real graph: the line reaches a clean
        terminal, not a fault, solely because its inbox credential is absent."""
        _line_token_env(monkeypatch, tmp_path)
        _, built = build_line(
            LineConfig(folder_id="wf-deg", seat="s", run_root=tmp_path / "runs", alias="drill")
        )
        degraded = built.inbox

        artifacts = FakeArtifacts()
        graph_deps = LineDeps(
            coordinator=FakeCoordinator([{"verdict": "done", "reason": "ok"}]),
            worker=FakeWorker(),
            inbox=degraded,
            artifacts=artifacts,
            guards=LineGuards(bounds=LineBounds(max_rounds=3)),
            folder_id="wf-deg",
        )
        compiled = build_goal_line_graph(graph_deps).compile(checkpointer=InMemorySaver())
        compiled.invoke(
            {"round_no": 1},
            config={"configurable": {"thread_id": "deg1"}, "recursion_limit": 100},
        )
        assert artifacts.terminal["terminal"] == "done"
        assert artifacts.terminal["pump_fault"] is False


class FakeCoordinator:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)

    def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
        return self.script.pop(0) if self.script else {"verdict": "done", "reason": "script end"}


class FakeWorker:
    def turn(self, prompt: str, round_no: int) -> dict[str, Any]:
        return {
            "schema_version": "fleet-graph.worker-turn-report/v1",
            "turn_id": f"t-{round_no}",
            "outcome": "completed",
            "summary": "did it",
            "did": ["completed action"],
            "files": [],
            "self_tests": [],
            "blocker": None,
        }


class FakeArtifacts:
    def __init__(self) -> None:
        self.terminal: dict[str, Any] | None = None

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        return True

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> str:
        return "worker-report.json"

    def write_terminal(self, **kwargs: Any) -> str:
        self.terminal = kwargs
        return "terminal.json"

    def write_fault_terminal(self, **kwargs: Any) -> str:
        return "fault"
