"""Thin CLI entrypoint.

`version`, `hello`, `line run`, `dd run`, `scheduler run`, `inbox list`, and
`supervise audit`.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from fleet_graph import __version__


def _hello(args: argparse.Namespace) -> int:
    """Run hello-graph for real, through the gateway."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from fleet_graph.executors.text_node import TextNode
    from fleet_graph.graphs.hello import HelloConfig, build_hello_graph

    node = TextNode()
    graph = build_hello_graph(node, HelloConfig(drafter=args.drafter, critic=args.critic))

    with SqliteSaver.from_conn_string(args.checkpoint) as saver:
        compiled = graph.compile(checkpointer=saver)
        state = compiled.invoke(
            {"topic": args.topic},
            config={"configurable": {"thread_id": args.thread}},
        )

    json.dump(
        {
            "topic": state.get("topic"),
            "draft": state.get("draft"),
            "critique": state.get("critique"),
            "usage": state.get("usage", []),
        },
        sys.stdout,
        ensure_ascii=False,
        indent=1,
    )
    sys.stdout.write("\n")
    return 0


def _line_run(args: argparse.Namespace) -> int:
    """Run one ronin line to termination, printing its terminal record."""
    import pathlib

    from fleet_graph.graphs.runner import LineConfig, run_line

    config = LineConfig(
        folder_id=args.folder,
        seat=args.seat,
        run_root=pathlib.Path(args.run_root or f"/data/fleet-graph/runs/{args.folder}"),
        max_rounds=args.max_rounds,
        noop_limit=args.noop_limit,
        timeout_limit=args.timeout_limit,
        turn_timeout_seconds=args.turn_timeout,
        coordinator_timeout_seconds=args.coordinator_timeout,
        alias=args.alias,
        generation=args.generation,
        # None -> durable default under run_root; ":memory:" must be asked for.
        checkpoint_path=args.checkpoint,
    )
    result = run_line(config, run_id=args.run_id)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    # A line that ended `done` is not the same as a line that ended well; the
    # exit code reports termination, and acceptance stays a human's job.
    return 0 if result.get("terminal") in {"done", "blocked", "bounds"} else 1


def plugin_binding_config(path: Any) -> dict[str, Any]:
    """Read a plugin binding, whole config or bare section.

    dd already runs on one of these; being able to point at a copy of it
    without editing is the difference between wiring this up and transcribing
    twelve digests by hand.
    """
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} does not hold an object")
    if "plugin_producer" in loaded:
        return loaded
    return {"plugin_producer": loaded}


def _dd_run(args: argparse.Namespace) -> int:
    """Run one development through the dd pipeline to termination."""
    import pathlib

    from fleet_graph.dd.bootstrap import IdentityChanged, committed_target_base
    from fleet_graph.dd.git import run_git
    from fleet_graph.dd.vendor.plugin_adapter import load_plugin_binding
    from fleet_graph.graphs.dd_runner import DevelopmentConfig, run_pipeline

    if args.resume and not args.checkpoint:
        # An in-memory checkpointer has no thread to resume. Silently starting
        # over would re-dispatch stages that are already sealed.
        raise SystemExit("--resume needs the --checkpoint the run was started with")

    workspace = pathlib.Path(args.workspace).resolve()
    binding = load_plugin_binding(plugin_binding_config(pathlib.Path(args.plugin_binding)))

    head = args.spec_commit
    if not head:
        head = run_git(workspace, "rev-parse", "HEAD", check=True).stdout.strip()

    if args.target_base:
        target_base = args.target_base
    else:
        try:
            target_base = committed_target_base(workspace) or head
        except IdentityChanged as changed:
            # A refusal, not a crash: the operator has a legible next step.
            raise SystemExit(str(changed)) from changed

    run_root = pathlib.Path(args.run_root or f"/data/fleet-graph/dd/{args.development}")
    config = DevelopmentConfig(
        development_id=args.development,
        workspace_path=workspace,
        state_root=pathlib.Path(args.state_root or run_root / "state"),
        run_root=run_root,
        remote_url=args.remote_url,
        remote_ref=args.remote_ref,
        # The identity the development committed wins over HEAD: by now HEAD
        # has moved past the base the spec was approved against.
        target_base_commit=target_base,
        root_handoff_digest=args.root_digest,
        plugin_binding=binding,
        head_commit=head,
        generation=args.generation,
        checkpoint_path=args.checkpoint or ":memory:",
        run_config={"acceptance_commands": [c.split() for c in args.accept]},
        models=dict(pair.split("=", 1) for pair in args.stage_model),
        publish_merge=args.publish_merge,
    )

    board = None
    if args.board_card:
        from fleet_graph.bus.board import Board
        from fleet_graph.bus.client import BusClient

        board = Board(BusClient())

    result = run_pipeline(
        config,
        board=board,
        gate_card_entity_id=args.board_card or "",
        resume=args.resume,
    )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    # `complete` is the only ending that means the pipeline did what it was
    # asked. A refusal is a legitimate answer, not a success. Waiting on a
    # human is neither, and gets its own code so a caller can tell "come back
    # later" apart from "this failed".
    if result.get("terminal") == "complete":
        return 0
    return 75 if result.get("awaiting") else 1


def _dd_bootstrap(args: argparse.Namespace) -> int:
    """Write and commit the attempt context a development starts from."""
    import pathlib

    from fleet_graph.dd.bootstrap import build_attempt_context
    from fleet_graph.dd.git import run_git

    workspace = pathlib.Path(args.workspace).resolve()

    def git(*argv: str) -> str:
        return run_git(workspace, *argv, check=True).stdout.strip()

    base = args.target_base or git("rev-parse", "HEAD")
    context = build_attempt_context(
        development_id=args.development,
        spec=pathlib.Path(args.spec).read_bytes(),
        target_base_commit=base,
    )
    context.write(workspace)

    git("add", "--", ".dev-dispatch")
    git(
        "-c",
        "user.name=Dev Dispatch",
        "-c",
        "user.email=dev-dispatch@example.invalid",
        "commit",
        "-q",
        "-m",
        f"dev-dispatch: bootstrap {args.development}",
    )

    json.dump(
        {
            "development_id": args.development,
            "target_base_commit": base,
            "spec_digest": context.spec_digest,
            "commit": git("rev-parse", "HEAD"),
            "files": sorted(context.files),
        },
        sys.stdout,
        ensure_ascii=False,
        indent=1,
    )
    sys.stdout.write("\n")
    return 0


def _scheduler_run(args: argparse.Namespace) -> int:
    """Run the resident scheduler: look at each line, ask, start or record why not."""
    import pathlib

    from fleet_graph.scheduler.daemon import Scheduler, SchedulerConfig
    from fleet_graph.scheduler.launcher import TransientLauncher
    from fleet_graph.scheduler.probe import GatewayProber, HttpxProbeTransport

    config = SchedulerConfig.from_json(pathlib.Path(args.config))
    scheduler = Scheduler(
        config,
        prober=None if args.no_probe else GatewayProber(HttpxProbeTransport()),
        launcher=TransientLauncher(dry_run=args.dry_run),
        observe=lambda result: print(json.dumps(result.as_dict(), ensure_ascii=False), flush=True),
    )

    if args.once:
        scheduler.tick()
        return 0
    scheduler.run_forever()
    return 0


def _inbox_list(args: argparse.Namespace) -> int:
    """Render the pending-verdict view straight off the board. Read-only."""
    from fleet_graph.bus.client import BusClient
    from fleet_graph.supervise.inbox import list_pending, render_text

    client = BusClient(base_url=args.bus_url)
    rows = list_pending(client)
    if args.json:
        json.dump([row.as_dict() for row in rows], sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        print(render_text(rows))
    return 0


def _supervise_audit(args: argparse.Namespace) -> int:
    """Mechanical audit of one development or goal line. Casts no verdict."""
    import pathlib

    from fleet_graph.supervise.audit import (
        DEFAULT_RUN_ROOT,
        OldEngineClient,
        audit_development,
        audit_goal_line,
        publish_report,
        render_note,
    )

    if args.target.startswith("wf-"):
        report = audit_goal_line(
            args.target, run_root=pathlib.Path(args.run_root or DEFAULT_RUN_ROOT)
        )
    else:
        if not args.repo:
            raise SystemExit("development 审计需要 --repo：一个持有 accepted commit 的本地 clone")
        report = audit_development(
            args.target,
            engine=OldEngineClient(args.engine_url),
            repo=pathlib.Path(args.repo).resolve(),
        )

    if args.no_note:
        report.gaps.append("evidence note 被 --no-note 抑制，仅本地输出")
    elif args.card:
        from fleet_graph.bus.client import BusClient

        result = publish_report(
            BusClient(base_url=args.bus_url),
            report,
            card_entity_id=args.card,
            question_note_id=args.question or "",
        )
        report.gaps.append(f"evidence note 已落板: {result.message_id}")
    else:
        # The ref contract needs a card to hang the note on; without one the
        # honest move is a local report plus a named gap, not a guessed ref.
        report.gaps.append("无 --card 可挂 evidence note（老引擎 gate 不产板卡）；报告仅本地输出")

    if args.json:
        json.dump(report.as_dict(), sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        print(render_note(report))
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-graph")
    parser.add_argument("--version", action="version", version=__version__)
    parser.set_defaults(func=lambda _: 0)

    subparsers = parser.add_subparsers()
    hello = subparsers.add_parser("hello", help="run hello-graph through the gateway")
    hello.add_argument("--topic", default="what a work-stealing scheduler is")
    hello.add_argument("--drafter", default="deepseek-v4-flash")
    hello.add_argument("--critic", default="glm-4.6")
    hello.add_argument("--thread", default="hello-1")
    hello.add_argument("--checkpoint", default=":memory:")
    hello.set_defaults(func=_hello)

    line = subparsers.add_parser("line", help="run a ronin line")
    line_sub = line.add_subparsers()
    run = line_sub.add_parser("run", help="run one line to termination")
    run.add_argument("--folder", required=True, help="work folder id")
    run.add_argument("--seat", required=True, help="worker seat from agents.yaml")
    run.add_argument("--run-root", default=None)
    run.add_argument("--max-rounds", type=int, default=10)
    run.add_argument("--noop-limit", type=int, default=3)
    run.add_argument("--timeout-limit", type=int, default=2)
    run.add_argument("--turn-timeout", type=int, default=3000)
    run.add_argument("--coordinator-timeout", type=int, default=2700)
    run.add_argument("--alias", default=None, help="agent-bus inbox alias")
    run.add_argument(
        "--generation",
        type=int,
        default=1,
        help="stable thread identity is folder:g{generation}; a restart of the "
        "same generation resumes its checkpoint and re-adopts in-flight runs",
    )
    run.add_argument(
        "--checkpoint",
        default=None,
        help="checkpoint sqlite path; defaults to <run-root>/checkpoint.sqlite3",
    )
    run.add_argument("--run-id", default=None)
    run.set_defaults(func=_line_run)

    dd = subparsers.add_parser("dd", help="run a dev-dispatch development")
    dd_sub = dd.add_subparsers()
    dd_run = dd_sub.add_parser("run", help="run one development to termination")
    dd_run.add_argument("--development", required=True, help="development id")
    dd_run.add_argument("--workspace", required=True, help="the git worktree to work in")
    dd_run.add_argument(
        "--plugin-binding",
        required=True,
        help="JSON file holding the plugin_producer section dd already runs on",
    )
    dd_run.add_argument("--remote-url", required=True)
    dd_run.add_argument("--remote-ref", required=True, help="refs/heads/... durable ref")
    dd_run.add_argument("--root-digest", required=True, help="sha256: of the initial handoff")
    dd_run.add_argument("--spec-commit", default=None, help="defaults to the workspace HEAD")
    dd_run.add_argument(
        "--target-base",
        default=None,
        help="defaults to the target_base_commit the development committed, "
        "then to the spec commit",
    )
    dd_run.add_argument("--generation", type=int, default=1)
    dd_run.add_argument("--run-root", default=None)
    dd_run.add_argument("--state-root", default=None)
    dd_run.add_argument("--checkpoint", default=None)
    dd_run.add_argument(
        "--accept",
        action="append",
        default=[],
        help="an acceptance command; repeatable",
    )
    dd_run.add_argument("--board-card", default=None, help="card entity id; enables the gate")
    dd_run.add_argument(
        "--resume",
        action="store_true",
        help="resume the thread this development already suspended, instead of starting it. "
        "Carries no verdict: the gate re-reads the board itself. Needs the same --checkpoint",
    )
    dd_run.add_argument(
        "--stage-model",
        action="append",
        default=[],
        metavar="STAGE=MODEL",
        help="override one stage's model, e.g. continuous_review=deepseek-v4-pro. "
        "The role's own selector is the default and stays the policy",
    )
    dd_run.add_argument(
        "--publish-merge",
        action="store_true",
        help="push the durable ref. Off by default: it is the one step here that cannot be undone",
    )
    dd_run.set_defaults(func=_dd_run)

    dd_boot = dd_sub.add_parser(
        "bootstrap", help="write and commit the attempt context a development starts from"
    )
    dd_boot.add_argument("--development", required=True)
    dd_boot.add_argument("--workspace", required=True)
    dd_boot.add_argument("--spec", required=True, help="the approved spec to freeze")
    dd_boot.add_argument("--target-base", default=None, help="defaults to the workspace HEAD")
    dd_boot.set_defaults(func=_dd_bootstrap)

    scheduler = subparsers.add_parser("scheduler", help="the resident line scheduler")
    scheduler_sub = scheduler.add_subparsers()
    sched_run = scheduler_sub.add_parser("run", help="tick until stopped")
    sched_run.add_argument("--config", required=True, help="JSON config: lines, roots, caps")
    sched_run.add_argument("--once", action="store_true", help="one tick, then exit")
    sched_run.add_argument(
        "--dry-run", action="store_true", help="decide and log, but launch nothing"
    )
    sched_run.add_argument(
        "--no-probe",
        action="store_true",
        help="run without a gateway probe. Every line then refuses on no_probe, "
        "which is the honest reading of not being able to ask",
    )
    sched_run.set_defaults(func=_scheduler_run)

    from fleet_graph.bus.client import DEFAULT_BUS_URL

    inbox = subparsers.add_parser("inbox", help="the pending-verdict view of the board")
    inbox_sub = inbox.add_subparsers()
    inbox_list = inbox_sub.add_parser(
        "list", help="questions on the board that no work.decision.v1 references"
    )
    inbox_list.add_argument("--json", action="store_true")
    inbox_list.add_argument("--bus-url", default=DEFAULT_BUS_URL)
    inbox_list.set_defaults(func=_inbox_list)

    supervise = subparsers.add_parser(
        "supervise", help="the supervision face (audits, no verdicts)"
    )
    supervise_sub = supervise.add_subparsers()
    audit = supervise_sub.add_parser(
        "audit",
        help="mechanical evidence audit of a development id or a wf- folder id",
    )
    audit.add_argument("target", help="development_id, or a goal line's wf- folder id")
    audit.add_argument("--json", action="store_true")
    audit.add_argument(
        "--repo",
        default=None,
        help="local clone holding the development's commits (required for development targets)",
    )
    audit.add_argument(
        "--engine-url",
        default="http://127.0.0.1:7460",
        help="legacy controller base URL; only ever queried with GET",
    )
    audit.add_argument(
        "--run-root", default=None, help="goal line run root (default /data/fleet-graph/runs)"
    )
    audit.add_argument("--card", default=None, help="card entity id to hang the evidence note on")
    audit.add_argument(
        "--question", default=None, help="question note id the evidence note should also reference"
    )
    audit.add_argument("--bus-url", default=DEFAULT_BUS_URL)
    audit.add_argument(
        "--no-note", action="store_true", help="print the report only; publish nothing to the board"
    )
    audit.set_defaults(func=_supervise_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
