"""Codex 公开 API 字段映射；不会构造成功结果。"""

import json
from pathlib import Path


def pointer_escape(value):
    return str(value).replace("~", "~0").replace("/", "~1")


class Mapper:
    def __init__(self, bundle: Path):
        self.bundle = bundle

    def value(self, file, pointer):
        value = json.loads((self.bundle / file).read_text())
        for key in pointer.split("/")[1:]:
            key = key.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value

    def record(self, fields):
        values, sources = {}, {}
        for name, (file, pointer) in fields.items():
            try:
                values[name] = self.value(file, pointer)
            except (KeyError, IndexError, TypeError):
                continue  # 缺失原始证据就留缺口，验收器拒绝。
            sources[name] = {"file": file, "pointer": pointer}
        return {"values": values, "sources": sources}

    def snapshot(self):
        status_file = "raw/status.json"
        status = self.value(status_file, "")
        events_file = "raw/events.json"
        events = self.value(events_file, "/events")
        result = {
            "schema": "fleet-e2e.snapshot/1",
            "events": {"file": events_file, "pointer": "/events"},
            **{name: [] for name in ("dds", "runs", "reviews", "acceptances", "approvals", "prs")},
        }
        event_by_run = {}
        dispatch = {}
        observed_scribes = {}
        done_index = None
        for index, event in enumerate(events):
            payload = event["payload"]
            if event["kind"] == "run.intent":
                event_by_run[payload["run_id"]] = index
            if event["kind"] == "dd.dispatched":
                dispatch[payload["dd_id"]] = index
            if event["kind"] == "scribe.observed":
                observed_scribes[payload["run_id"]] = index
            if event["kind"] == "goal.done":
                done_index = index
        for run_id, run in status["runs"].items():
            base = "/runs/" + pointer_escape(run_id)
            if run["role"] != "acceptance":
                fields = {
                    "run_id": (status_file, base + "/ticket/run_id"),
                    "role": (status_file, base + "/role"),
                    "status": (status_file, base + "/result/status"),
                    "session_id": (status_file, base + "/result/session_ref"),
                    "dd_id": (status_file, base + "/owner"),
                }
                if run["role"] == "scribe" and run_id in event_by_run:
                    idx = event_by_run[run_id]
                    fields.update(
                        {
                            "final": (events_file, f"/events/{idx}/payload/prompt/final"),
                            "event_range": (
                                events_file,
                                f"/events/{idx}/payload/prompt/event_range",
                            ),
                        }
                    )
                    if run_id in observed_scribes:
                        obs = observed_scribes[run_id]
                        fields.update(
                            {
                                "observed_seq": (events_file, f"/events/{obs}/seq"),
                                "observed_run_id": (events_file, f"/events/{obs}/payload/run_id"),
                                "observations": (
                                    events_file,
                                    f"/events/{obs}/payload/output/observations",
                                ),
                            }
                        )
                result["runs"].append(self.record(fields))
            elif run_id in event_by_run:
                idx = event_by_run[run_id]
                result["acceptances"].append(
                    self.record(
                        {
                            "dd_id": (status_file, base + "/owner"),
                            "run_id": (status_file, base + "/ticket/run_id"),
                            "status": (status_file, base + "/result/status"),
                            "results": (status_file, base + "/result/results"),
                            "commit": (events_file, f"/events/{idx}/payload/prompt/head"),
                            "workspace": (events_file, f"/events/{idx}/payload/prompt/workspace"),
                        }
                    )
                )
            if run["role"] == "goal":
                actions = run.get("result", {}).get("output") or []
                for index, action in enumerate(actions if isinstance(actions, list) else []):
                    if not isinstance(action, dict) or action.get("type") != "approve":
                        continue
                    dd_id = action.get("dd_id")
                    if dd_id not in status["dds"]:
                        continue
                    action_base = base + f"/result/output/{index}"
                    dd_base = "/dds/" + pointer_escape(dd_id)
                    result["approvals"].append(
                        self.record(
                            {
                                "dd_id": (status_file, action_base + "/dd_id"),
                                "commit": (status_file, dd_base + "/head"),
                                "review_ref": (status_file, action_base + "/review_ref"),
                                "decision": (status_file, action_base + "/type"),
                                "goal_run_id": (status_file, base + "/ticket/run_id"),
                                "applied_review_ref": (status_file, dd_base + "/approved"),
                            }
                        )
                    )
        for dd_id, dd in status["dds"].items():
            base = "/dds/" + pointer_escape(dd_id)
            fields = {
                "dd_id": (status_file, base + "/dd_id"),
                "commit": (status_file, base + "/head"),
                "spec_path": (status_file, base + "/spec_path"),
                "pr_url": (status_file, base + "/pr/url"),
            }
            if dd_id in dispatch:
                fields["spec_commit"] = (events_file, f"/events/{dispatch[dd_id]}/payload/head")
            result["dds"].append(self.record(fields))
            for role in ("cr", "fr"):
                review_run = dd.get(role, {}).get("run_id")
                fields = {
                    "dd_id": (status_file, base + "/dd_id"),
                    "run_id": (status_file, base + f"/{role}/run_id"),
                    "commit": (status_file, base + f"/{role}/head"),
                    "verdict": (status_file, base + f"/{role}/output/type"),
                }
                if review_run:
                    fields["role"] = (status_file, "/runs/" + pointer_escape(review_run) + "/role")
                if role == "fr":
                    fields["review_ref"] = (status_file, base + "/review_ref")
                result["reviews"].append(self.record(fields))
        final_pr_file = None
        for path in sorted((self.bundle / "raw/prs").glob("*.json")):
            file = str(path.relative_to(self.bundle))
            pr_base = "/data/repository/pullRequest"
            pr = self.value(file, pr_base)
            manifest = json.loads((self.bundle / "manifest.json").read_text())
            if pr["baseRefName"] == manifest["target_branch"]:
                final_pr_file = file
            result["prs"].append(
                self.record(
                    {
                        **{
                            name: (file, pr_base + "/" + field)
                            for name, field in {
                                "url": "url",
                                "base": "baseRefName",
                                "head": "headRefName",
                                "head_sha": "headRefOid",
                                "merge_sha": "mergeCommit/oid",
                                "state": "state",
                            }.items()
                        },
                        "repository": (file, "/data/repository/nameWithOwner"),
                    }
                )
            )
        goal_fields = {"goal_id": (status_file, "/goal_id"), "status": (status_file, "/status")}
        if done_index is not None:
            goal_fields["done_seq"] = (events_file, f"/events/{done_index}/seq")
        if status.get("finalized"):
            repo_id = next(iter(status["finalized"]))
            goal_fields["commit"] = (
                status_file,
                "/finalized/" + pointer_escape(repo_id) + "/target_head",
            )
        if final_pr_file:
            goal_fields["pr_url"] = (final_pr_file, "/data/repository/pullRequest/url")
        result["goal"] = self.record(goal_fields)
        return result
