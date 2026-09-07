"""服务管理的新增线名册；版本库名册仅提供已有线种子。"""

import json
import os
from pathlib import Path

from fleet_graph.dd.operation_lock import operation_lock
from fleet_graph.state.run_artifacts import write_json_durable


def roster_path() -> Path:
    return Path(os.environ.get("FLEET_GRAPH_RUNTIME_ROSTER", "/data/fleet-graph/goal/roster.json"))


def runtime_entries() -> list[dict]:
    path = roster_path()
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    entries = raw.get("lines")
    if not isinstance(entries, list) or any(
        not isinstance(e, dict) or not e.get("folder_id") for e in entries
    ):
        raise ValueError("运行名册格式损坏")
    return entries


def merge_lines(seed: list[dict]) -> list[dict]:
    merged = {e["folder_id"]: e for e in seed if isinstance(e, dict) and e.get("folder_id")}
    merged.update({e["folder_id"]: e for e in runtime_entries()})
    return list(merged.values())


def seed_entries() -> list[dict]:
    seed = Path(os.environ.get("FLEET_GRAPH_LINES_CONFIG", "config/ronin-lines.json"))
    if not seed.exists():
        return []
    return json.loads(seed.read_text()).get("lines", [])


def admit_line(entry: dict) -> dict:
    path = roster_path()
    with operation_lock(path.with_suffix(".lock")):
        lines = runtime_entries()
        for seeded in seed_entries():
            if (
                seeded.get("alias") == entry["alias"]
                and seeded.get("folder_id") != entry["folder_id"]
            ):
                raise ValueError("alias 已被种子名册中的工作线占用")
        current = next((e for e in lines if e["folder_id"] == entry["folder_id"]), None)
        if current:
            if current["alias"] != entry["alias"]:
                raise ValueError("同一工作线的 alias 不可在入编重投中改变")
            return current
        if any(e["alias"] == entry["alias"] for e in lines):
            raise ValueError("alias 已被其他工作线占用")
        line = {
            "folder_id": entry["folder_id"],
            "alias": entry["alias"],
            "seat": entry.get("seat_hint") or "opencode-glm53",
            "max_rounds": entry.get("max_rounds") or 100,
            "enabled": True,
            "acceptance_digest": entry["acceptance_digest"],
            "acceptance_argv": entry["acceptance_argv"],
        }
        write_json_durable(path, {"lines": [*lines, line]})
        return line
