"""每 Goal 一张 LangGraph；事件驱动、单 Goal call、并行线性 DD。"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from fleet_graph.protocol import (
    ACTION_SCHEMA,
    IMPL_SCHEMA,
    REVIEW_SCHEMA,
    SCRIBE_SCHEMA,
    validate,
    validate_actions,
)


def key(*parts):
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:24]


class Tick(TypedDict):
    tick: int


class Engine:
    def __init__(
        self,
        store,
        runtime,
        git,
        commands,
        reply=None,
        *,
        capacity=8,
        scribe_interval=60,
        warning_turns=20,
    ):
        self.store, self.runtime, self.git, self.commands, self.reply = (
            store,
            runtime,
            git,
            commands,
            reply,
        )
        self.capacity, self.scribe_interval, self.warning_turns = (
            capacity,
            scribe_interval,
            warning_turns,
        )
        graph = StateGraph(Tick)
        for name, method in (
            ("collect", self.collect),
            ("actions", self.actions),
            ("schedule", self.schedule),
            ("observe", self.observe),
        ):

            async def node(state, method=method):
                await method()
                return {"tick": state["tick"] + 1}

            graph.add_node(name, node)
        graph.add_edge(START, "collect")
        graph.add_edge("collect", "actions")
        graph.add_edge("actions", "schedule")
        graph.add_edge("schedule", "observe")
        graph.add_edge("observe", END)
        self.graph = graph.compile()

    async def tick(self):
        return await self.graph.ainvoke({"tick": 0})

    def request(self, kind, request_id, current, caller=None, reply_to=None):
        goal = self.store.read()["goal"]
        self.store.enqueue(
            {
                "request_id": request_id,
                "type": kind,
                "goal_id": goal["goal_id"],
                "goal_version": goal["version"],
                "caller": caller or {"kind": "engine", "id": goal["goal_id"]},
                "input": current,
                "reply_to": reply_to,
            }
        )

    def dd_update(self, dd_id, kind, updates):
        self.store.change(
            kind, {"dd_id": dd_id, **updates}, lambda s: s["dds"][dd_id].update(updates)
        )

    def rework(self, dd_id, current):
        dd = self.store.read()["dds"][dd_id]
        self.dd_update(
            dd_id,
            "dd.rework",
            {
                "step": "impl",
                "current": current,
                "head": None,
                "cleanup_head": dd.get("head") or dd.get("cleanup_head"),
                "acceptance": None,
                "cr": None,
                "fr": None,
                "review_ref": None,
                "approved": None,
            },
        )

    async def collect(self):
        state = self.store.read()
        if state["stop"]:
            for run_id, item in list(state["runs"].items()):
                if item["status"] != "launching":
                    continue
                port = self.commands if item["role"] == "acceptance" else self.runtime
                try:
                    ticket = await port.recover(run_id)
                except Exception as exc:
                    self.store.change(
                        "run.recovery_uncertain", {"run_id": run_id, "error": str(exc)}
                    )
                    continue
                if ticket is None:
                    self.store.change(
                        "run.paused_before_launch",
                        {"run_id": run_id},
                        lambda s, i=run_id: s["runs"][i].update(status="paused"),
                    )
                else:
                    self._started(run_id, item, ticket)
        state = self.store.read()
        running = [
            (i, r) for i, r in state["runs"].items() if r["status"] in {"running", "uncertain"}
        ]

        async def inspect(item):
            run_id, run = item
            port = self.commands if run["role"] == "acceptance" else self.runtime
            try:
                if self.store.read()["stop"] == "immediate":
                    await port.stop(run["ticket"])
                return run_id, run, await port.inspect(run["ticket"])
            except Exception as exc:
                self.store.change("runtime.query_error", {"run_id": run_id, "error": str(exc)})
                return run_id, run, {"status": "running"}

        for run_id, _run, result in await asyncio.gather(*(inspect(item) for item in running)):
            if result["status"] == "running":
                continue
            if result["status"] in {"unknown", "uncertain"} or (
                result["status"] == "lost" and "launch_uncertain" in str(result.get("error", ""))
            ):
                self.store.change(
                    "run.uncertain",
                    {"run_id": run_id, "result": result},
                    lambda s, i=run_id, r=result: s["runs"][i].update(status="uncertain", result=r),
                )
                continue
            # 先保存结果，处理中的崩溃通过 collected 状态重放业务折叠。
            self.store.change(
                "run.collected",
                {"run_id": run_id, "result": result},
                lambda s, i=run_id, r=result: s["runs"][i].update(status="collected", result=r),
            )
        for run_id, run in list(self.store.read()["runs"].items()):
            if run["status"] == "collected":
                await self.finish_run(run_id, run)
        state = self.store.read()
        if (
            state["stop"]
            and state["status"] != "stopped"
            and not any(
                r["status"] in {"running", "launching", "collected", "uncertain"}
                for r in state["runs"].values()
            )
        ):
            self.store.change("goal.stopped", {}, lambda s: s.update(status="stopped"))

    async def finish_run(self, run_id, run):
        role, owner = run["role"], run["owner"]
        handoff = None
        state = self.store.read()
        dd = state["dds"].get(owner)
        if dd and (dd.get("obsolete_run") == run_id or dd["step"] == "cancelled"):
            self.store.change(
                "run.obsolete",
                {"run_id": run_id},
                lambda s: s["runs"][run_id].update(status="finished"),
            )
            return
        output = run["result"].get("output")
        needs_goal = (
            role == "impl" and isinstance(output, dict) and output.get("type") == "needs_goal"
        )
        if (
            dd
            and not needs_goal
            and (role == "acceptance" or run["result"]["status"] == "succeeded")
        ):
            try:
                handoff = await asyncio.to_thread(
                    self.git.verify_handoff, state["repos"][dd["repo_ref"]], dd
                )
            except Exception as exc:
                handoff = exc
        # 此后仅同步折叠；run.finished 与状态变化/事件请求一起提交。
        with self.store.atomic():
            await self._finish_run(run_id, run, handoff)

    async def _finish_run(self, run_id, run, handoff):
        result, role, owner = run["result"], run["role"], run["owner"]
        successful = result["status"] == "succeeded"
        output = result.get("output")
        if successful and role != "acceptance":
            try:
                validate(
                    output,
                    ACTION_SCHEMA
                    if role == "goal"
                    else SCRIBE_SCHEMA
                    if role == "scribe"
                    else IMPL_SCHEMA
                    if role == "impl"
                    else REVIEW_SCHEMA,
                )
                if role == "goal":
                    validate_actions(output)
            except Exception as exc:
                successful = False
                result = {**result, "status": "failed", "error": f"输出协议错误: {exc}"}

        def finish(s):
            s["runs"][run_id]["status"] = "finished"

        if role == "scribe":
            if successful:

                def observations(s):
                    for obs in output["observations"]:
                        s["observations"].append(
                            {**obs, "run_id": run_id, "event_range": run["event_range"]}
                        )
                    s["scribe_cursor"] = run["event_range"][1]
                    finish(s)

                self.store.change(
                    "scribe.observed", {"run_id": run_id, "output": output}, observations
                )
            else:
                self.store.change("scribe.failed", {"run_id": run_id, "result": result}, finish)
            return
        if not successful and role != "acceptance":

            def fault(s):
                finish(s)
                if role == "goal":
                    s["requests"][owner]["status"] = "interrupted"
                    s["status"] = "blocked"
                else:
                    s["dds"][owner].update(
                        step="interrupted",
                        current={
                            "from": "runtime",
                            "role": role,
                            "run_id": run_id,
                            "result": result,
                        },
                    )

            self.store.change("runtime.failed", {"run_id": run_id, "result": result}, fault)
            if role != "goal":
                self.request(
                    "dd.needs_goal", key(run_id, "error"), {"dd_id": owner, "runtime_error": result}
                )
            return
        if role == "goal":
            if (
                run.get("goal_version", self.store.read()["goal"]["version"])
                != self.store.read()["goal"]["version"]
            ):

                def stale(s):
                    finish(s)
                    s["requests"][owner]["status"] = "pending"

                self.store.change("goal.stale_output", {"run_id": run_id}, stale)
                return

            def accept(s):
                finish(s)
                s["requests"][owner]["status"] = "actions"
                for index, action in enumerate(output):
                    action_id = key(run_id, index)
                    s["actions"].setdefault(
                        action_id,
                        {
                            "request_id": owner,
                            "run_id": run_id,
                            "index": index,
                            "action": action,
                            "status": "pending",
                        },
                    )

            self.store.change(
                "goal.actions_received", {"run_id": run_id, "actions": output}, accept
            )
            return
        dd = self.store.read()["dds"][owner]
        if dd.get("obsolete_run") == run_id or dd["step"] == "cancelled":
            self.store.change("run.obsolete", {"run_id": run_id}, finish)
            return
        if role == "acceptance" and result["status"] == "lost":
            self.dd_update(
                owner,
                "dd.interrupted",
                {"step": "interrupted", "current": {"role": role, "result": result}},
            )
            self.request(
                "dd.needs_goal", key(run_id, "lost"), {"dd_id": owner, "runtime_error": result}
            )
            self.store.change("run.finished", {"run_id": run_id}, finish)
            return
        if role == "impl" and output["type"] == "needs_goal":
            self.dd_update(owner, "dd.needs_goal", {"step": "needs_goal", "current": output})
            self.request("dd.needs_goal", key(run_id, "help"), {"dd_id": owner, **output})
            self.store.change("run.finished", {"run_id": run_id}, finish)
            return
        if isinstance(handoff, Exception):
            self.rework(owner, {"from": "handoff", "message": str(handoff)})
            self.store.change("run.finished", {"run_id": run_id}, finish)
            return
        head = handoff["head"]
        current_version = self.store.read()["goal"]["version"]
        if (
            run.get("goal_version", dd["goal_version"]) != current_version
            or run.get("input_version", dd["input_version"]) != dd["input_version"]
        ):
            self.rework(
                owner, {"from": "version", "message": "目标或输入版本变化，必须重新落实与审查"}
            )
            self.dd_update(owner, "dd.goal_version_changed", {"goal_version": current_version})
        elif role != "impl" and dd.get("head") != head:
            self.rework(
                owner, {"from": "version", "message": "审查中代码版本变化，必须重新验收与审查"}
            )
        elif role == "impl":
            if output["type"] == "needs_goal":
                self.dd_update(owner, "dd.needs_goal", {"step": "needs_goal", "current": output})
                self.request("dd.needs_goal", key(run_id, "help"), {"dd_id": owner, **output})
            else:
                self.dd_update(
                    owner,
                    "dd.implemented",
                    {"step": "acceptance", "head": head, "current": output, "approved": None},
                )
        elif role == "acceptance":
            if result["status"] == "lost":
                self.dd_update(
                    owner,
                    "dd.interrupted",
                    {"step": "interrupted", "current": {"role": role, "result": result}},
                )
                self.request(
                    "dd.needs_goal", key(run_id, "lost"), {"dd_id": owner, "runtime_error": result}
                )
            elif dd["step"] == "baseline_running":
                self.dd_update(
                    owner,
                    "dd.baseline",
                    {
                        "step": "impl",
                        "baseline": result,
                        "current": {"from": "baseline", "result": result},
                        "head": None,
                    },
                )
            elif successful:
                self.dd_update(
                    owner,
                    "dd.accepted",
                    {
                        "step": "cr",
                        "acceptance": result,
                        "current": {"from": "acceptance", "result": result},
                    },
                )
            else:
                self.rework(owner, {"from": "acceptance", "result": result})
        elif output["type"] == "fail":
            self.rework(owner, {"from": role, **output})
        elif role == "cr":
            self.dd_update(
                owner,
                "dd.cr_passed",
                {
                    "step": "fr",
                    "cr": {"head": head, "run_id": run_id, "output": output},
                    "current": {"from": "cr", **output},
                },
            )
        elif role == "fr":
            review_ref = key(owner, head, run_id)
            self.dd_update(
                owner,
                "dd.review_requested",
                {
                    "step": "review",
                    "fr": {"head": head, "run_id": run_id, "output": output},
                    "review_ref": review_ref,
                    "current": output,
                },
            )
            self.request(
                "dd.review_requested",
                review_ref,
                {
                    "dd_id": owner,
                    "review_ref": review_ref,
                    "head": head,
                    "fr": output,
                    "pr": dd["pr"],
                },
                {"kind": "dd", "id": owner},
                {"kind": "dd", "id": owner},
            )
        self.store.change("run.finished", {"run_id": run_id}, finish)

    async def actions(self):
        if self.store.read()["stop"]:
            return
        for action_id in list(self.store.read()["actions"]):
            state = self.store.read()
            if state["stop"]:
                break
            record = state["actions"][action_id]
            origin = state["runs"].get(record.get("run_id"), {})
            if (
                record["status"] == "pending"
                and origin.get("goal_version", state["goal"]["version"]) != state["goal"]["version"]
            ):
                stale_run_id = record.get("run_id")

                def invalidate(s, request_id=record["request_id"], run_id=stale_run_id):
                    for item in s["actions"].values():
                        if item.get("run_id") == run_id and item["status"] == "pending":
                            item["status"] = "obsolete"
                    s["requests"][request_id]["status"] = "pending"

                self.store.change("goal.stale_actions", {"action_id": action_id}, invalidate)
                continue
            if record["status"] not in {"pending", "executing"}:
                continue
            self.store.change(
                "action.intent",
                {"action_id": action_id, **record},
                lambda s, i=action_id: s["actions"][i].update(status="executing"),
            )
            try:
                result = await self.execute_action(action_id, record)
                self.store.change(
                    "action.succeeded",
                    {"action_id": action_id, "result": result},
                    lambda s, i=action_id, r=result: s["actions"][i].update(
                        status="succeeded", result=r
                    ),
                )
            except Exception as exc:
                with self.store.atomic():
                    self.store.change(
                        "action.failed",
                        {"action_id": action_id, "error": str(exc)},
                        lambda s, i=action_id, error=str(exc): s["actions"][i].update(
                            status="failed", error=error
                        ),
                    )
                    self.request(
                        "action.failed",
                        key(action_id, "failed"),
                        {"action_id": action_id, "action": record["action"], "error": str(exc)},
                    )
        for request_id, request in self.store.read()["requests"].items():
            if request["status"] == "actions" and all(
                a["status"] in {"succeeded", "failed", "obsolete"}
                for a in self.store.read()["actions"].values()
                if a["request_id"] == request_id
            ):
                self.store.change(
                    "request.delivered",
                    {"request_id": request_id},
                    lambda s, i=request_id: s["requests"][i].update(status="delivered"),
                )

    async def execute_action(self, action_id, record):
        state, a = self.store.read(), record["action"]
        kind, goal = a["type"], state["goal"]
        if kind == "dispatch":
            dd_id = key(goal["goal_id"], action_id)
            if dd_id in state["dds"]:
                return {"dd_id": dd_id}
            repo = state["repos"][a["repo_ref"]]
            if (
                a["target_branch"] != goal["source_branch"]
                or a["source_branch"] == a["target_branch"]
            ):
                raise ValueError("DD 必须指向本 Goal release 且 source 不同")
            if any(
                d["worktree"] == a["worktree"]
                or (d["repo_ref"] == a["repo_ref"] and d["source_branch"] == a["source_branch"])
                for d in state["dds"].values()
                if d["step"] not in {"done", "cancelled"}
            ):
                raise ValueError("工作目录或分支已被在途 DD 占用")
            handoff = await asyncio.to_thread(self.git.verify_handoff, repo, a)
            pr = await asyncio.to_thread(self.git.ensure_pr, repo, a, action_id)
            dd = {
                **a,
                "dd_id": dd_id,
                "pr": pr,
                "step": "baseline",
                "head": handoff["head"],
                "goal_version": goal["version"],
                "input_version": 1,
                "current": {"from": "dispatch", "summary": a["summary"]},
                "turn": 0,
            }
            self.store.change("dd.dispatched", dd, lambda s: s["dds"].update({dd_id: dd}))
            return {"dd_id": dd_id, "pr": pr}
        if kind == "add_repo":
            repo_id = key(a["repo"]["path"], a["repo"]["remote"])
            if repo_id in state["repos"]:
                return {"repo_ref": repo_id}
            repo = await asyncio.to_thread(
                self.git.prepare_repo,
                {**a["repo"], "operation_id": action_id},
                goal["source_branch"],
                a.get("takeover", False),
            )
            self.store.change(
                "repo.added",
                {"repo_ref": repo_id, "repo": repo},
                lambda s: s["repos"].update({repo_id: repo}),
            )
            return {"repo_ref": repo_id}
        if kind in {"reject", "revise", "cancel"}:
            dd = state["dds"][a["dd_id"]]
            if action_id in dd.get("applied_actions", []):
                return {"status": dd["step"]}
            if dd["step"] in {"done", "cancelled"}:
                if kind == "cancel" and dd["step"] == "cancelled":
                    return await asyncio.to_thread(
                        self.git.cleanup,
                        state["repos"][dd["repo_ref"]],
                        {**dd, "status": "cancelled"},
                    )
                raise ValueError("终结 DD 不可返工")
            live = [
                (i, r)
                for i, r in state["runs"].items()
                if r["owner"] == a["dd_id"]
                and r["status"] in {"running", "launching", "collected", "uncertain"}
            ]
            if live:
                raise ValueError("DD 在途；先 stop/resume 或待当前交接后修改输入")
            if kind == "cancel":
                try:
                    verified = await asyncio.to_thread(
                        self.git.verify_handoff, state["repos"][dd["repo_ref"]], dd
                    )
                    dd = {**dd, "head": verified["head"], "cleanup_head": verified["head"]}
                except Exception:
                    pass
                self.dd_update(
                    a["dd_id"],
                    "dd.cancelled",
                    {
                        "step": "cancelled",
                        "current": a,
                        "head": dd.get("head"),
                        "cleanup_head": dd.get("cleanup_head"),
                    },
                )
                result = await asyncio.to_thread(
                    self.git.cleanup,
                    state["repos"][dd["repo_ref"]],
                    {**dd, "step": "cancelled", "status": "cancelled"},
                )
                return result
            with self.store.atomic():
                self.rework(
                    a["dd_id"],
                    {"from": "goal", "message": a["message"], "goal_version": goal["version"]},
                )
                self.dd_update(
                    a["dd_id"],
                    "dd.revised",
                    {
                        "input_version": dd["input_version"] + 1,
                        "goal_version": goal["version"],
                        "applied_actions": [*dd.get("applied_actions", []), action_id],
                    },
                )
            return {"status": "impl"}
        if kind == "approve":
            dd = state["dds"][a["dd_id"]]
            if dd["step"] == "done" and dd.get("approved") == a["review_ref"]:
                if not dd.get("cleanup"):
                    cleanup = await asyncio.to_thread(
                        self.git.cleanup, state["repos"][dd["repo_ref"]], {**dd, "status": "done"}
                    )
                    self.dd_update(a["dd_id"], "dd.cleaned", {"cleanup": cleanup})
                return dd["merge"]
            if dd["step"] not in {"review", "merging"} or dd.get("review_ref") != a["review_ref"]:
                raise ValueError("批准必须匹配当前待审记录")
            recovered = None
            if dd["goal_version"] != goal["version"]:
                if dd["step"] == "merging":
                    recovered = await asyncio.to_thread(
                        self.git.recover_merge,
                        state["repos"][dd["repo_ref"]],
                        dd["source_branch"],
                        dd["target_branch"],
                        dd["head"],
                        dd["pr"],
                    )
                if recovered is None or recovered["status"] != "merged":
                    self.rework(
                        a["dd_id"], {"from": "version", "message": "目标版本已变化，批准失效"}
                    )
                    self.dd_update(
                        a["dd_id"], "dd.goal_version_changed", {"goal_version": goal["version"]}
                    )
                    raise ValueError("目标版本已变化，需重新验收与审查")
            if (
                not dd.get("acceptance")
                or not dd.get("cr")
                or not dd.get("fr")
                or dd["cr"]["head"] != dd["head"]
                or dd["fr"]["head"] != dd["head"]
            ):
                raise ValueError("同一版本验收与 CR/FR 证据不完整")
            self.dd_update(
                a["dd_id"], "dd.merge_intent", {"step": "merging", "approved": a["review_ref"]}
            )
            result = recovered or await asyncio.to_thread(
                self.git.merge,
                state["repos"][dd["repo_ref"]],
                dd["source_branch"],
                dd["target_branch"],
                dd["head"],
                dd["worktree"],
                dd["pr"],
            )
            if result["status"] == "merged":
                with self.store.atomic():
                    self.dd_update(a["dd_id"], "dd.merged", {"step": "done", "merge": result})
                    self.request(
                        "dd.finished",
                        key(a["dd_id"], "merged"),
                        {"dd_id": a["dd_id"], "result": result},
                    )
                try:
                    cleanup = await asyncio.to_thread(
                        self.git.cleanup,
                        state["repos"][dd["repo_ref"]],
                        {**dd, "step": "done", "status": "done"},
                    )
                    self.dd_update(a["dd_id"], "dd.cleaned", {"cleanup": cleanup})
                except Exception as exc:
                    self.dd_update(a["dd_id"], "dd.cleanup_failed", {"cleanup_error": str(exc)})
            else:
                self.rework(a["dd_id"], {"from": "merge", "result": result})
            return result
        if kind == "reply":
            request = state["requests"][a["in_reply_to"]]["envelope"]
            if (
                request["caller"]["kind"] not in {"human", "agent"}
                or request.get("reply_to") is None
            ):
                raise ValueError("回复需要外部调用方与 reply_to 关联")
            if self.reply:
                return await self.reply.send(request, a["text"], action_id)
            # 本 MCP 的耐久 mailbox 是默认投递端；接收者用 goal_replies 全量读取。
            result = {
                "delivery": "mailbox",
                "action_id": action_id,
                "in_reply_to": a["in_reply_to"],
                "reply_to": request["reply_to"],
                "text": a["text"],
            }
            previous = state.get("deliveries", {}).get(action_id)
            if previous:
                return previous
            self.store.change(
                "reply.delivered",
                result,
                lambda s: s.setdefault("deliveries", {}).update({action_id: result}),
            )
            return result
        if kind == "blocked" and any(
            d["step"] not in {"done", "cancelled", "needs_goal", "interrupted", "review"}
            for d in state["dds"].values()
        ):
            raise ValueError("仍有可推进 DD；应使用 waiting")
        if kind in {"waiting", "blocked"}:
            self.store.change("goal.intent", a, lambda s: s.update(status=kind, intent=a))
            return {"status": kind}
        if kind == "done":
            if state["status"] == "done":
                return {"status": "done"}
            if any(d["step"] not in {"done", "cancelled"} for d in state["dds"].values()):
                raise ValueError("存在未完成 DD，不能收尾")
            if any(r["status"] == "pending" for r in state["requests"].values()):
                raise ValueError("仍有未处理请求，须先处理后收尾")
            for repo_id, repo in state["repos"].items():
                # 已成功项仍核对 source/target；不能在后来新增提交时沿用旧成功标记。
                head = await asyncio.to_thread(self.git.inspect_refs, repo, goal["source_branch"])
                source_head = head["source_head"]
                result = await asyncio.to_thread(
                    self.git.merge, repo, goal["source_branch"], repo["target_branch"], source_head
                )
                self.store.change(
                    "repo.finalized",
                    {"repo_ref": repo_id, "result": result},
                    lambda s, i=repo_id, r=result, h=source_head: (
                        s["finalized"].update({i: {**r, "source_head": h}})
                        if r["status"] == "merged"
                        else None
                    ),
                )
                if result["status"] != "merged":
                    raise ValueError(f"repo {repo_id} 收尾需要 Goal 安排 DD: {result}")
            self.store.change("goal.done", a, lambda s: s.update(status="done", intent=a))
            return {"status": "done"}
        raise ValueError(f"未知动作 {kind}")

    async def launch(self, role, owner, prompt, workspace, schema=None):
        state = self.store.read()
        # 同一逻辑 step 的意图先记录，恢复时以原 run_id 重新 start，仅允许 adapter 接管。
        pending = next(
            (
                (i, r)
                for i, r in state["runs"].items()
                if r["owner"] == owner and r["status"] == "launching"
            ),
            None,
        )
        if pending:
            run_id, run = pending
        else:
            count = sum(r["owner"] == owner for r in state["runs"].values())
            run_id = key(state["goal"]["goal_id"], owner, role, count)
            run = {
                "role": role,
                "owner": owner,
                "status": "launching",
                "prompt": prompt,
                "workspace": workspace,
                "schema": schema,
                "scope": key(state["goal"]["goal_id"], owner if role != "goal" else "goal", role),
                "goal_version": state["goal"]["version"],
                "input_version": state["dds"].get(owner, {}).get("input_version"),
            }
            if role == "scribe":
                run["event_range"] = prompt["event_range"]
            self.store.change(
                "run.intent", {"run_id": run_id, **run}, lambda s: s["runs"].update({run_id: run})
            )
        if role == "acceptance":
            ticket = await self.commands.start(run_id, workspace, prompt["commands"])
        else:
            ticket = await self.runtime.start(run_id, role, workspace, prompt, schema, run["scope"])

        self._started(run_id, run, ticket)
        if (
            role not in {"goal", "scribe"}
            and self.store.read()["dds"][owner]["turn"] % self.warning_turns == 0
        ):
            self.store.change(
                "dd.turn_warning", {"dd_id": owner, "turn": self.store.read()["dds"][owner]["turn"]}
            )

    def _started(self, run_id, run, ticket):
        role, owner = run["role"], run["owner"]

        def launched(s):
            s["runs"][run_id].update(status="running", ticket=ticket)
            if role == "goal":
                s["requests"][owner]["status"] = "running"
            elif role != "scribe":
                dd = s["dds"][owner]
                dd["step"] = "baseline_running" if dd["step"] == "baseline" else f"{role}_running"
                dd["turn"] += 1

        self.store.change("run.started", {"run_id": run_id, "ticket": ticket}, launched)

    async def schedule(self):
        state = self.store.read()
        if state["stop"] or state["status"] in {"done", "stopped", "preparing"}:
            return
        for _run_id, run in list(state["runs"].items()):
            if run["status"] in {"launching", "paused"}:
                if self.store.read()["stop"]:
                    return
                if run["status"] == "paused":
                    self.store.change(
                        "run.resumed",
                        {"run_id": _run_id},
                        lambda s, i=_run_id: s["runs"][i].update(status="launching"),
                    )
                await self.launch(
                    run["role"], run["owner"], run["prompt"], run["workspace"], run["schema"]
                )
        state = self.store.read()
        if not any(
            r["role"] == "goal"
            and r["status"] in {"running", "launching", "collected", "uncertain"}
            for r in state["runs"].values()
        ) and not any(r["status"] == "actions" for r in state["requests"].values()):
            request = next(
                (r["envelope"] for r in state["requests"].values() if r["status"] == "pending"),
                None,
            )
            if request:
                prompt = {
                    "schema": "goal.input/3",
                    "request_id": request["request_id"],
                    "request_type": request["type"],
                    "caller": request["caller"],
                    "goal_id": state["goal"]["goal_id"],
                    "goal_version": state["goal"]["version"],
                    "requested_goal_version": request["goal_version"],
                    "current": request["input"],
                    "active_dds": [
                        {"dd_id": i, "step": d["step"]}
                        for i, d in state["dds"].items()
                        if d["step"] not in {"done", "cancelled"}
                    ],
                    "repos": state["repos"],
                    "history": {
                        "work_folder": state["goal"]["work_folder"],
                        "engine": str(self.store.root),
                        "query": "goal_events / goal_session / goal_artifact",
                    },
                }
                await self.launch(
                    "goal",
                    request["request_id"],
                    prompt,
                    next(iter(state["repos"].values()))["path"],
                    ACTION_SCHEMA,
                )
        state = self.store.read()
        available = self.capacity - sum(
            r["status"] == "running" and r["role"] not in {"goal", "scribe"}
            for r in state["runs"].values()
        )
        for dd_id, dd in list(state["dds"].items()):
            if self.store.read()["stop"] or available <= 0:
                break
            if dd["step"] not in {"baseline", "impl", "acceptance", "cr", "fr"}:
                continue
            if any(
                r["owner"] == dd_id
                and r["status"] in {"running", "launching", "collected", "uncertain"}
                for r in self.store.read()["runs"].values()
            ):
                continue
            role = "acceptance" if dd["step"] in {"baseline", "acceptance"} else dd["step"]
            repo = state["repos"][dd["repo_ref"]]
            prompt = {
                "schema": "dd.turn.input/2",
                "dd_id": dd_id,
                "role": role,
                "workspace": dd["worktree"],
                "branch": dd["source_branch"],
                "pr_ref": dd["pr"],
                "spec_ref": dd["spec_path"],
                "current": dd["current"],
                "head": dd.get("head"),
                "acceptance": dd.get("acceptance"),
                "goal_version": dd["goal_version"],
                "input_version": dd["input_version"],
                "history_ref": {
                    "goal_id": state["goal"]["goal_id"],
                    "dd_id": dd_id,
                    "engine": str(self.store.root),
                },
            }
            if role == "acceptance":
                prompt["commands"] = repo["acceptance"]
            await self.launch(
                role,
                dd_id,
                prompt,
                dd["worktree"],
                IMPL_SCHEMA if role == "impl" else REVIEW_SCHEMA if role != "acceptance" else None,
            )
            available -= 1

    async def observe(self):
        import time

        state = self.store.read()
        final = state["status"] == "done"
        if state["stop"] or state["status"] == "preparing":
            return
        if final and state.get("final_scribe_attempted"):
            return
        if not final and self.scribe_interval <= 0:
            return
        if any(
            r["role"] == "scribe"
            and r["status"] in {"running", "launching", "collected", "uncertain"}
            for r in state["runs"].values()
        ):
            return
        last = state.get("scribe_attempt", 0)
        if not final and time.time() - last < self.scribe_interval:
            return
        page = self.store.events(state["scribe_cursor"], 1000)
        meaningful = [
            e
            for e in page["events"]
            if not e["kind"].startswith("scribe.")
            and not (
                e["kind"].startswith("run.")
                and e["payload"].get("run_id") in state["runs"]
                and state["runs"][e["payload"]["run_id"]]["role"] == "scribe"
            )
        ]
        if not meaningful:
            return

        def attempted(s):
            s["scribe_attempt"] = time.time()
            if final:
                s["final_scribe_attempted"] = True

        self.store.change("scribe.attempt", {"final": final}, attempted)
        try:
            await self.launch(
                "scribe",
                "scribe",
                {
                    "events": page["events"],
                    "event_range": [state["scribe_cursor"] + 1, page["next"]],
                    "final": final,
                    "history_ref": {
                        "goal_id": state["goal"]["goal_id"],
                        "query": "goal_events / goal_session",
                    },
                },
                str(self.store.root),
                SCRIBE_SCHEMA,
            )
        except Exception as exc:
            # start 异常不代表进程不存在；先只读核查，再决定 failed 或 uncertain。
            for run_id, item in self.store.read()["runs"].items():
                if item["role"] != "scribe" or item["status"] != "launching":
                    continue
                recovery_error = None
                try:
                    ticket = await self.runtime.recover(run_id)
                except Exception as query_exc:
                    ticket = item.get("ticket") or {"run_id": run_id}
                    recovery_error = str(query_exc)
                confirmed_absent = ticket is None and recovery_error is None
                result = {
                    "status": "failed" if confirmed_absent else "lost",
                    "error": "launch_failed" if confirmed_absent else "launch_uncertain",
                    "detail": str(exc),
                    "recovery_error": recovery_error,
                }
                self.store.change(
                    "scribe.launch_failed" if confirmed_absent else "scribe.launch_uncertain",
                    {"run_id": run_id, "result": result},
                    lambda s, i=run_id, r=result, t=ticket, absent=confirmed_absent: s["runs"][
                        i
                    ].update(status="finished" if absent else "uncertain", result=r, ticket=t),
                )
            self.store.change("scribe.failed", {"error": str(exc), "final": final})
