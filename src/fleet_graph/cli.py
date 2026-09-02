"""Thin CLI entrypoint.

`version`, `hello`, `line run`, `research run`, `research serve`, `dd run`,
`goal serve`, `scheduler run`, `inbox list`, and `supervise audit`.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import sys
import time
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


def default_research_run_root(question: str) -> str:
    """默认 run_root 由题面内容寻址派生——R3-fix 误双开保护的锚点。

    不显式给 ``--run-root`` 的两次同题启动落到同一 run_root ⇒ 同一 run instance
    ⇒ 同一 thread 身份 ⇒ 第二次启动领养而非并跑双烧。显式 ``--run-root`` 才进入
    「独立实例」语义（那本就该是显式动作）。抽成具名函数以便验收脚本直接断言
    这条保护，而不是复制派生表达式。
    """
    from fleet_graph.graphs.research_pipeline import derive_research_id

    return f"/data/fleet-graph/research/{derive_research_id(question)}"


def _research_run(args: argparse.Namespace) -> int:
    """Run one research ticket to termination, printing its terminal record.

    R6：经统一入口 ``research_entry.run_research_ticket``（与 MCP tool / skill
    同一路由）——按 ``--tier light|heavy`` 或确定性规模判定分档，finalise 侧归位
    report 到 wiki 域 ``DeepThought/<topic>/``。
    """
    import pathlib

    from fleet_graph.graphs.research_runner import default_publisher
    from fleet_graph.research_entry import run_research_ticket

    result = run_research_ticket(
        args.question,
        tier=args.tier,
        scale=args.scale,
        run_root=pathlib.Path(args.run_root) if args.run_root else None,
        generation=args.generation,
        max_clues=args.max_clues,
        concurrency=args.concurrency,
        checkpoint=args.checkpoint,
        instance=args.instance,
        publisher=default_publisher(),
        # R8 判据 ①：CLI 入口记录进程真实 argv（sys.argv），不是 canonical 重建值。
        launch_argv=list(sys.argv),
        launch_entry="cli",
    )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    # 终态 ∈ {converged, capped, partial} 才算跑通；fault 非零退出（规格第 9 条）。
    return 0 if result.get("terminal") in {"converged", "capped", "partial"} else 1


def _research_serve(args: argparse.Namespace) -> int:
    """Serve the research MCP surface on loopback. It is its own service."""
    from fleet_graph.research_mcp import serve

    try:
        serve(host=args.host, port=args.port, wiki_root=args.wiki_root)
    except RuntimeError as exc:
        # A startup refusal (root unbound, port taken) is a visible failure,
        # not a crash loop: print the clear reason and exit non-zero.
        print(f"fleet-graph research serve: {exc}", file=sys.stderr)
        return 1
    return 0


def _line_run(args: argparse.Namespace) -> int:
    """Run one ronin line to termination, printing its terminal record."""
    import pathlib

    from fleet_graph.acceptance import AcceptanceSpec
    from fleet_graph.graphs.runner import LineConfig, run_line
    from fleet_graph.state.work_folder import (
        WorkFolderBroken,
        WorkFolderError,
        resume_verification,
    )

    acceptance = None
    if args.acceptance_json:
        try:
            acceptance = AcceptanceSpec.from_cli_json(args.acceptance_json)
        except (ValueError, TypeError) as exc:
            # A declaration we cannot read is an operator error worth stopping
            # on, not something to silently degrade to "not declared".
            raise SystemExit(f"--acceptance-json is not a valid declaration: {exc}") from exc

    # M5: the revival envelope arrives as one JSON argument from the scheduler's
    # launcher. An unreadable envelope is an operator error worth stopping on.
    revival = None
    if args.revival:
        try:
            revival = json.loads(args.revival)
        except (ValueError, TypeError) as exc:
            raise SystemExit(f"--revival is not a valid JSON object: {exc}") from exc
        if not isinstance(revival, dict):
            raise SystemExit("--revival must be a JSON object")

    # E4a: the orchestration layer runs wf_resume at generation start and
    # injects the mechanical result into every coordinator round. A BROKEN
    # folder stops the line (the house rule); a transport/protocol failure is
    # a missing fact, not a reason to kill the line -- the field is then
    # simply absent and the N7 guard defaults conservatively.
    resume_verification_facts = None
    try:
        resume_verification_facts = resume_verification(args.folder)
    except WorkFolderBroken as exc:
        raise SystemExit(f"line start refused: {exc}") from exc
    except WorkFolderError:
        resume_verification_facts = None

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
        acceptance=acceptance,
        resume_verification=resume_verification_facts,
        board_card_entity_id=args.board_card or "",
        # M5: the revival envelope threaded through from the scheduler's launcher
        # (`--revival`, one JSON argument) for a line whose `done` terminal a
        # valid revoke overturned. An unreadable envelope is an operator error
        # worth stopping on, not something to silently drop.
        revival=revival,
    )
    result = run_line(config, run_id=args.run_id)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    # A line that ended `done` is not the same as a line that ended well; the
    # exit code reports termination, and acceptance stays a human's job.
    return 0 if result.get("terminal") in {"done", "blocked", "bounds"} else 1


def perform_set_seat(
    *,
    folder_id: str,
    to_seat: str,
    reason: str,
    who: str,
    lines_config: pathlib.Path,
    run_root: pathlib.Path | None = None,
    prober: Any = None,
    probe_enabled: bool = True,
    clock: Any = time.time,
) -> dict[str, Any]:
    """The set-seat operation, as a plain function so tests can drive it.

    Step 7's core: probe the target seat (C4 precheck, probe healthy before
    switching), write a C1-complete override to the scheduler's persistent
    surface, and bump the persisted generation so the next scheduler launch is
    a fresh thread cold-starting on the override seat.

    Refusals are operator errors (not in roster, missing reason, no-op switch,
    probe not healthy) and raise ``SystemExit`` -- the same shape the rest of
    the CLI uses for a command that cannot do what it was told. A no-op switch
    (already on that seat) is refused before any probe: there is nothing to
    switch, and manufacturing an override would only create audit noise.
    """
    from fleet_graph.scheduler.daemon import (
        SchedulerConfig,
        bump_line_generation,
    )
    from fleet_graph.scheduler.seat_override import SeatOverrideStore, validate_override
    from fleet_graph.state.run_artifacts import iso

    if not folder_id:
        raise SystemExit("set-seat needs a folder_id")
    if not reason:
        raise SystemExit("set-seat needs --reason: a seat switch without a reason is not auditable")
    if not who:
        raise SystemExit("set-seat needs --who: a seat switch without an operator is not auditable")

    config = SchedulerConfig.from_json(pathlib.Path(lines_config))
    line = next((entry for entry in config.lines if entry.folder_id == folder_id), None)
    if line is None:
        raise SystemExit(
            f"set-seat refused: {folder_id} is not in the roster at {lines_config}; "
            "a seat switch needs a roster line to name the 'from' seat"
        )

    store = SeatOverrideStore(run_root or config.run_root)
    current_override = store.get(folder_id)
    from_seat = current_override.to if current_override is not None else line.seat
    if from_seat == to_seat:
        raise SystemExit(
            f"set-seat refused: {folder_id} already runs on seat {to_seat!r} "
            "(roster seat when no override is in effect); a no-op switch changes nothing"
        )

    # C4: probe the face the target seat depends on *before* switching. A seat
    # that cannot be probed (no credential, unregistered) reads as "we don't
    # know", and not knowing is a refusal -- the switch may be perfectly fine
    # and we simply cannot ask, but switching blind is not the spec.
    if probe_enabled:
        if prober is None:
            from fleet_graph.scheduler.probe import CliGatewayProber

            prober = CliGatewayProber()
        try:
            healthy = bool(prober.check(to_seat))
        except Exception as exc:
            raise SystemExit(
                f"set-seat refused: gateway probe for seat {to_seat!r} could not be run: {exc}"
            ) from exc
        if not healthy:
            raise SystemExit(
                f"set-seat refused: gateway probe red for seat {to_seat!r}; "
                "probe healthy before switching (C4 precheck)"
            )

    when = iso(clock())
    record = validate_override(
        {
            "folder_id": folder_id,
            "who": who,
            "when": when,
            "from": from_seat,
            "to": to_seat,
            "reason": reason,
        }
    )
    store.write(record)
    next_generation = bump_line_generation(run_root or config.run_root, folder_id, line.generation)
    return {
        **record.as_dict(),
        "generation": next_generation,
        "run_root": str(run_root or config.run_root),
    }


def _line_set_seat(args: argparse.Namespace) -> int:
    """Switch one goal line's runtime seat, audited, via the override surface."""
    who = args.who or os.environ.get("USER") or "operator"
    result = perform_set_seat(
        folder_id=args.folder,
        to_seat=args.seat,
        reason=args.reason,
        who=who,
        lines_config=pathlib.Path(args.lines_config),
        run_root=pathlib.Path(args.run_root) if args.run_root else None,
        prober=None,
        probe_enabled=not args.no_probe,
    )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    print(
        f"set-seat: {result['folder_id']} {result['from']} -> {result['to']} "
        f"(next launch as generation {result['generation']})",
        file=sys.stderr,
    )
    return 0


def perform_line_revive(
    *,
    folder_id: str,
    who: str,
    basis: str,
    lines_config: pathlib.Path,
    run_root: pathlib.Path | None = None,
    generation: int | None = None,
    run_id: str | None = None,
    reason: str | None = None,
    checkpoints: Any = None,
    clock: Any = time.time,
) -> dict[str, Any]:
    """The line-revive operation, as a plain function so tests can drive it.

    M5's first-class revival entry. Two gates, both mandatory, before anything
    is written:

    1. **C1 precheck** -- the target line's current checkpoint-authoritative
       terminal must really be ``done``, and the given ``--generation`` (or
       ``--run-id``) must match that checkpoint record. Any mismatch is a
       refusal (`refused: target not terminal_done` / `refused: generation
       mismatch`) and nothing is written or bumped.
    2. **C1 write** -- the revoke record must carry who/basis/generation/when;
       ``validate_revive`` refuses a record missing any of them before it
       reaches disk.

    Only after both pass is the revoke record written and the persisted
    generation bumped, so the next scheduler launch cold-starts on a fresh
    thread (the old `done` thread is spent -- see daemon.py).
    """
    from fleet_graph.scheduler.checkpoint_terminal import SqliteCheckpointTerminalReader
    from fleet_graph.scheduler.daemon import (
        SchedulerConfig,
        bump_line_generation,
        stalled_generation,
    )
    from fleet_graph.scheduler.revive import ReviveStore, validate_revive
    from fleet_graph.state.run_artifacts import iso

    if not folder_id:
        raise SystemExit("line revive needs a folder_id")
    if not who:
        raise SystemExit("line revive needs --who: a revoke without an operator is not auditable")
    if not basis:
        raise SystemExit(
            "line revive needs --basis: a revoke without a mechanical reference "
            "(goal.md ruling id / board decision id / message reference) is not auditable"
        )
    if generation is None and run_id is None:
        raise SystemExit(
            "line revive needs --generation or --run-id: a revoke must name the "
            "generation (or run id) of the `done` terminal it overturns"
        )

    config = SchedulerConfig.from_json(pathlib.Path(lines_config))
    line = next((entry for entry in config.lines if entry.folder_id == folder_id), None)
    if line is None:
        raise SystemExit(
            f"line revive refused: {folder_id} is not in the roster at {lines_config}; "
            "a revoke needs a roster line to name the generation base"
        )

    effective_run_root = pathlib.Path(run_root) if run_root is not None else config.run_root
    reader = checkpoints or SqliteCheckpointTerminalReader(effective_run_root)
    current_generation = stalled_generation(effective_run_root, folder_id, line.generation)

    # C1 precheck: the current checkpoint-authoritative terminal must be `done`,
    # and the recorded generation must match where that `done` lives. Walk the
    # same (current, previous) pair the daemon reads, so the CLI and the daemon
    # can never disagree about which terminal a revoke refers to.
    done_generation: int | None = None
    done_record: dict[str, Any] | None = None
    for candidate in (current_generation, current_generation - 1):
        if candidate < 1:
            continue
        reading = reader.read(folder_id, candidate)
        if reading.fault is not None:
            break
        if reading.authoritative:
            record = reading.record
            if record is not None and record.get("terminal") == "done":
                done_generation = candidate
                done_record = record
            break
    if done_generation is None:
        raise SystemExit(
            f"line revive refused: target not terminal_done: {folder_id} is not 'done' "
            "in its current checkpoint (revival is only legal against a done terminal)"
        )
    if generation is not None and generation != done_generation:
        raise SystemExit(
            f"line revive refused: generation mismatch: --generation {generation} does not "
            f"match the checkpoint's done terminal at generation {done_generation}"
        )
    if run_id is not None and done_record is not None and done_record.get("run_id") != run_id:
        raise SystemExit(
            f"line revive refused: generation mismatch: --run-id {run_id!r} does not match "
            f"the checkpoint's done terminal run_id {done_record.get('run_id')!r}"
        )

    when = iso(clock())
    record = validate_revive(
        {
            "folder_id": folder_id,
            "who": who,
            "basis": basis,
            "generation": done_generation,
            "when": when,
            "reason": reason or "",
        }
    )
    ReviveStore(effective_run_root).write(record)
    next_generation = bump_line_generation(effective_run_root, folder_id, line.generation)
    return {
        **record.as_dict(),
        "next_generation": next_generation,
        "run_root": str(effective_run_root),
    }


def _line_revive(args: argparse.Namespace) -> int:
    """Revive one done goal line, audited, via the revoke surface."""
    who = args.who or os.environ.get("USER") or "operator"
    result = perform_line_revive(
        folder_id=args.folder,
        who=who,
        basis=args.basis,
        generation=args.generation,
        run_id=args.run_id,
        reason=args.reason,
        lines_config=pathlib.Path(args.lines_config),
        run_root=pathlib.Path(args.run_root) if args.run_root else None,
    )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    print(
        f"line revive: {result['folder_id']} revived by {result['who']} on basis "
        f"{result['basis']!r} (next launch as generation {result['next_generation']})",
        file=sys.stderr,
    )
    return 0


def _line_overrides(args: argparse.Namespace) -> int:
    """The C3 reconcile/lint surface: fold converged overrides, list the drift.

    Zero drift exits 0 (clean); any override still differing from the roster
    exits 1 and prints every drift line with its diff facts -- drift is loud,
    never silent. The fold itself is C2: an override that came to agree with
    the roster (the roster PR merged and deployed) is cleared automatically.
    """
    from fleet_graph.scheduler.daemon import SchedulerConfig
    from fleet_graph.scheduler.seat_override import (
        SeatOverrideStore,
        render_drift_line,
        roster_seat_from,
    )

    config = SchedulerConfig.from_json(pathlib.Path(args.lines_config))
    store = SeatOverrideStore(pathlib.Path(args.run_root) if args.run_root else config.run_root)
    result = store.reconcile(roster_seat_from(config))

    drift = [
        {
            "folder_id": folder_id,
            "who": override.who,
            "when": override.when,
            "from": override.from_seat,
            "to": override.to,
            "reason": override.reason,
            "roster_seat": roster or None,
        }
        for folder_id, override, roster in result.drifting
    ]
    if args.json:
        json.dump(
            {"cleared": [o.as_dict() for o in result.cleared], "drift": drift},
            sys.stdout,
            ensure_ascii=False,
            indent=1,
        )
        sys.stdout.write("\n")
    else:
        for override in result.cleared:
            print(
                f"seat override cleared (converged with roster): {override.folder_id} "
                f"{override.from_to}",
                file=sys.stderr,
            )
        if result.drifting:
            print("seat override drift (roster ≠ effective):", file=sys.stderr)
            for folder_id, override, roster in result.drifting:
                print("  " + render_drift_line(folder_id, override, roster), file=sys.stderr)
        else:
            print("no seat override drift; roster and effective seats agree")
    return 1 if result.drifting else 0


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


def _env_pairs(pairs: list[str]) -> dict[str, str]:
    """KEY=VALUE flags into a dict; a pair with no '=' is a refusal, not a guess."""
    env: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise SystemExit(f"--accept-env wants KEY=VALUE, got {pair!r}")
        env[key] = value
    return env


def _stage_timeouts(pairs: list[str]) -> dict[str, int]:
    """STAGE=SECONDS flags into a dict of positive whole seconds.

    The control plane already validated the values at admission; this side
    only has to turn the transport's strings back into the integers the
    runner's `DevelopmentConfig.timeouts` expects. A value that is not a
    positive integer is an operator error and a refusal, never a silent guess.
    """
    timeouts: dict[str, int] = {}
    for pair in pairs:
        stage, separator, raw = pair.partition("=")
        if not separator or not stage:
            raise SystemExit(f"--stage-timeout wants STAGE=SECONDS, got {pair!r}")
        try:
            seconds = int(raw)
        except ValueError as exc:
            raise SystemExit(
                f"--stage-timeout {pair!r} is not an integer number of seconds"
            ) from exc
        if seconds <= 0:
            raise SystemExit(f"--stage-timeout {pair!r} must be a positive number of seconds")
        timeouts[stage] = seconds
    return timeouts


#: env fallback for the fixed per-order management execution cost, mirroring
#: how ``FLEET_GRAPH_COST_OBS_DIR`` wires the exposition directory.
MANAGEMENT_COST_ENV = "FLEET_GRAPH_MANAGEMENT_COST"


def _management_cost(args: argparse.Namespace) -> Any | None:
    """The ``(order_id) -> float`` management cost, or None when unmeasured.

    The flag wins; the environment variable is the fallback a launched unit
    inherits. An unparsable value is a refusal, not a silent zero -- manager
    spend that is not measured must stay absent rather than faked as 0.
    """
    raw = args.management_cost
    source = "--management-cost"
    if raw is None:
        raw = os.environ.get(MANAGEMENT_COST_ENV)
        source = MANAGEMENT_COST_ENV
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{source} is not a number: {raw!r}") from exc
    return lambda _order_id: value


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
    management_cost = _management_cost(args)
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
        # shlex, not str.split: a quoted argument in an acceptance command
        # must survive the round-trip through the launcher's shlex.join.
        run_config={
            "acceptance_commands": [shlex.split(c) for c in args.accept],
            "setup_commands": [shlex.split(c) for c in args.setup],
            "acceptance_env": _env_pairs(args.accept_env),
        },
        models=dict(pair.split("=", 1) for pair in args.stage_model),
        # The per-stage run fence, forwarded verbatim from the admission record
        # (`--stage-timeout implement=7200`). Values are whole seconds; the
        # control plane validated them at create time, so a malformed one here
        # is an operator error worth stopping on.
        timeouts=_stage_timeouts(args.stage_timeout),
        publish_merge=args.publish_merge,
        cost_obs_dir=args.cost_obs_dir or "",
        management_cost=management_cost,
        # The bounded principal that dispatched this development (a line folder
        # or a human subject), threaded to the stage run labels as
        # `dispatched_by`. Absent, the actor falls back to the dispatcher.
        dispatched_by=args.dispatched_by,
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


def _dd_serve(args: argparse.Namespace) -> int:
    """Serve the dev-dispatch MCP surface on loopback. It is the control plane."""
    from fleet_graph.dd.service import serve

    serve(
        host=args.host,
        port=args.port,
        root=args.root,
        plugin_binding=args.plugin_binding,
        working_directory=args.working_directory,
        executable=args.executable,
        stage_models=dict(pair.split("=", 1) for pair in args.stage_model),
        auto_resume=args.auto_resume,
        auto_resume_interval=args.auto_resume_interval,
        work_folder_root=args.work_folder_root,
    )
    return 0


def _goal_serve(args: argparse.Namespace) -> int:
    """Serve the goal-driven MCP surface on loopback. It is its own service."""
    from fleet_graph.goal.service import serve

    try:
        serve(
            host=args.host,
            port=args.port,
            work_folder_root=args.work_folder_root,
            goal_queue_home=args.goal_queue_home,
        )
    except RuntimeError as exc:
        # A startup refusal (root unbound, port taken) is a visible failure,
        # not a crash loop: print the clear reason and exit non-zero.
        print(f"fleet-graph goal serve: {exc}", file=sys.stderr)
        return 1
    return 0


def _decision_serve(args: argparse.Namespace) -> int:
    """Serve the decision MCP surface on loopback. It is its own service.

    The synchronous decision-delivery surface: one call proves the verdict was
    delivered and consumed by the parked owner, or returns an explicit refusal
    (line not parked / no such waiting party / invalid payload) -- never a
    silent swallow after HTTP 200.
    """
    from fleet_graph.decision_mcp import serve

    try:
        serve(
            host=args.host,
            port=args.port,
            run_root=args.run_root,
            lines_config=args.lines_config,
            state_dir=args.state_dir,
            dd_root=args.dd_root,
        )
    except RuntimeError as exc:
        # A startup refusal (port taken, state dir unusable) is a visible
        # failure, not a crash loop: print the clear reason and exit non-zero.
        print(f"fleet-graph decision serve: {exc}", file=sys.stderr)
        return 1
    return 0


def _state_serve(args: argparse.Namespace) -> int:
    """Serve the M1 fleet-state read-model on loopback. Read-only."""
    from fleet_graph.state.fleet_state import DEFAULT_ENROLL_QUEUE, FleetStateConfig, serve

    serve(
        FleetStateConfig(
            host=args.host,
            port=args.port,
            run_root=pathlib.Path(args.run_root),
            dd_root=pathlib.Path(args.dd_root),
            lines_config=pathlib.Path(args.lines_config),
            bridge_state_dir=pathlib.Path(args.bridge_state_dir),
            bus_url=args.bus_url,
            enroll_queue_path=(
                pathlib.Path(args.enroll_queue) if args.enroll_queue else DEFAULT_ENROLL_QUEUE
            ),
        )
    )
    return 0


def _scheduler_run(args: argparse.Namespace) -> int:
    """Run the resident scheduler: look at each line, ask, start or record why not."""
    import pathlib

    from fleet_graph.scheduler.checkpoint_terminal import SqliteCheckpointTerminalReader
    from fleet_graph.scheduler.daemon import Scheduler, SchedulerConfig
    from fleet_graph.scheduler.launcher import TransientLauncher
    from fleet_graph.scheduler.probe import CliGatewayProber, GatewayProber, HttpxProbeTransport
    from fleet_graph.scheduler.wake import LiveWakeSignals

    config = SchedulerConfig.from_json(pathlib.Path(args.config))

    # R3 step 2 canary switch: same check(seat) contract either way, so the
    # Scheduler injection point is unchanged and rollback is config + restart.
    if args.no_probe:
        prober = None
    elif config.probe_via_runtime:
        prober = CliGatewayProber()
    else:
        prober = GatewayProber(HttpxProbeTransport())

    # The board question on parking is best-effort: a scheduler without a bus
    # credential still schedules, it just cannot escalate. Constructing the
    # client is what needs the token, so that is what the try guards.
    board = None
    try:
        from fleet_graph.bus.board import Board
        from fleet_graph.bus.client import BusClient

        board = Board(BusClient())
    except Exception:  # escalation is optional, scheduling is not
        board = None

    scheduler = Scheduler(
        config,
        prober=prober,
        launcher=TransientLauncher(dry_run=args.dry_run),
        observe=lambda result: print(json.dumps(result.as_dict(), ensure_ascii=False), flush=True),
        wake=LiveWakeSignals(),
        board=board,
        # E3: normal terminal/account/parking decisions come from the line's
        # durable checkpoint (get_state); terminal.json is the derived view.
        checkpoints=SqliteCheckpointTerminalReader(config.run_root),
    )

    # R4-2: the supervisor event observer rides this scheduler's tick. Config
    # gated (a reviewed rollout switch, like probe_via_runtime); a missing bus
    # credential degrades E1 to nothing but leaves E2-E4 observing.
    if config.supervisor_events:
        from fleet_graph.scheduler.daemon import SystemdUnitProbe
        from fleet_graph.scheduler.supervisor_events import (
            ObserverConfig,
            SupervisorObserver,
            observer_environment,
        )

        # The same env lines get: PATH plus the reviewed extras (bus token
        # file among them), so the short-run supervisor process can publish
        # its evidence note. The decision credential rides separately and
        # only here: it comes from the daemon's own environment, never from
        # the config's line_environment -- putting it there would hand every
        # line pump the key the fourth gate exists to keep away from lines
        # (agent children are scrubbed either way, but a pump process has no
        # business holding it at all).
        supervisor_environment = observer_environment(scheduler.line_environment(), os.environ)

        scheduler.supervisor = SupervisorObserver(
            ObserverConfig(
                run_root=config.run_root,
                cap_window_seconds=config.cap_window_seconds,
                read_model_base_url=config.read_model_base_url,
                heartbeat_stale_threshold_seconds=config.heartbeat_stale_threshold_seconds,
                environment=supervisor_environment,
                # M3 E5 harvest: 纯配置透传，无业务逻辑。
                harvest_allowlist_path=config.harvest_allowlist_path,
                harvest_default_branch=config.harvest_default_branch,
                harvest_deploy=config.harvest_deploy,
                repo=config.repo,
                # M4 E7: 纯配置透传，无业务逻辑。
                e7_allowlist_path=config.e7_allowlist_path,
            ),
            launcher=TransientLauncher(dry_run=args.dry_run),
            bus=board.client if board is not None else None,
            units=SystemdUnitProbe(),
            observe=lambda record: print(json.dumps(record, ensure_ascii=False), flush=True),
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
        # Engine selection is a fact on disk, not a flag to remember: a
        # development the new control plane admitted has a record under its
        # root, and its evidence is assembled in-process. Everything else is
        # a legacy development and goes to the old controller, GETs only.
        dd_root = pathlib.Path(args.dd_root)
        if (dd_root / args.target / "record.json").is_file():
            from fleet_graph.dd.control_plane import DdControlPlane
            from fleet_graph.supervise.audit import GraphEngineSource

            engine: Any = GraphEngineSource(DdControlPlane(root=dd_root))
        else:
            engine = OldEngineClient(args.engine_url)
        report = audit_development(
            args.target,
            engine=engine,
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
        report.evidence_note_id = result.message_id
    else:
        # The ref contract needs a card to hang the note on; without one the
        # honest move is a local report plus a named gap, not a guessed ref.
        report.gaps.append("无 --card 可挂 evidence note（老引擎 gate 不产板卡）；报告仅本地输出")

    if args.json:
        json.dump(report.as_dict(), sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        print(render_note(report))
        if report.evidence_note_id:
            print(f"evidence note 已落板: {report.evidence_note_id}")
    return 0 if report.ok else 1


def _supervisor_run(args: argparse.Namespace) -> int:
    """One supervisor turn: event in, audit report out. Publishes no decision."""
    import pathlib

    from fleet_graph.graphs.supervisor import SupervisorRunConfig, run_supervisor
    from fleet_graph.supervise.events import SupervisorEventError, validate_event

    try:
        event = validate_event(json.loads(args.event_json))
    except (ValueError, SupervisorEventError) as exc:
        # A refusal, not a crash: unknown event names are rejected out loud,
        # never mapped onto a neighbour.
        raise SystemExit(f"--event-json is not a valid supervisor event: {exc}") from exc

    bus = None
    if not args.no_note:
        try:
            from fleet_graph.bus.client import BusClient

            bus = BusClient(base_url=args.bus_url)
        except Exception:
            # No credential -> the act node records the degradation and the
            # report still lands in the supervisor's own run root.
            bus = None

    # M4 wiki 人话账 (交付 B)：`--wiki` 可选 enable 开关。off（默认）-> wiki=None
    # 零回归（E5/E6/E7 的 deps.wiki 保持 None）；on -> 构造 DefaultWikiClient()
    # （katana-wiki-mcp :8113）注入 E5/E6/E7 三路 config.wiki。
    wiki = None
    if args.wiki:
        from fleet_graph.supervise.wiki_report import DefaultWikiClient

        wiki = DefaultWikiClient()

    config = SupervisorRunConfig(
        event=event.as_dict(),
        state_root=pathlib.Path(args.state_root),
        run_root=pathlib.Path(args.run_root),
        checkpoint_path=args.checkpoint,
        agent_run_bin=args.agent_run_bin,
        audit_timeout_seconds=args.audit_timeout,
        engine_url=args.engine_url,
        repo=pathlib.Path(args.repo).resolve() if args.repo else None,
        dd_root=pathlib.Path(args.dd_root),
        publish_notes=not args.no_note,
        bus=bus,
        # R4-3: the decision publisher builds its own client against this URL,
        # with its own credential -- the board client above is never reused.
        bus_url=args.bus_url,
        # M3 harvest (E5): allowlist + target branch + deploy command. The
        # allowlist is deny-all when no file is given -- E5 then refuses every
        # write and records the refusal, which is the M3 first delivery.
        harvest_allowlist_path=args.harvest_allowlist,
        harvest_default_branch=args.harvest_default_branch,
        harvest_deploy_command=args.harvest_deploy,
        harvest_verify_argv=args.harvest_verify,
        harvest_verify_real_argv=args.harvest_verify_real,
        # M4 E7: goal.md 直写目标线白名单（deny-all 默认）。
        e7_allowlist_path=args.e7_allowlist,
        # M4 wiki 人话账 (交付 B)：None 或 DefaultWikiClient()。
        wiki=wiki,
    )
    result = run_supervisor(config)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    # Reaching a receipt is this process doing its job; the classification is
    # the report's content, not this process's exit status.
    return 0 if result.get("receipt_path") else 1


def _supervisor_reset(args: argparse.Namespace) -> int:
    """Reset one event key's supervisor state so the observer re-fires it.

    Idempotent; touches only the supervisor's own state surface (receipt +
    cursor). The checkpoint db is untouched on purpose: re-runs are new
    attempts and therefore fresh threads."""
    import pathlib

    from fleet_graph.scheduler.supervisor_events import reset_supervisor_event

    cursor_path = (
        pathlib.Path(args.cursor)
        if args.cursor
        else pathlib.Path(args.run_root) / ".scheduler" / "supervisor-cursor.json"
    )

    bus = None
    if args.board_seq is None and args.key.startswith("e1-"):
        try:
            from fleet_graph.bus.client import BusClient

            bus = BusClient(base_url=args.bus_url)
        except Exception:
            # No credential -> the summary records the degradation and points
            # at --board-seq; resetting receipt + attempts still proceeds.
            bus = None

    summary = reset_supervisor_event(
        args.key,
        state_root=pathlib.Path(args.state_root),
        cursor_path=cursor_path,
        board_seq=args.board_seq,
        bus=bus,
    )
    summary["daemon"] = (
        "fleet-graphd reloads the cursor file at the start of every tick -- no "
        "restart required; only a reset racing an in-flight tick can be "
        "overwritten once (re-run this command, or restart to be certain)"
    )
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    return 0


def _decision_bridge_run(args: argparse.Namespace) -> int:
    """The resident decision bridge: read verdicts, resolve, recover, seal.

    Read-only against the bus (``GET .../messages`` only); the recovery call
    goes through the owner's controlled entry (dd gate resume, a registered
    line entry, or an HTTP owner for the isolated drill). One JSON line per
    cycle on stdout.
    """
    from fleet_graph.decision_bridge.bridge import DecisionBridge, DecisionBridgeConfig
    from fleet_graph.decision_bridge.owners import HttpOwnerSource

    owner_source = None
    if args.owner_url:
        owner_source = HttpOwnerSource(args.owner_url)

    line_owners, line_run_root = _load_line_roster(args.lines_config)

    bridge = DecisionBridge(
        DecisionBridgeConfig(
            state_dir=pathlib.Path(args.state_dir),
            poll_interval_seconds=args.poll_interval,
            board_page_limit=args.page_limit,
            owner_url=args.owner_url,
            dd_root=pathlib.Path(args.dd_root),
            line_owners=line_owners,
            line_run_root=line_run_root,
            kill_window_file=pathlib.Path(args.kill_window_file) if args.kill_window_file else None,
            kill_window_seconds=args.kill_window_seconds,
        ),
        bus=_build_bridge_bus(args),
        owner_source=owner_source,
    )
    bridge.run_forever(
        observe=lambda record: print(
            json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True
        ),
        ticks=args.ticks,
    )
    return 0


def _build_bridge_bus(args: argparse.Namespace):
    """The read-only bus client for the bridge. None on missing credential --
    observation is optional, recovery discipline is not."""
    try:
        from fleet_graph.bus.client import BusClient

        return BusClient(base_url=args.bus_url)
    except Exception:
        return None


def _load_line_roster(lines_config: str | None) -> tuple[list[object], pathlib.Path]:
    """Read the goal-line roster, fail-soft on an unreadable or malformed file.

    The roster is the bridge's only route to the registered line recovery entry
    (spec item 4). A missing or malformed roster must be a *recorded
    degradation*, not a startup crash: the same posture the bridge already takes
    for a missing env file or a missing bus token. On failure the bridge still
    starts and recovers dd developments, and the preserved 60s poller keeps
    covering lines until the roster is fixed.
    """
    line_run_root = pathlib.Path("/data/fleet-graph/runs")
    if not lines_config:
        return [], line_run_root
    try:
        with open(lines_config, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        print(
            f"decision-bridge: lines roster unreadable at {lines_config}: {exc}; "
            "line recovery stays offline (dd recovery and the 60s poller are unaffected)",
            file=sys.stderr,
        )
        return [], line_run_root
    if not isinstance(raw, dict):
        print(
            f"decision-bridge: lines roster at {lines_config} is not a JSON object; "
            "line recovery stays offline",
            file=sys.stderr,
        )
        return [], line_run_root
    entries = raw.get("lines")
    line_owners: list[object] = []
    if isinstance(entries, list):
        line_owners = [
            {k: v for k, v in entry.items() if not str(k).startswith("_")}
            if isinstance(entry, dict)
            else entry
            for entry in entries
        ]
    if raw.get("run_root"):
        line_run_root = pathlib.Path(str(raw["run_root"]))
    return line_owners, line_run_root


def _decision_bridge_status(args: argparse.Namespace) -> int:
    """Dump the bridge's durable state: cursor plus one receipt per source."""
    import pathlib

    from fleet_graph.decision_bridge.store import BridgeStore, BridgeStoreError

    try:
        store = BridgeStore(pathlib.Path(args.state_dir)).open()
        payload = {"cursor": store.cursor(), "receipts": store.receipts()}
        store.close()
    except BridgeStoreError as exc:
        print(f"decision-bridge store unusable: {exc}", file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    return 0


def _goal_interrupt_run(args: argparse.Namespace) -> int:
    """The resident E2 goal-interrupt bridge: recover suspended lines.

    Read-only against the bus. For every still-suspended question it queries the
    authoritative decision chain and drives a validated ``DecisionInput`` back
    through the same ``resume_key`` that suspended the line. One JSON line per
    cycle on stdout. This is the production side of the E2 interrupt that
    ``build_line`` opens up in the line process itself: together they make the
    in-graph decision interrupt the real path for a human-decision wait.
    """
    import pathlib

    from fleet_graph.goal_interrupt.bridge import GoalInterruptBridge, GoalInterruptBridgeConfig
    from fleet_graph.goal_interrupt.store import GoalInterruptStore
    from fleet_graph.graphs.runner import LineConfig, resume_goal_line

    lines, line_run_root = _load_line_roster(args.lines_config)
    if args.run_root:
        line_run_root = pathlib.Path(args.run_root)

    def _field(line: object, key: str, default: Any = "") -> Any:
        return line.get(key, default) if isinstance(line, dict) else getattr(line, key, default)

    def resumer_for(store: GoalInterruptStore, line: object) -> Any:
        folder_id = str(_field(line, "folder_id"))
        seat = str(_field(line, "seat"))
        alias = _field(line, "alias", None)

        def resumer(decision: Any) -> str:
            record = store.interrupt(decision.resume_key)
            generation = int(record["generation"]) if record else 1
            config = LineConfig(
                folder_id=folder_id,
                seat=seat,
                run_root=line_run_root / folder_id,
                generation=generation,
                alias=alias,
            )
            _state, status = resume_goal_line(config, decision)
            return status

        return resumer

    bus = _build_bridge_bus(args)
    bridges: list[GoalInterruptBridge] = []
    for line in lines:
        folder_id = str(_field(line, "folder_id"))
        if not folder_id:
            continue
        store = GoalInterruptStore(line_run_root / folder_id).open()
        bridges.append(
            GoalInterruptBridge(
                GoalInterruptBridgeConfig(
                    poll_interval_seconds=args.poll_interval,
                    board_page_limit=args.page_limit,
                ),
                store=store,
                bus=bus,
                resumer=resumer_for(store, line),
            )
        )

    if not bridges:
        print("goal-interrupt: no lines in the roster; nothing to bridge", file=sys.stderr)

    remaining = args.ticks
    while remaining is None or remaining > 0:
        for bridge in bridges:
            print(json.dumps(bridge.run_once(), ensure_ascii=False, sort_keys=True), flush=True)
        if remaining is not None:
            remaining -= 1
            if remaining == 0:
                break
        if args.poll_interval > 0:
            time.sleep(args.poll_interval)
    return 0


def _arbiter_run(args: argparse.Namespace) -> int:
    """One A2 tick: reconcile identity, triage, reason, and (with --publish) post.

    Defaults to dry-run/offline: the arbiter reads the board and records what
    it would publish, but writes nothing. Publication requires the explicit
    --publish flag; this development never enables it in production.

    The managed path (--publish) first proves, read-only, against the real
    Agent Bus gateway, that the caller is the expected arbiter principal and
    that the ``arbiter`` alias resolves to it: ``GET /v1/agents/whoami`` names
    the caller, the alias read surface's ``current_agent_id`` is the
    authoritative identity, and the inbox ``agent:<current_agent_id>`` is
    derived only after both verify. A missing/mismatched/rebound/ambiguous/
    unauthorized identity is refused with a non-secret error and a non-zero
    exit before any model work or publication.
    """
    from fleet_graph.arbiter.a2 import TextReasoner, run_arbiter
    from fleet_graph.arbiter.managed_path import build_receipt
    from fleet_graph.arbiter.reconcile import (
        ARBITER_ALIAS,
        DEFAULT_EXPECTED_PRINCIPAL,
        BusPrincipalBindingProbe,
        ReconciliationError,
        reconcile_principal_alias,
    )
    from fleet_graph.bus.client import BusClient

    client = BusClient(base_url=args.bus_url)

    if args.publish:
        alias = args.alias or ARBITER_ALIAS
        expected = os.environ.get("FLEET_GRAPH_ARBITER_PRINCIPAL") or DEFAULT_EXPECTED_PRINCIPAL
        probe = BusPrincipalBindingProbe(client)
        try:
            reconcile_principal_alias(
                whoami_agent_id=probe.whoami(),
                current_agent_id=probe.alias_agent_id(alias),
                expected_principal=expected,
                alias=alias,
            )
        except ReconciliationError as exc:
            print(f"arbiter refused: {exc.detail}", file=sys.stderr)
            return 1

    reasoner = TextReasoner(model=args.model)
    result = run_arbiter(client=client, reasoner=reasoner, publish=args.publish, alias=args.alias)
    payload = {
        "dry_run": result.dry_run,
        "emitted": [message.as_dict() for message in result.emitted],
        "suppressed": result.suppressed,
        "refused": result.refused,
        "audit": result.audit().as_dict(),
        "receipt": build_receipt(result),
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    return 0


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
    # agent-run splits this budget across the role's route chain (perAttempt =
    # total / legs): with a 2-leg chain a 2700s budget kills the first leg at
    # 1350s. wf-c106b9 g1-g4 (2026-08-31) died exactly there mid-work -- the
    # coordinator was actively stepping (12 tool-calling steps) when the leg
    # budget expired, misread as provider timeout. 5400 keeps the first leg's
    # effective window at the intended 2700s.
    run.add_argument("--coordinator-timeout", type=int, default=5400)
    run.add_argument("--alias", default=None, help="agent-bus inbox alias")
    run.add_argument(
        "--board-card",
        default=None,
        help="the board card entity id the scheduler already materialised for "
        "this line; the E2 interrupt runtime reuses it instead of publishing a "
        "second card",
    )
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
    run.add_argument(
        "--acceptance-json",
        default=None,
        help="the roster's acceptance declaration as one JSON argument "
        '({"argvs": [[...]], "cwd": ..., "timeout_seconds": ...}). The trust '
        "anchor is the roster config's PR review, so its visibility in argv "
        "is acceptable; absence means the line records `not_declared`",
    )
    run.add_argument(
        "--revival",
        default=None,
        help="M5: the revival envelope as one JSON argument "
        '({"who": ..., "basis": ..., "generation": ..., "reason": ...}) '
        "threaded through from the scheduler's launcher for a line whose "
        "`done` terminal a valid revoke overturned. Absent means a normal "
        "launch with no revival fact",
    )
    run.set_defaults(func=_line_run)

    set_seat = line_sub.add_parser(
        "set-seat",
        help="switch one line's runtime seat: probe (C4), write an audited "
        "override (C1), bump the generation so the next launch cold-starts on "
        "the new seat. Never rewrites the roster.",
    )
    set_seat.add_argument("folder", help="the goal line's work folder id (wf-...)")
    set_seat.add_argument("seat", help="the seat to switch this line TO")
    set_seat.add_argument("--reason", required=True, help="why (C1: a switch must be explainable)")
    set_seat.add_argument(
        "--who",
        default=None,
        help="who is doing this (C1; defaults to $USER)",
    )
    set_seat.add_argument(
        "--lines-config",
        default="config/ronin-lines.json",
        help="the roster SSoT the 'from' seat is read from",
    )
    set_seat.add_argument(
        "--run-root",
        default=None,
        help="override where the override surface and stall-state live "
        "(default the roster's run_root)",
    )
    set_seat.add_argument(
        "--no-probe",
        action="store_true",
        help="skip the C4 gateway precheck of the target seat (drills only: "
        "a production switch without the precheck is not the spec)",
    )
    set_seat.set_defaults(func=_line_set_seat)

    revive = line_sub.add_parser(
        "revive",
        help="M5: revive one done goal line -- write a C1-complete revoke "
        "record (who/basis/generation/when) to the scheduler's persistent "
        "surface and bump the generation so the next launch cold-starts on a "
        "fresh thread. Never rewrites terminal.json, never touches the "
        "checkpoint. Refused unless the line's current checkpoint terminal is "
        "really `done` at the recorded generation.",
    )
    revive.add_argument("folder", help="the goal line's work folder id (wf-...)")
    revive.add_argument(
        "--basis",
        required=True,
        help="the mechanical reference for the revoke -- a goal.md ruling "
        "block id, a board decision id, or a message reference, never free "
        "prose (C1)",
    )
    revive.add_argument(
        "--who",
        default=None,
        help="who is overturning the terminal (C1; defaults to $USER)",
    )
    revive.add_argument(
        "--generation",
        type=int,
        default=None,
        help="the generation of the `done` terminal being overturned; must "
        "match the checkpoint record (or use --run-id instead)",
    )
    revive.add_argument(
        "--run-id",
        default=None,
        help="the run id of the `done` terminal being overturned; must match "
        "the checkpoint record (or use --generation instead)",
    )
    revive.add_argument(
        "--reason",
        default=None,
        help="optional prose; never sufficient on its own (C1 -- `basis` is "
        "the auditable reference)",
    )
    revive.add_argument(
        "--lines-config",
        default="config/ronin-lines.json",
        help="the roster SSoT the generation base is read from",
    )
    revive.add_argument(
        "--run-root",
        default=None,
        help="override where the revoke surface and stall-state live "
        "(default the roster's run_root)",
    )
    revive.set_defaults(func=_line_revive)

    overrides = line_sub.add_parser(
        "overrides",
        help="the C3 reconcile/lint face: fold overrides that converged with "
        "the roster (C2) and list every remaining roster ≠ effective drift "
        "loudly. Exits 1 while drift exists, 0 when clean.",
    )
    overrides.add_argument(
        "--lines-config",
        default="config/ronin-lines.json",
        help="the roster SSoT that defines each line's seat",
    )
    overrides.add_argument(
        "--run-root",
        default=None,
        help="override where the override surface lives (default the roster's run_root)",
    )
    overrides.add_argument("--json", action="store_true")
    overrides.set_defaults(func=_line_overrides)

    research = subparsers.add_parser("research", help="run a deep-research ticket")
    research_sub = research.add_subparsers()
    research_run = research_sub.add_parser("run", help="run one research ticket to termination")
    research_run.add_argument("--question", required=True, help="the research question")
    research_run.add_argument(
        "--run-root",
        default=None,
        help="run root; defaults to /data/fleet-graph/research/<research_id>",
    )
    research_run.add_argument(
        "--tier",
        default=None,
        choices=("light", "heavy"),
        help="light/heavy tier (R6 unified routing); defaults to a deterministic "
        "scale-based routing (heavy when the sources scale >= 4, else light)",
    )
    research_run.add_argument(
        "--scale",
        type=int,
        default=None,
        help="scale input for the deterministic tier routing when --tier is absent "
        "(defaults to the number of configured sources)",
    )
    research_run.add_argument(
        "--wiki-root",
        default=None,
        help="wiki-domain root the final report is placed under (DeepThought/<topic>/); "
        "defaults to $FLEET_GRAPH_WIKI_ROOT or /data/vault",
    )
    research_run.add_argument(
        "--generation",
        type=int,
        default=1,
        help="stable thread identity is research_id:g{generation}; a restart of the "
        "same generation resumes its checkpoint and re-adopts in-flight runs",
    )
    research_run.add_argument(
        "--max-clues",
        type=int,
        default=None,
        help="clue board size bound override (R6 tier bounds by default; "
        "hitting the bound terminates the run as `capped`)",
    )
    research_run.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="how many open clues one dispatch wave launches in parallel (R3 fan-out; "
        "R6 tier bounds by default); only affects how many run per wave, never "
        "clue/run id derivation",
    )
    research_run.add_argument(
        "--instance",
        default=None,
        help="explicit run instance (R3-fix); defaults to a stable content-address of "
        "the run root, so different run roots of the same question stay isolated and "
        "never collide on the bus 409. Keep it stable across kill-restart",
    )
    research_run.add_argument(
        "--checkpoint",
        default=None,
        help="checkpoint sqlite path; defaults to <run-root>/checkpoint.sqlite3",
    )
    research_run.set_defaults(func=_research_run)

    research_serve = research_sub.add_parser(
        "serve", help="serve the research MCP surface (the standalone research service)"
    )
    research_serve.add_argument("--host", default="127.0.0.1")
    research_serve.add_argument("--port", type=int, default=5612)
    research_serve.add_argument(
        "--wiki-root",
        default=None,
        help="wiki-domain root for DeepThought/<topic>/ report placement; required -- "
        "without it (or without FLEET_GRAPH_WIKI_ROOT) the service refuses to start",
    )
    research_serve.set_defaults(func=_research_serve)

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
    dd_run.add_argument(
        "--setup",
        action="append",
        default=[],
        help="a setup command run before the acceptance commands; repeatable",
    )
    dd_run.add_argument(
        "--accept-env",
        action="append",
        default=[],
        help="KEY=VALUE overlaid on setup and acceptance commands; repeatable",
    )
    dd_run.add_argument("--board-card", default=None, help="card entity id; enables the gate")
    dd_run.add_argument(
        "--dispatched-by",
        default="",
        help="bounded principal that dispatched this development (a line folder "
        "or a human subject), recorded as the `dispatched_by` label on every "
        "dd-worker run; never a run_id/uuid. Empty falls back to the dispatcher",
    )
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
        "--stage-timeout",
        action="append",
        default=[],
        metavar="STAGE=SECONDS",
        help="override one stage's run fence in whole seconds, e.g. "
        "implement=7200. Stages without an override keep the 3600s default",
    )
    dd_run.add_argument(
        "--publish-merge",
        action="store_true",
        help="push the durable ref. Off by default: it is the one step here that cannot be undone",
    )
    dd_run.add_argument(
        "--cost-obs-dir",
        default=None,
        help="node_exporter textfile directory for the cost-observability exposition; "
        "unset means the run does not collect (FLEET_GRAPH_COST_OBS_DIR is the env fallback)",
    )
    dd_run.add_argument(
        "--management-cost",
        type=float,
        default=None,
        help="fixed management execution cost per order, emitted under the "
        "management attribution (rule 1 numerator). Unset means manager spend "
        "is not measured and is accounted absent, never faked as a measured "
        "zero (FLEET_GRAPH_MANAGEMENT_COST is the env fallback)",
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

    dd_serve = dd_sub.add_parser(
        "serve", help="serve the dev-dispatch MCP surface (the in-process control plane)"
    )
    dd_serve.add_argument("--host", default="127.0.0.1")
    dd_serve.add_argument("--port", type=int, default=5610)
    dd_serve.add_argument(
        "--root", default=None, help="development state root (default /data/fleet-graph/dd)"
    )
    dd_serve.add_argument(
        "--plugin-binding",
        default=None,
        help="JSON file holding the production-pinned plugin_producer section "
        "(default /data/fleet-graph/dd/plugin-binding.json)",
    )
    dd_serve.add_argument(
        "--working-directory",
        default=None,
        help="working directory for launched dd runs (default the deployed release)",
    )
    dd_serve.add_argument(
        "--executable",
        default=None,
        help="fleet-graph executable for launched dd runs (default the deployed release)",
    )
    dd_serve.add_argument(
        "--stage-model",
        action="append",
        default=[],
        metavar="STAGE=MODEL",
        help="server-side policy: override one stage's model for every launched "
        "run (e.g. continuous_review=deepseek-v4-pro); the roles' own "
        "selectors stay the default",
    )
    dd_serve.add_argument(
        "--auto-resume",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="patrol awaiting_gate developments and resume them once their "
        "decision lands on the board (default on; env FLEET_GRAPH_DD_AUTO_RESUME)",
    )
    dd_serve.add_argument(
        "--auto-resume-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="seconds between auto-resume patrols (default 60; "
        "env FLEET_GRAPH_DD_AUTO_RESUME_INTERVAL)",
    )
    dd_serve.add_argument(
        "--work-folder-root",
        default=None,
        help="directory owning one governed work-folder repository per folder id; "
        "the concrete wf_reconcile source is bound from it "
        "(env FLEET_GRAPH_WORK_FOLDER_ROOT)",
    )
    dd_serve.set_defaults(func=_dd_serve)

    goal = subparsers.add_parser(
        "goal",
        help="the goal-driven MCP surface (goal_enroll / goal-open / briefing)",
    )
    goal_sub = goal.add_subparsers()
    goal_serve = goal_sub.add_parser(
        "serve", help="serve the goal-driven MCP surface (the standalone goal service)"
    )
    goal_serve.add_argument("--host", default="127.0.0.1")
    goal_serve.add_argument("--port", type=int, default=5611)
    goal_serve.add_argument(
        "--work-folder-root",
        default=None,
        help="directory owning one governed goal-folder repository per folder id; "
        "required -- without it (or without FLEET_GRAPH_WORK_FOLDER_ROOT) the "
        "service refuses to start (GOAL_ENROLL_SOURCE_UNBOUND family)",
    )
    goal_serve.add_argument(
        "--goal-queue-home",
        default=None,
        help="independent queue home owning enroll-queue.jsonl and "
        "enroll-rejections.jsonl; default /data/fleet-graph/goal "
        "(env FLEET_GRAPH_GOAL_QUEUE_HOME) -- separate from the work-folder-root",
    )
    goal_serve.set_defaults(func=_goal_serve)

    decision = subparsers.add_parser(
        "decision",
        help="the decision MCP surface (synchronous verdict delivery to parked lines)",
    )
    decision_sub = decision.add_subparsers()
    decision_serve = decision_sub.add_parser(
        "serve",
        help="serve the decision MCP surface (the standalone decision service)",
    )
    decision_serve.add_argument("--host", default="127.0.0.1")
    decision_serve.add_argument("--port", type=int, default=5614)
    decision_serve.add_argument(
        "--run-root",
        default=None,
        help="where the lines' stall-state lives; defaults to the roster's run_root",
    )
    decision_serve.add_argument(
        "--lines-config",
        default=None,
        help="JSON config listing the goal-line roster (a `lines` array plus an "
        "optional `run_root`). Absent/malformed degrades to 'no registered "
        "lines' (every delivery answers NO_WAITING_PARTY)",
    )
    decision_serve.add_argument(
        "--state-dir",
        default=None,
        help="where the delivery ledger and metrics textfile live; defaults to "
        "/data/fleet-graph/decision-mcp (env FLEET_GRAPH_DECISION_MCP_STATE_DIR)",
    )
    decision_serve.add_argument(
        "--dd-root",
        default=None,
        help="where the dd developments live (the awaiting_gate records the gate "
        "delivery resolves server-side); defaults to /data/fleet-graph/dd",
    )
    decision_serve.set_defaults(func=_decision_serve)

    state = subparsers.add_parser(
        "state", help="the M1 fleet-state read-model (read-only /v1 views)"
    )
    state_sub = state.add_subparsers()
    state_serve = state_sub.add_parser(
        "serve",
        help="serve the read-only fleet-state views: GET /v1/lines, GET /v1/decisions",
    )
    state_serve.add_argument("--host", default="127.0.0.1")
    state_serve.add_argument("--port", type=int, default=7494)
    state_serve.add_argument(
        "--run-root",
        default="/data/fleet-graph/runs",
        help="where the lines' heartbeat.json / terminal.json live",
    )
    state_serve.add_argument(
        "--dd-root",
        default="/data/fleet-graph/dd",
        help="dd development state root (status.json / record.json)",
    )
    state_serve.add_argument(
        "--lines-config",
        default="config/ronin-lines.json",
        help="the roster SSoT that decides which lines /v1/lines covers",
    )
    state_serve.add_argument(
        "--bridge-state-dir",
        default="/data/fleet-graph/decision-bridge",
        help="the decision-bridge durable state root (holds bridge.sqlite3)",
    )
    state_serve.add_argument(
        "--bus-url",
        default=None,
        help="optional agent-bus base URL for the published-decisions view "
        "(read-only; unset or unreadable degrades to receipts only)",
    )
    state_serve.add_argument(
        "--enroll-queue",
        default=None,
        help="the goal enrollment pending queue (enroll-queue.jsonl) the "
        "/v1/enrollments view re-reads per request; defaults to the goal "
        "service's own queue home /data/fleet-graph/goal/enroll-queue.jsonl "
        "(the same home goal serve writes by default)",
    )
    state_serve.set_defaults(func=_state_serve)

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

    from fleet_graph.arbiter.a2 import DEFAULT_REASONING_MODEL

    arbiter = subparsers.add_parser(
        "arbiter", help="A2 read-only fleet arbiter (triage and suggest, never decide)"
    )
    arbiter_sub = arbiter.add_subparsers()
    arbiter_run = arbiter_sub.add_parser(
        "run", help="one tick: triage, reason, and (with --publish) post suggestions"
    )
    arbiter_run.add_argument("--bus-url", default=DEFAULT_BUS_URL)
    arbiter_run.add_argument(
        "--alias", default=None, help="arbiter inbox alias for consultation messages"
    )
    arbiter_run.add_argument("--model", default=DEFAULT_REASONING_MODEL)
    arbiter_run.add_argument(
        "--publish",
        action="store_true",
        help="publish suggestions to the board (off by default: dry-run)",
    )
    arbiter_run.set_defaults(func=_arbiter_run)

    decision_bridge = subparsers.add_parser(
        "decision-bridge",
        help="the E1 decision event bridge (read verdicts, recover waiting owners)",
    )
    decision_bridge_sub = decision_bridge.add_subparsers()
    bridge_run = decision_bridge_sub.add_parser(
        "run", help="poll the board and recover owners, one JSON line per cycle"
    )
    bridge_run.add_argument("--bus-url", default=DEFAULT_BUS_URL)
    bridge_run.add_argument(
        "--state-dir",
        default="/data/fleet-graph/decision-bridge",
        help="the bridge's own durable state root (holds bridge.sqlite3)",
    )
    bridge_run.add_argument("--poll-interval", type=float, default=1.0)
    bridge_run.add_argument("--page-limit", type=int, default=200)
    bridge_run.add_argument(
        "--owner-url",
        default=None,
        help="recover through this HTTP owner instead of the dd control plane "
        "(the isolated drill's fake owner)",
    )
    bridge_run.add_argument("--dd-root", default="/data/fleet-graph/dd")
    bridge_run.add_argument(
        "--lines-config",
        default=None,
        help="JSON config listing the goal-line roster (a `lines` array plus an "
        "optional `run_root`). When set, the bridge also discovers and recovers "
        "parked lines through their registered control entry",
    )
    bridge_run.add_argument(
        "--kill-window-file",
        default=None,
        help="test seam: write a sentinel here and hold after the owner answers "
        "but before the terminal seal (crash-window drill)",
    )
    bridge_run.add_argument("--kill-window-seconds", type=float, default=2.0)
    bridge_run.add_argument(
        "--ticks",
        type=int,
        default=None,
        help="run this many cycles then exit (default: forever)",
    )
    bridge_run.set_defaults(func=_decision_bridge_run)

    bridge_status = decision_bridge_sub.add_parser(
        "status", help="dump the bridge's cursor and receipts as JSON"
    )
    bridge_status.add_argument("--state-dir", default="/data/fleet-graph/decision-bridge")
    bridge_status.set_defaults(func=_decision_bridge_status)

    goal_interrupt = subparsers.add_parser(
        "goal-interrupt",
        help="the E2 in-graph goal-interrupt bridge (resume suspended lines)",
    )
    goal_interrupt_sub = goal_interrupt.add_subparsers()
    goal_interrupt_run = goal_interrupt_sub.add_parser(
        "run", help="poll held interrupts and resume them, one JSON line per cycle"
    )
    goal_interrupt_run.add_argument("--bus-url", default=DEFAULT_BUS_URL)
    goal_interrupt_run.add_argument(
        "--lines-config",
        default=None,
        help="JSON config listing the goal-line roster (a `lines` array plus an "
        "optional `run_root`). Each line's interrupt store lives under "
        "<run_root>/<folder_id>/goal-interrupt.sqlite3",
    )
    goal_interrupt_run.add_argument(
        "--run-root",
        default=None,
        help="override the roster's run root (default the roster's, else /data/fleet-graph/runs)",
    )
    goal_interrupt_run.add_argument("--poll-interval", type=float, default=1.0)
    goal_interrupt_run.add_argument("--page-limit", type=int, default=200)
    goal_interrupt_run.add_argument(
        "--ticks",
        type=int,
        default=None,
        help="run this many cycles then exit (default: forever)",
    )
    goal_interrupt_run.set_defaults(func=_goal_interrupt_run)

    supervisor = subparsers.add_parser(
        "supervisor", help="the event-triggered supervisor graph (audits, no verdicts)"
    )
    supervisor_sub = supervisor.add_subparsers()
    supervisor_run = supervisor_sub.add_parser(
        "run",
        help="run one supervisor turn for one event; idempotent per event key",
    )
    supervisor_run.add_argument(
        "--event-json",
        required=True,
        help='the event as one JSON argument: {"type": ..., "key": ..., "payload": {...}}. '
        "The vocabulary is closed (board_question/blocked_decision/line_fault/cap_breaker); "
        "unknown names are refused",
    )
    supervisor_run.add_argument(
        "--state-root",
        default="/data/fleet-graph/supervisor",
        help="the supervisor's own root: checkpoint, agent runs, reports. "
        "Never a supervised line's work folder",
    )
    supervisor_run.add_argument(
        "--run-root", default="/data/fleet-graph/runs", help="where the lines' terminal.json live"
    )
    supervisor_run.add_argument(
        "--checkpoint",
        default=None,
        help="checkpoint sqlite path; defaults to <state-root>/checkpoint.sqlite3",
    )
    supervisor_run.add_argument(
        "--agent-run-bin", default=None, help="test seam: alternative agent-run binary"
    )
    supervisor_run.add_argument("--audit-timeout", type=int, default=900)
    supervisor_run.add_argument(
        "--engine-url",
        default="http://127.0.0.1:7460",
        help="legacy controller base URL for development audits; GETs only",
    )
    supervisor_run.add_argument(
        "--repo",
        default=None,
        help="explicit local clone for development audits; without it the repo "
        "resolves from the dd admission record (card head development_id -> "
        "<dd-root>/<id>/record.json -> repo_path), and a development that "
        "resolves neither reports the gap and classifies needs_human",
    )
    supervisor_run.add_argument(
        "--dd-root",
        default="/data/fleet-graph/dd",
        help="dd admission records root; E1 dd-gate events resolve their "
        "audit repo from <dd-root>/<development_id>/record.json",
    )
    supervisor_run.add_argument("--bus-url", default=DEFAULT_BUS_URL)
    supervisor_run.add_argument(
        "--no-note", action="store_true", help="publish nothing; report to the state root only"
    )
    supervisor_run.add_argument(
        "--harvest-allowlist",
        default=None,
        help="M3 harvest (E5): harvest write allowlist config file. Deny-all "
        "when unset -- E5 then refuses every write and records the refusal",
    )
    supervisor_run.add_argument(
        "--harvest-default-branch",
        default="main",
        help="M3 harvest (E5): target default branch for squash merge + ff-only pull",
    )
    supervisor_run.add_argument(
        "--harvest-deploy",
        action="append",
        default=[],
        help="M3 harvest (E5): deploy command argv; must be allowlisted to run. Repeatable",
    )
    supervisor_run.add_argument(
        "--harvest-verify",
        action="append",
        default=[],
        help="M3 harvest (E5): full-suite verify argv in the harvest worktree "
        "(defaults to 'make verify')",
    )
    supervisor_run.add_argument(
        "--harvest-verify-real",
        action="append",
        default=[],
        help="M3 harvest (E5): real-machine verify argv after deploy (defaults to 'make verify')",
    )
    supervisor_run.add_argument(
        "--e7-allowlist",
        default=None,
        help="M4 E7 (decision_swallowed): E7 goal.md 直写目标线白名单 config file. "
        "Deny-all when unset -- E7 then refuses every goal.md direct write and "
        "records the refusal",
    )
    supervisor_run.add_argument(
        "--wiki",
        action="store_true",
        help="M4 wiki 人话账 (交付 B): enable the katana-wiki-mcp client "
        "(DEFAULT_WIKI_MCP_URL) so E5/E6/E7 append achievement sections on "
        "successful closure. Off by default: deps.wiki stays None (零回归)",
    )
    supervisor_run.set_defaults(func=_supervisor_run)

    supervisor_reset = supervisor_sub.add_parser(
        "reset",
        help="reset one event key so the observer re-fires it: delete the "
        "receipt, clear the cursor's attempts counter, and (E1 only) rewind "
        "board_seq to just before the question. Idempotent; never touches the "
        "checkpoint db -- a re-run is a new attempt and thus a fresh thread. "
        "No daemon restart needed: the cursor is reloaded every tick",
    )
    supervisor_reset.add_argument("key", help="the event key, e.g. e3-<run_id> or e1-<note_id>")
    supervisor_reset.add_argument(
        "--state-root",
        default="/data/fleet-graph/supervisor",
        help="the supervisor's own root (holds reports/<key>.json)",
    )
    supervisor_reset.add_argument(
        "--run-root",
        default="/data/fleet-graph/runs",
        help="the scheduler run root; the cursor lives at "
        "<run-root>/.scheduler/supervisor-cursor.json unless --cursor is given",
    )
    supervisor_reset.add_argument(
        "--cursor", default=None, help="explicit cursor file path (overrides --run-root derivation)"
    )
    supervisor_reset.add_argument(
        "--board-seq",
        type=int,
        default=None,
        help="explicit board_seq to set (clamped: never moves the cursor "
        "forward). Without it an e1-<note_id> key is "
        "located mechanically on the bus and the cursor moves to just before "
        "that message (never forwards); when the note cannot be located "
        "(no credential, bus down, id not in the channel window) the summary "
        "says so and this flag is the fallback. E2/E3/E4 need no rewind: "
        "they re-derive from terminals/tick results every tick",
    )
    supervisor_reset.add_argument("--bus-url", default=DEFAULT_BUS_URL)
    supervisor_reset.set_defaults(func=_supervisor_reset)

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
        "--dd-root",
        default="/data/fleet-graph/dd",
        help="new-engine development root; a development with a record here "
        "is audited through the in-process control plane instead",
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
