"""联合阶段才执行的真实 MCP 驱动；本开发阶段未运行。"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from fleet_graph.ports import MCPPort


async def main(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    port = MCPPort(args.url)
    request = json.loads(Path(args.request).read_text())
    enrolled = await port.call("goal_enroll", {"request": request})
    goal_id = enrolled["goal_id"]
    (output / "enrolled.json").write_text(json.dumps(enrolled, ensure_ascii=False, indent=2))
    started = time.monotonic()
    status = enrolled
    while time.monotonic() - started < args.timeout:
        status = await port.call("goal_status", {"goal_id": goal_id})
        (output / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2))
        if status["status"] in {"done", "stopped", "blocked"}:
            break
        await asyncio.sleep(2)
    cursor = 0
    with (output / "events.jsonl").open("w") as log:
        while True:
            page = await port.call(
                "goal_events", {"goal_id": goal_id, "after": cursor, "limit": 1000}
            )
            for event in page["events"]:
                log.write(json.dumps(event, ensure_ascii=False) + "\n")
            if page["next"] == cursor:
                break
            cursor = page["next"]
    if status["status"] != "done":
        await port.call("goal_stop", {"goal_id": goal_id, "immediate": False})
        raise SystemExit(f"联合验收未完成：{status['status']}，原始证据已保存")
    if len(status["finalized"]) != len(status["repos"]):
        raise SystemExit("done 与逐 repo 交付记录不一致")
    if any(dd["step"] not in {"done", "cancelled"} for dd in status["dds"].values()):
        raise SystemExit("done 仍有未终结 DD")
    print(
        json.dumps(
            {
                "goal_id": goal_id,
                "status": "运行场景完成，须继续按联合清单核对恢复与证据",
                "evidence": str(output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:15611/mcp")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=7200)
    asyncio.run(main(parser.parse_args()))
