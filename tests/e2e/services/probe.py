"""本次 Docker 网络内的真实服务探针；不接受外部服务 URL。"""

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def http_json(base, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base + path,
        headers=headers,
        data=None if body is None else json.dumps(body).encode(),
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


async def wf_probe(write=False):
    from fastmcp import Client

    base = "http://work-folder:5602/mcp" if write else "http://127.0.0.1:5602/mcp"
    async with Client(base, timeout=15) as client:

        async def call(name, arguments):
            result = await client.call_tool(name, arguments)
            require(not result.is_error, f"MCP {name} 返回错误")
            data = result.data
            if not isinstance(data, dict):
                data = json.loads(result.content[0].text)
            require(data.get("ok") is not False, f"MCP {name} 操作失败: {data}")
            return data

        await call("wf_list", {"limit": 1})
        if not write:
            return {"service": "work-folder", "readiness": "ok"}
        run_id = uuid.uuid4().hex
        created = await call("wf_create", {"topic": f"容器契约探针-{run_id}"})
        folder_id = created["folder_id"]
        content = f"# Docker E2E 探针\n\n标识：{run_id}\n"
        written = await call(
            "fs_create",
            {
                "folder_id": folder_id,
                "filename": "service-probe.md",
                "content": content,
            },
        )
        read = await call("fs_read_bytes", {"folder_id": folder_id, "filename": "service-probe.md"})
        require(read["eof"], "Work Folder 回读没有覆盖完整文件")
        require(
            base64.b64decode(read["content_base64"], validate=True) == content.encode("utf-8"),
            "Work Folder 回读字节不一致",
        )
        require(bool(written.get("commit")), "Work Folder 写入未返回 Git commit")
        return {
            "service": "work-folder",
            "read_write": "ok",
            "folder_id": folder_id,
            "commit": written["commit"],
            "content_revision": read.get("content_revision"),
        }


def bus_probe(write=False):
    base = "http://agent-bus:7470" if write else "http://127.0.0.1:7470"
    require(http_json(base, "/healthz")["database"] == "ok", "bus 数据库未就绪")
    require(http_json(base, "/readyz")["status"] == "ready", "bus bootstrap 未就绪")
    if not write:
        return {"service": "agent-bus", "readiness": "ok"}
    secret = Path("/run/secrets/bus_admin_token")
    admin = (
        secret.read_text(encoding="utf-8").strip()
        if secret.is_file()
        else os.environ["BUS_ADMIN_TOKEN"]
    )
    try:
        http_json(base, "/v1/agents")
    except urllib.error.HTTPError as error:
        require(error.code == 401, "bus 未鉴权请求没有返回 401")
    else:
        raise RuntimeError("bus 未鉴权请求错误地通过")
    run_id = uuid.uuid4().hex
    agents = []
    for suffix in ("sender", "receiver"):
        agents.append(
            http_json(
                base,
                "/v1/agents",
                admin,
                {
                    "agent_id": f"e2e-{run_id}-{suffix}",
                    "display_name": f"容器探针 {suffix}",
                    "kind": "agent",
                },
            )
        )
    sender, receiver = agents
    path = f"/v1/channels/{receiver['inbox_channel_id']}"
    payload = {"text": f"真实 bus 往返 {run_id}"}
    published = http_json(
        base,
        path + "/publish",
        sender["token"],
        {
            "kind": "message",
            "payload": payload,
            "idempotency_key": run_id,
        },
    )
    consumed = http_json(base, path + "/consume", receiver["token"], {"max_messages": 10})
    require(len(consumed["deliveries"]) == 1, "bus 没有交付唯一消息")
    delivery = consumed["deliveries"][0]
    require(delivery["message"]["payload"] == payload, "bus 回读消息内容不一致")
    acknowledged = http_json(
        base,
        f"/v1/deliveries/{delivery['delivery_id']}/ack",
        receiver["token"],
        {"lease_token": delivery["lease_token"]},
    )
    require(acknowledged.get("ok") is not False, "bus ack 失败")
    after = http_json(base, path + "/consume", receiver["token"], {"max_messages": 10})
    require(after["deliveries"] == [], "bus ack 后再次交付了相同消息")
    return {
        "service": "agent-bus",
        "read_write": "ok",
        "ack": "ok",
        "message_id": published["message_id"],
        "delivery_id": delivery["delivery_id"],
        "agents": [item["agent_id"] for item in agents],
    }


def git_sync_probe():
    root = "/data/work-folder"

    def git(*args):
        return subprocess.check_output(["git", "-C", root, *args], text=True).strip()

    require(
        git("remote", "get-url", "origin") == "git://git-remote:9418/work-folder.git",
        "禁止向本次内部 Git remote 以外的地址推送",
    )
    require(git("status", "--porcelain") == "", "Work Folder 数据仓存在未提交修改")
    git("push", "origin", "HEAD:main")
    head = git("rev-parse", "HEAD")
    remote = git("ls-remote", "origin", "refs/heads/main").split()[0]
    require(head == remote, "内部 Git remote 与 Work Folder HEAD 不一致")
    return {"service": "git-remote", "explicit_push": "ok", "commit": head}


def runtime_bus_messages():
    """读取本次完整频道记录；调用方先保存原始页，再做 lifecycle 判定。"""
    token = Path("/run/secrets/bus_admin_token").read_text(encoding="utf-8").strip()
    pages, messages, cursor = [], [], 0
    for _ in range(1000):
        page = http_json(
            "http://agent-bus:7470",
            f"/v1/channels/board:agent-runs/messages?after_seq={cursor}&limit=100",
            token,
        )
        pages.append(page)
        rows = page["messages"]
        if not rows:
            return {"channel": "board:agent-runs", "pages": pages, "messages": messages}
        seqs = [row["channel_seq"] for row in rows]
        require(seqs == sorted(set(seqs)) and seqs[0] > cursor, "bus 消息分页未严格推进")
        messages.extend(rows)
        cursor = seqs[-1]
    raise RuntimeError("bus 消息超过探针分页上限，拒绝把截断证据判为完整")


def verify_runtime_lifecycle(evidence, status):
    """要求真实 Goal/Impl run_id 的 started/exited 消息，普通bootstrap消息不能充数。"""
    require(evidence.get("channel") == "board:agent-runs", "runtime lifecycle 频道错误")
    matches = {}
    for role in ("goal", "impl"):
        expected = {
            run["ticket"]["run_id"]
            for run in status["runs"].values()
            if run.get("role") == role and run.get("result", {}).get("status") == "succeeded"
        }
        require(expected, f"缺少成功 {role} runtime ticket，无法核对 bus lifecycle")
        for run_id in sorted(expected):
            rows = [
                row
                for row in evidence["messages"]
                if row.get("sender_agent_id") == "mcp-gateway"
                and row.get("payload", {}).get("run_id") == run_id
                and re.fullmatch(r"agent\.run\.(started|exited)\.v[123]", row.get("kind", ""))
            ]
            for started in rows:
                if ".started." not in started["kind"]:
                    continue
                payload = started["payload"]
                if "fleet_role" in payload.get("labels", {}):
                    require(payload["labels"]["fleet_role"] == role, "bus lifecycle 角色标签不一致")
                for exited in rows:
                    if exited["kind"] != started["kind"].replace(".started.", ".exited."):
                        continue
                    code = exited["payload"].get("exit_code")
                    if (
                        type(code) is int
                        and code == 0
                        and exited["channel_seq"] > started["channel_seq"]
                    ):
                        matches[role] = {
                            "run_id": run_id,
                            "started_message_id": started["message_id"],
                            "exited_message_id": exited["message_id"],
                            "version": started["kind"].rsplit(".", 1)[-1],
                        }
        require(role in matches, f"真实 bus 缺失成功 {role} 的 started/exited 成对记录")
    return {"service": "agent-bus", "runtime_lifecycle": "passed", "roles": matches}


def main():
    require(Path("/.dockerenv").exists(), "探针只允许在本次 Docker 容器内执行")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "probe",
        choices=[
            "work-folder-health",
            "work-folder-read-write",
            "bus-health",
            "bus-read-write",
            "git-sync",
        ],
    )
    probe = parser.parse_args().probe
    if probe.startswith("work-folder-"):
        result = asyncio.run(wf_probe(write=probe.endswith("read-write")))
    elif probe.startswith("bus-"):
        result = bus_probe(write=probe.endswith("read-write"))
    else:
        result = git_sync_probe()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
