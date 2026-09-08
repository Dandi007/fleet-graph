"""有上限的公开测试驱动反馈；不是 runtime 原生重试。"""

POLICY = "e2e-format-feedback-v1"
MAX_FEEDBACK = 2
MESSAGE = (
    "测试驱动反馈（策略 {policy}，第 {attempt}/2 次）：run {run_id} 已明确因最终输出协议失败"
    "（exit 91 / contract_violation），本反馈不表示实现或审查通过。"
    "先经 goal_status、goal_events、goal_session 核对真实状态与已发生的工具副作用，"
    "保留当前目标、SPEC、分支、worktree 和 PR。对于已有 interrupted DD，必须先对同一 dd_id "
    "输出原生 revise，并在 message 中要求原角色修正最终 JSON 交接，让引擎重新经过 Impl、"
    "程序验收、CR、FR；不得新建替代 DD，不得由 Goal 自行代做 CR/FR，不得把 commit SHA "
    "当作 review_ref，缺少当轮真实 review 时不得 approve。先核对再行动，避免重复已完成的"
    "工具副作用。整个 turn 只能执行 tool calls 并在最后一次回复输出 JSON；"
    "禁止中途输出 assistant text、进展说明或思考摘要。最后只输出当轮 Stop schema 允许的 "
    "JSON 值，不含说明或 Markdown 围栏；"
    "不能调用不存在的 StructuredOutput 工具。此前 failed Session 必须保留。"
)


class FeedbackPolicy:
    def __init__(self):
        self.sent = set()
        self.attempts = 0

    def request(self, case_id, status, events):
        runs = status.get("runs")
        if (
            self.attempts >= MAX_FEEDBACK
            or status.get("status") != "blocked"
            or status.get("engine_alive") is not True
            or not isinstance(runs, dict)
            or not runs
            or any(run.get("status") != "finished" for run in runs.values())
        ):
            return None
        seqs = [event["seq"] for event in events]
        if not seqs or seqs != list(range(1, len(seqs) + 1)):
            raise ValueError("反馈判断要求从 seq 1 开始的完整公开事件")
        collected = [
            event
            for event in events
            if event["kind"] == "run.collected"
            and runs.get(event["payload"]["run_id"], {}).get("role") != "scribe"
        ]
        if not collected:
            return None
        latest = collected[-1]
        run_id = latest["payload"]["run_id"]
        run = runs.get(run_id, {})
        result = run.get("result", {})
        runtime_result = result.get("result", {})
        failures = [event for event in events if event["kind"] == "runtime.failed"]
        if (
            run_id in self.sent
            or run.get("role") != "goal"
            or result.get("status") != "failed"
            or runtime_result.get("exit_code") != 91
            or runtime_result.get("exit_reason") != "contract_violation"
            or latest["payload"].get("result") != result
            or not failures
            or failures[-1]["payload"].get("run_id") != run_id
            or failures[-1]["payload"].get("result") != result
            or failures[-1]["seq"] <= latest["seq"]
        ):
            return None
        self.attempts += 1
        self.sent.add(run_id)
        return {
            "goal_id": status["goal_id"],
            "request_id": f"{POLICY}:{case_id}:{run_id}",
            "text": MESSAGE.format(policy=POLICY, attempt=self.attempts, run_id=run_id),
            "caller": {"kind": "agent", "id": "fleet-docker-e2e-feedback-v1"},
            "reply_to": {"transport": "mailbox", "id": case_id},
        }


async def send_feedback(policy, client, call, write, bundle, case_id, status):
    """只在稳定 blocked 分支调用；保存判断依据后最多发送一次公开请求。"""
    folder = bundle / "raw/feedback" / f"{policy.attempts + 1:02d}"
    write(folder / "status.json", status)
    events, cursor, pages = [], 0, []
    while True:
        page = await call(
            client, "goal_events", {"goal_id": status["goal_id"], "after": cursor, "limit": 1000}
        )
        pages.append(page)
        write(folder / "event-pages.json", pages)
        events.extend(page["events"])
        if not page["events"] and page["next"] == cursor:
            break
        if page["next"] <= cursor:
            raise ValueError("反馈事件分页游标未推进")
        cursor = page["next"]
    current = await call(client, "goal_status", {"goal_id": status["goal_id"]})
    write(folder / "status-before-send.json", current)
    if current != status:
        write(
            folder / "decision.json",
            {"policy": POLICY, "send": False, "reason": "公开状态变化，重新观察"},
        )
        return "changed"
    request = policy.request(case_id, status, events)
    if request is None:
        write(
            folder / "decision.json", {"policy": POLICY, "send": False, "attempts": policy.attempts}
        )
        return "stop"
    write(
        folder / "intent.json",
        {
            "policy": POLICY,
            "attempt": policy.attempts,
            "limit": MAX_FEEDBACK,
            "trigger_run_ids": [request["request_id"].rsplit(":", 1)[-1]],
            "interrupted_dd_ids": [
                key for key, dd in status.get("dds", {}).items() if dd.get("step") == "interrupted"
            ],
            "request": request,
        },
    )
    try:
        response = await call(client, "goal_message", request)
        write(folder / "response.json", response)
        if (
            response.get("request_id") != request["request_id"]
            or response.get("queued") is not True
            or response.get("requires_resume") is not False
        ):
            raise ValueError("公开反馈未确认入队到活跃引擎；不执行 resume")
    except Exception as exc:
        write(folder / "error.json", {"error": str(exc), "attempt": policy.attempts})
        return "stop"
    return "sent"
