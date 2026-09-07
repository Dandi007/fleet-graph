"""统一 MCP 控制面；每 Goal 的运行记录与进程独立。"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import shlex
import subprocess
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fleet_graph.commands import Commands, identity, write_json
from fleet_graph.engine import Engine, key
from fleet_graph.git_ops import GitOps
from fleet_graph.ports import MCPPort, ReplyPort
from fleet_graph.protocol import ENROLL, validate
from fleet_graph.runtime import Runtime
from fleet_graph.store import Store


@contextmanager
def locked(path, nonblocking=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield


@asynccontextmanager
async def async_locked(path):
    path = Path(path)
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    guard = await asyncio.to_thread(path.open, "a")
    try:
        while True:
            try:
                await asyncio.to_thread(fcntl.flock, guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.01)
        yield
    finally:
        await asyncio.to_thread(guard.close)


class Control:
    def __init__(self, root, config, *, git=None, wf=None, spawner=None, runtime_factory=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.git = git or GitOps()
        self.wf = wf or MCPPort(config["work_folder_mcp"], config.get("mcp_timeout", 30))
        self.spawner = spawner or self.spawn
        self.runtime_factory = runtime_factory

    def store(self, goal_id):
        if (
            not isinstance(goal_id, str)
            or len(goal_id) != 24
            or any(c not in "0123456789abcdef" for c in goal_id)
        ):
            raise ValueError("未知 goal_id")
        root = self.root / goal_id
        if not (root / "events.sqlite3").is_file():
            raise ValueError("未知 goal_id")
        return Store(root)

    def engine(self, store):
        if self.runtime_factory:
            runtime = self.runtime_factory(store)
        else:
            runtime = Runtime(
                agent_run=self.config["agent_run"],
                agent_session=self.config["agent_session"],
                state_root=str(store.root / "runtime"),
                session_root=str(store.root / "sessions"),
                roles=self.config["roles"],
            )
        reply = None
        if self.config.get("reply_mcp"):
            reply = ReplyPort(MCPPort(self.config["reply_mcp"]), self.config["reply_tool"])
        return Engine(
            store,
            runtime,
            self.git,
            Commands(store.root / "commands", self.config.get("acceptance_timeout", 900)),
            reply,
            capacity=self.config.get("dd_capacity", 8),
            scribe_interval=self.config.get("scribe_interval", 60),
            warning_turns=self.config.get("warning_turns", 20),
        )

    def spawn(self, goal_id):
        # 所有配置写本 Goal 根；子进程只读这份已冻结配置。
        root = self.store(goal_id).root
        write_json(root / "config.json", self.config)
        with (root / "engine.log").open("ab") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "fleet_graph.cli",
                    "engine",
                    "--root",
                    str(self.root),
                    "--goal",
                    goal_id,
                    "--config",
                    str(root / "config.json"),
                ],
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        return {"pid": process.pid, "identity": identity(process.pid)}

    async def enroll(self, request):
        validate(request, ENROLL)
        for repo in request["repos"]:
            for command in repo["acceptance"]:
                if not shlex.split(command):
                    raise ValueError("验收命令不能为空")
            if not Path(repo["path"]).is_absolute():
                raise ValueError("repo path 必须为绝对路径")
        goal_id = key("enroll", request["request_id"])
        async with (
            async_locked(self.root / "enroll.lock"),
            async_locked(self.root / goal_id / "control.lock"),
        ):
            store = Store(self.root / goal_id)
            state = store.read()
            if state["goal"]:
                if state["goal"]["enrollment"] != request:
                    raise ValueError("重复 enroll 的内容不一致")
                if state["status"] != "preparing":
                    return self.status(goal_id)
            else:
                body = await self.wf.goal(request["work_folder"])
                goal = {
                    "goal_id": goal_id,
                    "version": 1,
                    "body": body,
                    "enrollment": request,
                    "work_folder": request["work_folder"],
                    "title": request["title"],
                    "source_branch": request["source_branch"],
                }
                store.change("goal.enroll_intent", goal, lambda s: s.update(goal=goal))
            for repo in request["repos"]:
                repo_id = key(repo["path"], repo["remote"])
                if repo_id in store.read()["repos"]:
                    continue
                for other in self.list():
                    if other["goal_id"] == goal_id:
                        continue
                    other_state = self.store(other["goal_id"]).read()
                    if other_state["goal"]["source_branch"] == request["source_branch"] and any(
                        Path(r["path"]).resolve() == Path(repo["path"]).resolve()
                        for r in other_state["repos"].values()
                    ):
                        raise ValueError("同一 release 已由其他 Goal 管理")
                store.change("repo.prepare_intent", {"repo_ref": repo_id, "repo": repo})
                prepared = await asyncio.to_thread(
                    self.git.prepare_repo,
                    {**repo, "operation_id": key(goal_id, repo_id, "prepare")},
                    request["source_branch"],
                    request.get("takeover", False),
                )
                store.change(
                    "repo.prepared",
                    {"repo_ref": repo_id, "repo": prepared},
                    lambda s, i=repo_id, r=prepared: s["repos"].update({i: r}),
                )
            state = store.read()
            engine = self.engine(store)
            engine.request(
                "goal.enrolled",
                key(goal_id, "enrolled"),
                {"goal": state["goal"], "work_folder": request["work_folder"]},
                {"kind": "mcp", "id": request["request_id"]},
            )
            store.change("goal.enrolled", {"goal_id": goal_id}, lambda s: s.update(status="active"))
            await self._spawn(store, goal_id)
            return self.status(goal_id)

    async def _spawn(self, store, goal_id):
        write_json(store.root / "launch.json", {"state": "launching"})
        try:
            process = await asyncio.to_thread(self.spawner, goal_id)
        except Exception as exc:
            write_json(store.root / "launch.json", {"state": "failed", "error": str(exc)})
            store.change(
                "engine.spawn_failed",
                {"error": str(exc)},
                lambda s: s.update(status="stopped", stop="spawn_failed"),
            )
            raise
        write_json(store.root / "launcher.json", process)
        write_json(store.root / "launch.json", {"state": "spawned", **process})
        store.change("engine.spawned", process)

    def list(self):
        result = []
        for folder in sorted(self.root.iterdir()):
            if folder.is_dir() and len(folder.name) == 24 and (folder / "events.sqlite3").exists():
                state = Store(folder).read()
                if state["goal"]:
                    result.append(
                        {
                            "goal_id": folder.name,
                            "title": state["goal"]["title"],
                            "status": state["status"],
                        }
                    )
        return result

    def status(self, goal_id):
        state = self.store(goal_id).read()
        alive = False
        for name in ("process.json", "launcher.json"):
            process_path = self.store(goal_id).root / name
            if process_path.exists():
                process = json.loads(process_path.read_text())
                alive = alive or bool(
                    process.get("identity") and identity(process["pid"]) == process["identity"]
                )
        return {
            "goal_id": goal_id,
            "status": state["status"],
            "engine_alive": alive,
            "goal_version": state["goal"]["version"],
            "work_folder": state["goal"]["work_folder"],
            "intent": state.get("intent"),
            "repos": state["repos"],
            "dds": state["dds"],
            "runs": {
                i: {k: v for k, v in r.items() if k not in {"prompt", "schema"}}
                for i, r in state["runs"].items()
            },
            "pending_requests": [
                i for i, r in state["requests"].items() if r["status"] != "delivered"
            ],
            "finalized": state["finalized"],
        }

    def message(self, goal_id, request_id, text, caller, reply_to):
        if (
            not text
            or not request_id
            or not isinstance(caller, dict)
            or caller.get("kind") not in {"human", "agent"}
            or not caller.get("id")
            or reply_to is None
        ):
            raise ValueError("消息必须有正文、request_id、外部 caller 与 reply_to")
        store = self.store(goal_id)
        with store.atomic():
            state = store.read()
            old = state["requests"].get(request_id)
            if old:
                envelope = old["envelope"]
                if (
                    envelope["type"],
                    envelope["input"],
                    envelope["caller"],
                    envelope["reply_to"],
                ) != ("line.message", {"text": text}, caller, reply_to):
                    raise ValueError("request_id 已用于不同消息")
            else:
                if state["status"] == "done":
                    raise ValueError("已完成 Goal 不再接收执行请求")
                self.engine(store).request(
                    "line.message", request_id, {"text": text}, caller, reply_to
                )
        return {
            "request_id": request_id,
            "queued": True,
            "requires_resume": bool(store.read()["stop"])
            or not self.status(goal_id)["engine_alive"],
        }

    def steer(self, goal_id, request_id, body, expected_version):
        if not body or not request_id:
            raise ValueError("目标正文与请求标识不能为空")
        store = self.store(goal_id)

        def apply(state):
            if request_id in state["requests"]:
                old = state["requests"][request_id]["envelope"]
                if old["type"] != "goal.steered" or old["input"]["body"] != body:
                    raise ValueError("重复 steer 内容不一致")
                return
            if state["status"] == "done" or state["goal"]["version"] != expected_version:
                raise ValueError("目标已终结或版本冲突")
            previous = state["goal"]["body"]
            state["goal"].update(body=body, version=expected_version + 1)
            envelope = {
                "request_id": request_id,
                "type": "goal.steered",
                "goal_id": goal_id,
                "goal_version": expected_version + 1,
                "caller": {"kind": "mcp", "id": request_id},
                "input": {
                    "body": body,
                    "previous_body": previous,
                    "goal_version": expected_version + 1,
                },
                "reply_to": None,
            }
            state["requests"][request_id] = {"envelope": envelope, "status": "pending"}
            if not state["stop"] and state["status"] in {"waiting", "blocked"}:
                state["status"] = "active"

        store.change(
            "goal.steered",
            {"request_id": request_id, "body": body, "expected_version": expected_version},
            apply,
        )
        return self.status(goal_id)

    async def stop(self, goal_id, immediate=False):
        store = self.store(goal_id)
        async with async_locked(store.root / "control.lock"):
            if store.read()["status"] == "done":
                return self.status(goal_id)

            def apply(state):
                if state["stop"] != "immediate":
                    state.update(stop="immediate" if immediate else "graceful", status="stopping")

            store.change("goal.stop_requested", {"immediate": immediate}, apply)
            engine = self.engine(store)
            if immediate:
                for run_id, run in store.read()["runs"].items():
                    if run["status"] == "running":
                        try:
                            await (
                                engine.commands if run["role"] == "acceptance" else engine.runtime
                            ).stop(run["ticket"])
                            store.change("run.stop_requested", {"run_id": run_id})
                        except Exception as exc:
                            store.change("run.stop_failed", {"run_id": run_id, "error": str(exc)})
            # launcher 尚未拿到 engine.lock 时也由其自行观察 stop，避免抢启动锁。
            if self.status(goal_id)["engine_alive"]:
                return self.status(goal_id)
            # 只有引擎锁的持有者可以折叠结果；活跃引擎自行观察 stop。
            try:
                with locked(store.root / "engine.lock", nonblocking=True):
                    await engine.collect()
            except BlockingIOError:
                pass
            return self.status(goal_id)

    async def resume(
        self, goal_id, confirm_launch_absent=False, confirmed_absent_runs=None, absence_evidence=""
    ):
        confirmed_absent_runs = confirmed_absent_runs or []
        if confirmed_absent_runs and not absence_evidence.strip():
            raise ValueError("确认 run 缺席必须提供现场核对证据 absence_evidence")
        store = self.store(goal_id)
        async with async_locked(store.root / "control.lock"):
            if self.status(goal_id)["engine_alive"]:
                raise ValueError("引擎尚未退出；等待停止完成再 resume")
            launch = store.root / "launch.json"
            uncertain = (
                launch.exists() and json.loads(launch.read_text()).get("state") == "launching"
            )
            if uncertain and not confirm_launch_absent:
                raise ValueError("上次启动状态不明；须先核对进程，禁止盲目重派")
            with locked(store.root / "engine.lock", nonblocking=True):
                if uncertain:
                    store.change("engine.launch_absence_confirmed", {"confirmed": True})
                if store.read()["status"] == "done":
                    return self.status(goal_id)
                engine = self.engine(store)
                confirmations = []
                for run_id in dict.fromkeys(confirmed_absent_runs):
                    run = store.read()["runs"].get(run_id)
                    if not run or run["status"] != "uncertain":
                        raise ValueError("只能确认仍处于 uncertain 的 run 缺席")
                    port = engine.commands if run["role"] == "acceptance" else engine.runtime
                    observed = await port.inspect(run["ticket"])
                    if observed.get("status") == "running":
                        raise ValueError(f"run {run_id} 仍在运行，不能确认缺席")
                    if observed.get("status") not in {"lost", "succeeded", "failed"}:
                        raise ValueError("run 查询未返回可核对状态")
                    # 迟到的真实结果优先，不能用人工缺席声明覆盖已完成输出。
                    result = (
                        observed
                        if observed["status"] != "lost"
                        else {
                            "status": "failed",
                            "output": None,
                            "error": "externally_confirmed_absent",
                            "absence_evidence": absence_evidence,
                            "observed": observed,
                        }
                    )
                    confirmations.append((run_id, result, observed))
                with store.atomic():
                    for run_id, result, observed in confirmations:
                        store.change(
                            "run.absence_confirmed",
                            {"run_id": run_id, "evidence": absence_evidence, "observed": observed},
                            lambda s, i=run_id, r=result: s["runs"][i].update(
                                status="collected", result=r
                            ),
                        )
                await engine.collect()
                store.change("goal.resumed", {}, lambda s: s.update(stop=None, status="active"))
                engine.request(
                    "goal.resumed",
                    key(
                        goal_id, "resume", store.events(0, 1)["next"], len(store.read()["requests"])
                    ),
                    {
                        "interrupted_requests": [
                            i
                            for i, r in store.read()["requests"].items()
                            if r["status"] == "interrupted"
                        ],
                        "dds": [
                            {"dd_id": i, "step": d["step"]} for i, d in store.read()["dds"].items()
                        ],
                    },
                )
            # control.lock 跨越 spawn 与 launcher 落盘，后续 resume 能看到真实身份。
            await self._spawn(store, goal_id)
            return self.status(goal_id)

    def replies(self, goal_id, after=0, limit=100):
        # query 游标跨所有事件推进；空页也应按 next 继续直到日志末尾。
        page = self.store(goal_id).events(after, limit)
        return {
            "replies": [e for e in page["events"] if e["kind"] == "reply.delivered"],
            "next": page["next"],
        }

    async def session(self, goal_id, run_id, offset=0, limit=100):
        store = self.store(goal_id)
        run = store.read()["runs"][run_id]
        if run["role"] == "acceptance":
            raise ValueError("程序验收记录通过 goal_artifact 查询")
        return await self.engine(store).runtime.session(run["ticket"], offset, limit)

    def artifact(self, goal_id, filename, offset=0, limit=65536):
        root = self.store(goal_id).root.resolve()
        path = (root / filename).resolve()
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or offset < 0
            or not 1 <= limit <= 1048576
        ):
            raise ValueError("工件路径或分页参数无效")
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(limit)
        return {
            "filename": filename,
            "offset": offset,
            "next": offset + len(data),
            "size": path.stat().st_size,
            "encoding": "base64",
            "data": base64.b64encode(data).decode(),
        }


def build_mcp(control):
    from fastmcp import FastMCP

    mcp = FastMCP("fleet-graph")

    @mcp.tool
    async def goal_enroll(request: dict) -> dict:
        """登记目标、准备 release 并启动独立引擎。"""
        return await control.enroll(request)

    @mcp.tool
    def goal_list() -> list:
        """列举本状态根的 Goal。"""
        return control.list()

    @mcp.tool
    def goal_status(goal_id: str) -> dict:
        """查询状态、版本、DD 与运行句柄。"""
        return control.status(goal_id)

    @mcp.tool
    def goal_events(goal_id: str, after: int = 0, limit: int = 100) -> dict:
        """顺序分页读取完整引擎 L0。"""
        return control.store(goal_id).events(after, limit)

    @mcp.tool
    def goal_message(
        goal_id: str, request_id: str, text: str, caller: dict, reply_to: dict
    ) -> dict:
        """持久化一个外部请求；stopped 状态须 resume。"""
        return control.message(goal_id, request_id, text, caller, reply_to)

    @mcp.tool
    def goal_steer(goal_id: str, request_id: str, body: str, expected_version: int) -> dict:
        """CAS 更新目标正文并排入独立请求。"""
        return control.steer(goal_id, request_id, body, expected_version)

    @mcp.tool
    async def goal_stop(goal_id: str, immediate: bool = False) -> dict:
        """graceful 或立即停止本 Goal。"""
        return await control.stop(goal_id, immediate)

    @mcp.tool
    async def goal_resume(
        goal_id: str,
        confirm_launch_absent: bool = False,
        confirmed_absent_runs: list[str] | None = None,
        absence_evidence: str = "",
    ) -> dict:
        """核对现场后恢复；仅人工核实启动回执丢失时确认引擎缺席。"""
        return await control.resume(
            goal_id, confirm_launch_absent, confirmed_absent_runs, absence_evidence
        )

    @mcp.tool
    def goal_observations(goal_id: str, offset: int = 0, limit: int = 100) -> dict:
        """读取附 L0 证据的书记员 L1。"""
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("分页参数无效")
        rows = control.store(goal_id).read()["observations"][offset : offset + limit]
        return {"observations": rows, "next": offset + len(rows)}

    @mcp.tool
    def goal_replies(goal_id: str, after: int = 0, limit: int = 100) -> dict:
        """接收者读取耐久 mailbox 的已投递回复。"""
        return control.replies(goal_id, after, limit)

    @mcp.tool
    async def goal_session(goal_id: str, run_id: str, offset: int = 0, limit: int = 100) -> dict:
        """通过 runtime 读取完整 Session 原始记录。"""
        return await control.session(goal_id, run_id, offset, limit)

    @mcp.tool
    def goal_artifact(goal_id: str, filename: str, offset: int = 0, limit: int = 65536) -> dict:
        """分页读取本 Goal 的完整原始工件字节。"""
        return control.artifact(goal_id, filename, offset, limit)

    return mcp
