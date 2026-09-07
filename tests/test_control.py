"""控制面纯单元测试；WF、Git 与进程均为替身。"""

import asyncio
import base64
from types import SimpleNamespace

import pytest

from fleet_graph import service
from fleet_graph.engine import key
from fleet_graph.ports import MCPPort, ReplyPort
from fleet_graph.service import Control, locked


class FakeWF:
    def __init__(self):
        self.ids = []

    async def goal(self, folder_id):
        self.ids.append(folder_id)
        return "完整目标正文"


class FakeGit:
    def __init__(self):
        self.prepared = []

    def prepare_repo(self, repo, branch, takeover):
        self.prepared.append(repo)
        return {**repo, "release_head": "a" * 40}


class FakeEngine:
    def __init__(self, store, owner):
        self.store, self.owner = store, owner
        self.runtime = self.commands = self

    def request(self, kind, request_id, current, caller=None, reply_to=None):
        goal = self.store.read()["goal"]
        self.store.enqueue(
            {
                "request_id": request_id,
                "type": kind,
                "goal_id": goal["goal_id"],
                "goal_version": goal["version"],
                "input": current,
                "caller": caller,
                "reply_to": reply_to,
            }
        )

    async def collect(self):
        self.owner.collects += 1
        await asyncio.sleep(0)

    async def stop(self, ticket):
        self.owner.stops.append(ticket)

    async def inspect(self, ticket):
        return self.owner.observed


@pytest.fixture
def setup(tmp_path, monkeypatch):
    state = SimpleNamespace(alive={}, spawned=[], collects=0, stops=[])

    def spawn(goal_id):
        pid = 100 + len(state.spawned)
        state.spawned.append(goal_id)
        state.alive[pid] = f"boot:{pid}"
        return {"pid": pid, "identity": state.alive[pid]}

    monkeypatch.setattr(service, "identity", lambda pid: state.alive.get(pid))
    wf, git = FakeWF(), FakeGit()
    control = Control(tmp_path, {}, git=git, wf=wf, spawner=spawn)
    monkeypatch.setattr(control, "engine", lambda store: FakeEngine(store, state))
    request = {
        "schema": "goal.enroll/2",
        "request_id": "enroll-one",
        "work_folder": "opaque:token/+",
        "title": "目标",
        "source_branch": "release/test",
        "repos": [
            {
                "path": "/group/repo",
                "remote": "origin",
                "target_branch": "main",
                "acceptance": ["test command"],
            }
        ],
    }
    return control, request, state, wf, git


def enroll(setup):
    return asyncio.run(setup[0].enroll(setup[1]))["goal_id"]


def test_enroll_idempotency_and_opaque_wf(setup):
    control, request, state, wf, git = setup
    goal_id = enroll(setup)
    assert asyncio.run(control.enroll(request))["goal_id"] == goal_id
    assert wf.ids == ["opaque:token/+"]
    assert len(git.prepared) == len(state.spawned) == 1
    repo_id = key("/group/repo", "origin")
    assert git.prepared[0]["operation_id"] == key(goal_id, repo_id, "prepare")
    assert "operation_id" not in request["repos"][0]


def test_message_replay_after_steer_preserves_original_version(setup):
    control, *_ = setup
    goal_id = enroll(setup)
    caller, reply_to = {"kind": "human", "id": "u"}, {"mailbox": "one"}
    control.message(goal_id, "message-one", "正文", caller, reply_to)
    control.steer(goal_id, "steer-one", "新目标", 1)
    control.message(goal_id, "message-one", "正文", caller, reply_to)
    assert control.store(goal_id).read()["requests"]["message-one"]["envelope"]["goal_version"] == 1
    with pytest.raises(ValueError):
        control.message(goal_id, "message-one", "不同正文", caller, reply_to)


def test_stop_does_not_collect_beside_live_engine(setup):
    control, _, state, *_ = setup
    goal_id = enroll(setup)
    with locked(control.store(goal_id).root / "engine.lock"):
        result = asyncio.run(control.stop(goal_id, immediate=True))
    assert result["status"] == "stopping"
    assert state.collects == 0
    asyncio.run(control.stop(goal_id))
    assert control.store(goal_id).read()["stop"] == "immediate"


def test_concurrent_resume_spawns_once(setup):
    control, _, state, *_ = setup
    goal_id = enroll(setup)
    state.alive.clear()

    async def concurrent():
        return await asyncio.gather(
            control.resume(goal_id), control.resume(goal_id), return_exceptions=True
        )

    result = asyncio.run(concurrent())
    assert len(state.spawned) == 2  # enroll 一次、resume 一次。
    assert sum(isinstance(value, ValueError) for value in result) == 1
    assert state.collects == 1


def test_resume_spawn_failure_is_durable(setup):
    control, _, state, *_ = setup
    goal_id = enroll(setup)
    state.alive.clear()

    def fail(_):
        raise OSError("模拟启动失败")

    control.spawner = fail
    with pytest.raises(OSError):
        asyncio.run(control.resume(goal_id))
    assert control.status(goal_id)["status"] == "stopped"
    assert control.store(goal_id).read()["stop"] == "spawn_failed"


def test_artifact_reads_all_pages_and_rejects_traversal(setup):
    control, *_ = setup
    goal_id = enroll(setup)
    data = bytes(range(256)) * 400
    (control.store(goal_id).root / "large.log").write_bytes(data)
    output, offset = b"", 0
    while offset < len(data):
        page = control.artifact(goal_id, "large.log", offset, 997)
        output += base64.b64decode(page["data"])
        offset = page["next"]
    assert output == data
    with pytest.raises(ValueError):
        control.artifact(goal_id, "../enroll.lock")


def test_wf_resume_broken_and_full_byte_pages():
    class FakePort(MCPPort):
        def __init__(self):
            self.calls, self.broken = [], False

        async def call(self, name, args):
            self.calls.append((name, args))
            if name == "wf_resume":
                return {
                    "ok": True,
                    "verification": {"overall": "BROKEN" if self.broken else "MATCH"},
                }
            content = "不带行号\n完整文本".encode()
            offset = args["offset"]
            part = content[offset : offset + 5]
            return {
                "ok": True,
                "encoding": "base64",
                "content_revision": "same",
                "content_base64": base64.b64encode(part).decode(),
                "next_offset": offset + len(part),
                "eof": offset + len(part) >= len(content),
            }

    port = FakePort()
    assert asyncio.run(port.goal("opaque/+")) == "不带行号\n完整文本"
    assert all(args["folder_id"] == "opaque/+" for _, args in port.calls)
    port.broken = True
    with pytest.raises(ValueError, match="阻塞"):
        asyncio.run(port.goal("opaque/+"))
    assert port.calls[-1][0] == "wf_resume"


def test_reply_requires_positive_receipt():
    class FakePort:
        async def call(self, tool, arguments):
            assert arguments["idempotency_key"] == "stable-action"
            return {"ok": False, "error": "not delivered"}

    with pytest.raises(RuntimeError, match="未确认"):
        asyncio.run(
            ReplyPort(FakePort(), "send").send(
                {"request_id": "r", "caller": {}}, "正文", "stable-action"
            )
        )


def test_mcp_tools_register_without_starting_server(setup):
    control, *_ = setup
    mcp = service.build_mcp(control)
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {
        "goal_enroll",
        "goal_resume",
        "goal_stop",
        "goal_message",
        "goal_session",
        "goal_artifact",
        "goal_events",
        "goal_replies",
        "goal_observations",
    } <= names


def test_launch_uncertainty_requires_confirmation_and_lock_absence(setup):
    from fleet_graph.commands import write_json

    control, _, state, *_ = setup
    goal_id = enroll(setup)
    root = control.store(goal_id).root
    write_json(root / "launch.json", {"state": "launching"})
    # 即使确认缺席，也不能覆盖仍活跃的已知进程。
    with pytest.raises(ValueError, match="尚未退出"):
        asyncio.run(control.resume(goal_id, confirm_launch_absent=True))
    state.alive.clear()
    with pytest.raises(ValueError, match="状态不明"):
        asyncio.run(control.resume(goal_id))
    with locked(root / "engine.lock"), pytest.raises(BlockingIOError):
        asyncio.run(control.resume(goal_id, confirm_launch_absent=True))
    assert len(state.spawned) == 1
    asyncio.run(control.resume(goal_id, confirm_launch_absent=True))
    assert len(state.spawned) == 2
    events = control.store(goal_id).events()["events"]
    assert sum(event["kind"] == "engine.launch_absence_confirmed" for event in events) == 1


@pytest.mark.parametrize("observed", ["running", "lost", "succeeded"])
def test_uncertain_run_confirmation_requires_evidence_and_rechecks(setup, observed):
    control, _, state, *_ = setup
    goal_id = enroll(setup)
    state.alive.clear()
    store = control.store(goal_id)
    store.change(
        "test.uncertain",
        {},
        lambda s: s["runs"].update(
            {
                "unknown-run": {
                    "role": "goal",
                    "status": "uncertain",
                    "ticket": {"run_id": "unknown-run"},
                }
            }
        ),
    )
    state.observed = {"status": observed, "output": [{"type": "idle"}], "error": "launch_uncertain"}
    with pytest.raises(ValueError, match="证据"):
        asyncio.run(control.resume(goal_id, confirmed_absent_runs=["unknown-run"]))
    if observed == "running":
        with pytest.raises(ValueError, match="仍在运行"):
            asyncio.run(
                control.resume(
                    goal_id,
                    confirmed_absent_runs=["unknown-run"],
                    absence_evidence="核对主机进程表与runtime记录",
                )
            )
        assert store.read()["runs"]["unknown-run"]["status"] == "uncertain"
    else:
        asyncio.run(
            control.resume(
                goal_id,
                confirmed_absent_runs=["unknown-run"],
                absence_evidence="核对主机进程表与runtime记录",
            )
        )
        run = store.read()["runs"]["unknown-run"]
        assert run["status"] == "collected"
        assert run["result"]["status"] == ("failed" if observed == "lost" else "succeeded")
        assert any(event["kind"] == "run.absence_confirmed" for event in store.events()["events"])
