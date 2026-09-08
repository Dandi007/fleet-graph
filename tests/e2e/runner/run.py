"""在独立容器中登记固定任务、调用公开 API 并采集原始证据。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from adapter import Mapper
from fastmcp import Client
from feedback import FeedbackPolicy, send_feedback
from monitor import BlockedIdleMonitor, EngineAbsentMonitor
from permissions import prepare_readable

HARNESS = Path("/harness")
REPO = Path("/workspace/fixture")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def http(url, token=None, body=None, timeout=30):
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


async def call(client, name, args):
    response = await client.call_tool(name, args)
    if response.is_error:
        raise RuntimeError(f"{name}: {response}")
    value = response.structured_content
    if value is None:
        value = json.loads(response.content[0].text)
    if isinstance(value, dict) and value.get("ok") is False:
        raise RuntimeError(f"{name}: {value}")
    return value


def service_probe():
    spec = importlib.util.spec_from_file_location("service_probe", HARNESS / "services/probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(*args):
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": str(HARNESS / "runner/askpass.py"),
    }
    return subprocess.check_output(
        ["git", "-c", "safe.directory=*", "-c", "credential.helper=", "-C", str(REPO), *args],
        env=env,
        text=True,
        stderr=subprocess.PIPE,
        timeout=120,
    ).strip()


def seed(repository, target):
    if REPO.exists():
        raise RuntimeError("本次 workspace 已存在 fixture；禁止覆盖或接管旧运行")
    shutil.copytree(HARNESS / "contract/fixture", REPO)
    git("init", "-b", target)
    git("config", "user.name", "Fleet Docker E2E")
    git("config", "user.email", "fleet-e2e@example.invalid")
    git("config", "credential.helper", "!gh auth git-credential")
    git("remote", "add", "origin", f"https://github.com/{repository}.git")
    if git("ls-remote", "origin", f"refs/heads/{target}"):
        raise RuntimeError("专用 target 已存在；禁止覆盖")
    git("add", ".")
    git("commit", "-m", "建立固定 slugify-v1 测试基线")
    git("push", "origin", f"HEAD:refs/heads/{target}")
    for path in [Path("/workspace"), REPO, *REPO.rglob("*")]:
        os.chown(path, 10001, 10001)
    return git("rev-parse", "HEAD")


async def smoke(bundle):
    checks = []
    manifest = http("http://candidate:15611/candidate-manifest")
    write(bundle / "raw/candidate-manifest.json", manifest)
    async with Client(os.environ["CANDIDATE_URL"], timeout=30) as client:
        names = sorted(tool.name for tool in await client.list_tools())
        required = {"goal_enroll", "goal_status", "goal_events", "goal_session", "goal_artifact"}
        if not required <= set(names):
            raise RuntimeError("候选公开 MCP 工具缺失")
        write(bundle / "raw/tools.json", names)
        checks.append({"name": "candidate_mcp", "status": "passed", "detail": names})
    token = Path("/run/secrets/gateway_token").read_text().strip()
    response = await asyncio.to_thread(
        http,
        "http://gateway:15722/v1/chat/completions",
        token,
        {
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "只回复 OK"}],
            "max_tokens": 64,
            "stream": False,
        },
        90,
    )
    if not response.get("choices"):
        raise RuntimeError("真实模型网关未返回 choices")
    write(bundle / "raw/gateway-smoke.json", response)
    checks.append({"name": "real_gateway", "status": "passed", "detail": "收到真实模型响应"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(
            {"https": os.environ["HTTPS_PROXY"], "http": os.environ["HTTP_PROXY"]}
        )
    )
    try:
        opener.open("http://example.com", timeout=10)
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
    else:
        raise RuntimeError("egress 未拒绝 example.com")
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=3):
            pass
    except OSError:
        pass
    else:
        raise RuntimeError("容器存在直接出网路径")
    checks.append({"name": "egress_boundary", "status": "passed", "detail": "代理拒绝及直连拒绝"})
    probe = service_probe()
    wf = await probe.wf_probe(write=True)
    bus = await asyncio.to_thread(probe.bus_probe, write=True)
    write(bundle / "raw/service-probes.json", {"work_folder": wf, "agent_bus": bus})
    checks.append(
        {"name": "service_round_trips", "status": "passed", "detail": {"wf": wf, "bus": bus}}
    )
    return checks, manifest


async def collect(client, bundle, goal_id, status):
    events, cursor, pages = [], 0, []
    while True:
        page = await call(
            client, "goal_events", {"goal_id": goal_id, "after": cursor, "limit": 1000}
        )
        pages.append(page)
        events.extend(page["events"])
        if page["next"] == cursor:
            break
        if page["next"] < cursor:
            raise RuntimeError("events 游标回退")
        cursor = page["next"]
    write(bundle / "raw/event-pages.json", pages)
    write(bundle / "raw/events.json", {"events": events})
    write(bundle / "raw/status.json", status)
    errors = []
    for run_id, run in status["runs"].items():
        if run["role"] == "acceptance":
            names = [f"commands/{run_id}/result.json", f"commands/{run_id}/input.json"]
            for filename in names:
                try:
                    blob, offset = bytearray(), 0
                    while True:
                        page = await call(
                            client,
                            "goal_artifact",
                            {
                                "goal_id": goal_id,
                                "filename": filename,
                                "offset": offset,
                                "limit": 65536,
                            },
                        )
                        blob.extend(base64.b64decode(page["data"], validate=True))
                        if page["next"] >= page["size"]:
                            break
                        if page["next"] <= offset:
                            raise RuntimeError("artifact 游标未推进")
                        offset = page["next"]
                    path = bundle / "raw/artifacts" / filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(blob)
                except Exception as exc:
                    errors.append({"artifact": filename, "error": str(exc)})
            continue
        pages, offset = [], 0
        try:
            while True:
                page = await call(
                    client,
                    "goal_session",
                    {"goal_id": goal_id, "run_id": run_id, "offset": offset, "limit": 1000},
                )
                pages.append(page)
                following = page.get("next_offset")
                if following is None:
                    break
                if following <= offset:
                    raise RuntimeError("Session 游标未推进")
                offset = following
            write(bundle / f"raw/sessions/{run_id}.json", {"pages": pages})
        except Exception as exc:
            errors.append({"session": run_id, "error": str(exc)})
    write(bundle / "raw/collection-errors.json", errors)


async def stop_goal(client, bundle, goal_id, status, reason):
    write(bundle / "raw/stop-reason.json", {"reason": reason, "observed_status": status})
    try:
        status = await call(client, "goal_stop", {"goal_id": goal_id, "immediate": True})
    except Exception as exc:
        write(bundle / "raw/stop-response.json", {"error": str(exc)})
        return status  # 停止接口失败也继续采集原始状态，不把 uncertain 改写成终态。
    write(bundle / "raw/stop-response.json", status)
    for _ in range(30):
        if not status["engine_alive"]:
            break
        await asyncio.sleep(2)
        status = await call(client, "goal_status", {"goal_id": goal_id})
    return status


def pull_requests(bundle, repository, target, source, status):
    token = Path(os.environ["GH_TOKEN_FILE"]).read_text().strip()
    owner, name = repository.split("/")
    branches = [(source, dd["source_branch"]) for dd in status["dds"].values()]
    branches.append((target, source))
    for base, head in branches:
        query = urllib.parse.urlencode({"state": "all", "base": base, "head": f"{owner}:{head}"})
        prs = http(f"https://api.github.com/repos/{repository}/pulls?{query}", token)
        for pr in prs:
            number = pr["number"]
            query = """query($owner:String!, $name:String!, $number:Int!) {
              repository(owner:$owner,name:$name) { nameWithOwner
                pullRequest(number:$number) { url state baseRefName headRefName headRefOid
                  mergeCommit { oid } } } }"""
            raw = http(
                "https://api.github.com/graphql",
                token,
                {"query": query, "variables": {"owner": owner, "name": name, "number": number}},
            )
            if raw.get("errors"):
                raise RuntimeError(f"GitHub GraphQL: {raw['errors']}")
            write(bundle / f"raw/prs/{number}.json", raw)


async def e2e(bundle, candidate):
    run_id = os.environ["E2E_RUN_ID"]
    repository = os.environ["FIXTURE_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("FIXTURE_REPOSITORY 必须为 owner/repo")
    if repository.split("/")[1] != "fleet-graph-e2e-fixtures":
        raise ValueError("只能向专用 fleet-graph-e2e-fixtures 仓库写入")
    target, source = f"e2e/{run_id}/target", f"release/e2e/{run_id}"
    seed_commit = await asyncio.to_thread(seed, repository, target)
    async with Client(os.environ["WF_URL"], timeout=30) as wf:
        created = await call(wf, "wf_create", {"topic": f"Docker E2E slugify {run_id}"})
        write(bundle / "raw/wf-create.json", created)
        folder_id = created["folder_id"]
        body = (HARNESS / "contract/goal.md").read_text()
        body += (
            f"\n本次运行：{run_id}；repo：{REPO}；remote：origin；release：{source}；"
            f"最终 target：{target}。DD worktree 必须位于 /workspace/dd-{run_id}。\n"
            "整线 PR 需要你在请求 done 前创建；done 的程序 Git 合并会推进 target。\n"
        )
        written = await call(
            wf, "fs_create", {"folder_id": folder_id, "filename": "goal.md", "content": body}
        )
        write(bundle / "raw/wf-goal-write.json", written)
    request = {
        "schema": "goal.enroll/2",
        "request_id": f"enroll-{run_id}",
        "work_folder": folder_id,
        "title": f"固定 slugify-v1 / {run_id}",
        "source_branch": source,
        "repos": [
            {
                "path": str(REPO),
                "remote": "origin",
                "target_branch": target,
                "acceptance": ["PYTHONDONTWRITEBYTECODE=1 make verify"],
            }
        ],
    }
    write(bundle / "raw/enroll-request.json", request)
    async with Client(os.environ["CANDIDATE_URL"], timeout=180) as client:
        status = await call(client, "goal_enroll", {"request": request})
        write(bundle / "raw/enroll-response.json", status)
        goal_id = status["goal_id"]
        commits = candidate["source"]
        manifest = {
            "schema": "fleet-e2e.run/1",
            "run_id": run_id,
            "candidate": {
                "name": commits["candidate"],
                "commits": {
                    "fleet_graph": commits["fleet_commit"],
                    "agent_runtime": commits["runtime_commit"],
                    "katana": commits["katana_commit"],
                },
            },
            "repository": repository,
            "target_branch": target,
            "source_branch": source,
            "goal_id": goal_id,
            "fixture": "slugify-v1",
            "seed_commit": seed_commit,
        }
        write(bundle / "manifest.json", manifest)
        deadline = time.monotonic() + int(os.environ.get("E2E_TIMEOUT", "3600"))
        blocked_idle = BlockedIdleMonitor()
        engine_absent = EngineAbsentMonitor()
        feedback = FeedbackPolicy()
        while time.monotonic() < deadline:
            status = await call(client, "goal_status", {"goal_id": goal_id})
            write(bundle / "raw/status.json", status)
            print(
                json.dumps(
                    {
                        "goal_id": goal_id,
                        "status": status["status"],
                        "dds": {key: dd["step"] for key, dd in status["dds"].items()},
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if status["status"] == "done" and not status["engine_alive"]:
                break
            if engine_absent.observe(status):
                status = await stop_goal(
                    client,
                    bundle,
                    goal_id,
                    status,
                    "连续两轮引擎离线且目标非 done，公开状态无变化；保留 uncertain/lost 证据",
                )
                break
            if blocked_idle.observe(status):
                try:
                    feedback_result = await send_feedback(
                        feedback, client, call, write, bundle, run_id, status
                    )
                except Exception as exc:
                    write(bundle / "raw/feedback/error.json", {"error": str(exc)})
                    feedback_result = "stop"
                if feedback_result != "stop":
                    blocked_idle = BlockedIdleMonitor()
                    engine_absent = EngineAbsentMonitor()
                    await asyncio.sleep(10)
                    continue
                status = await stop_goal(
                    client,
                    bundle,
                    goal_id,
                    status,
                    "连续两轮 blocked，所有 run finished 且公开状态无变化",
                )
                break
            await asyncio.sleep(10)
        else:
            status = await stop_goal(client, bundle, goal_id, status, "超过 E2E_TIMEOUT")
        await collect(client, bundle, goal_id, status)
    probe = service_probe()
    try:
        bus_evidence = await asyncio.to_thread(probe.runtime_bus_messages)
        write(bundle / "raw/runtime-bus.json", bus_evidence)
        bus_verdict = probe.verify_runtime_lifecycle(bus_evidence, status)
        write(bundle / "raw/runtime-bus-verification.json", bus_verdict)
    except Exception as exc:
        path = bundle / "raw/collection-errors.json"
        errors = json.loads(path.read_text())
        errors.append({"service": "runtime-bus", "error": str(exc)})
        write(path, errors)
    await asyncio.to_thread(pull_requests, bundle, repository, target, source, status)
    write(bundle / "snapshot.json", Mapper(bundle).snapshot())
    if status["engine_alive"]:
        raise RuntimeError("引擎仍活跃，不能变更最终 checkout")
    git("fetch", "origin", f"refs/heads/{target}")
    git("checkout", "--detach", "FETCH_HEAD")
    write(bundle / "raw/checkout-permissions.json", prepare_readable(REPO))
    write(
        bundle / "raw/target-checkout.json",
        {"repository": repository, "target": target, "head": git("rev-parse", "HEAD")},
    )
    if status["status"] != "done":
        raise RuntimeError(f"Goal 未完成：{status['status']}")
    return {
        "name": "goal_collection",
        "status": "passed",
        "detail": "目标终结且公共证据已采集；业务结果必须再经独立 verifier",
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["smoke", "e2e"], default="e2e")
    mode = parser.parse_args().mode
    if not Path("/.dockerenv").exists():
        raise RuntimeError("runner 仅允许在 Docker 内执行")
    run_id = os.environ["E2E_RUN_ID"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("E2E_RUN_ID 必须是安全的独立运行标识")
    bundle = Path("/artifacts") / run_id
    report = {
        "schema": "fleet-e2e.report/1",
        "run_id": run_id,
        "status": "failed",
        "candidate": {},
        "checks": [],
        "evidence": [],
    }
    try:
        checks, candidate = await smoke(bundle)
        report["checks"].extend(checks)
        report["candidate"] = candidate["source"]
        if mode == "e2e":
            report["checks"].append(await e2e(bundle, candidate))
        report["status"] = "passed" if mode == "smoke" else "collected"
    except Exception as exc:
        report["checks"].append({"name": "runner", "status": "failed", "detail": str(exc)[:4000]})
    report["evidence"] = [
        str(p.relative_to(bundle)) for p in sorted((bundle / "raw").rglob("*.json"))
    ]
    write(bundle / "runner-report.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["status"] in {"passed", "collected"} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
