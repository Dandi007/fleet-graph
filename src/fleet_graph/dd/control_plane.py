"""The in-process control plane behind the dev-dispatch MCP surface.

The supervision plane struck the separate graph-API tier (:5611): the MCP
service *is* the control plane. This module is what the service calls -- no
HTTP hop, no second engine. Its state model is the one the user ruled for R1
(wf-13ff9e plan.md §1 R1-a):

    state = the git ancestry chain (authoritative commits + receipts)
          + the durable checkpoint (in-flight graph state)
          + the run artifacts (events, results, launches)

There is deliberately **no database**. `status.json` under a development's
directory is a *rebuildable cache* for list/get fast paths: `rebuild_status`
recomputes it wholesale from the sources above, and a test proves the rebuilt
copy equals the cached one, so losing the file loses nothing.

**Admission is server-side derivation** (R1-b). `create` takes exactly a repo
path, a target base, and the spec -- everything else (development id, H0
handoff, root digest, bootstrap commit, durable ref, acceptance argv) is
derived here and written down where it can be independently re-derived:

- the development id is a digest over (repo, spec digest, target base), so
  `create` is idempotent by construction;
- the H0 handoff is canonical JSON whose digest seeds the receipt chain, and
  both the object and its digest are recomputable from the record;
- the target base is committed by bootstrap and read back by the run through
  `committed_target_base` (the §25 lesson: the two commands must compose on
  their defaults, with the introducing commit as the tamper anchor);
- the acceptance argv is read out of the **spec itself** (a ```dd-acceptance
  fenced block), because the spec is frozen and digest-bound at bootstrap --
  the graded party cannot edit the exam after admission.

**Starting is a transient systemd unit** (same isolation argument as
scheduler/launcher.py): the control plane can restart without killing runs in
flight. The thread identity is `{development_id}:g{generation}` (via
DevelopmentConfig), and the checkpoint lives at a path derived from the
development id, so a kill-restart re-enters the same generation's thread and
re-adopts the agent runs in flight -- while a start after a retryable
terminal (or a reconfigure) launches generation n+1 with fresh derived
identities, so a rerun never collides with its own past (R1-c; the tick-14
IDEMPOTENCY_CONFLICT lesson).

**Failure is classified into four classes under three exits** (R1-c;
`classify_failure`): environment/contract -> `reconfigure` the acceptance
context, then start a new generation; implementation -> the in-graph rework
loop, untouched here; fabrication (the UNVERIFIED_TEST_CLAIM family) ->
final, refused everywhere; rejection (human_gate REJECT, `GATE_REJECTED`) ->
a verdict, not a fault, classified independently so the supervision plane
never reads it as one.

**The gate carries no verdict.** `gate` reports the pending question note and
offers `resume`, which re-enters the suspended thread with no input at all --
the graph re-reads the board itself. Verdicts travel only as `work.decision.v1`
on the bus, published by a human; this module has no way to publish one.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_graph.dd.adoption import (
    ADOPTION_MECHANISM,
    AdoptionError,
    AdoptionLedger,
    AdoptionRecord,
    Discovery,
)
from fleet_graph.dd.bootstrap import (
    DEVELOPMENT_PATH,
    IdentityChanged,
    build_attempt_context,
    canonical_bytes,
    committed_target_base,
    digest_of,
)
from fleet_graph.dd.evidence import (
    KIND_ADOPTION,
    KIND_HUMAN_RECOVERY,
    KIND_SCOPE,
    EvidenceChain,
    EvidenceLink,
)
from fleet_graph.dd.git import run_git
from fleet_graph.dd.recovery import (
    RECOVERY_MECHANISM,
    HumanRecoveryExit,
    RecoveryDecision,
    RecoveryError,
)
from fleet_graph.dd.scope import (
    RULE_ID,
    ScopeBoundary,
    ScopeViolationError,
    default_boundary,
    evaluate_text,
    require_scope,
)
from fleet_graph.dd.upstream_constants import ATTEMPT_CONTEXT_CONTRACT_VERSION
from fleet_graph.graphs.dd_runner import EVENTS_FILE, RESULT_FILE
from fleet_graph.state.run_artifacts import iso, write_json_durable

DEFAULT_DD_ROOT = Path("/data/fleet-graph/dd")
DEFAULT_PLUGIN_BINDING = Path("/data/fleet-graph/dd/plugin-binding.json")
DEFAULT_WORKING_DIRECTORY = "/data/apps/fleet-graph/current"
DEFAULT_EXECUTABLE = "/data/apps/fleet-graph/current/.venv/bin/fleet-graph"
#: Fail-closed admission whitelist: a repo outside these roots is refused.
#: /tmp is admitted for throwaway acceptance repos (the §24 precedent).
DEFAULT_WORKTREE_ROOTS: tuple[str, ...] = ("/data/worktrees", "/tmp")

RECORD_FILE = "record.json"
STATUS_FILE = "status.json"
H0_FILE = "h0-handoff.json"
LAUNCHES_FILE = "launches.jsonl"
CHECKPOINT_FILE = "checkpoint.sqlite3"
LOG_FILE = "dd.log"
#: B2 append-only trails: adopted work and human recovery decisions, one sealed
#: record per line. Each record carries its own digest, so replay is idempotent
#: (adoption) and the decision is tamper-evident (recovery).
ADOPTIONS_FILE = "adoptions.jsonl"
RECOVERIES_FILE = "recoveries.jsonl"

UNIT_PREFIX = "fleet-graph-dd"

ACCEPTANCE_RECORD_PATH = ".dd-evidence/acceptance.json"

#: Where one generation's sealed gate-reject verdict is frozen for the launch
#: that carries it into the rework (wf-8d9737 rework contract A). Written by
#: `start` under the generation's run root, read by `dd run` through
#: `--gate-reject-file`.
GATE_REJECT_FILE = "gate-reject.json"

#: Rework contract B (wf-8d9737): a generation the engine cannot assemble a
#: new implement dispatch for -- a sealed-receipt replay that would open a
#: "new generation" with no new prompt and no new agent run -- is refused at
#: start instead of being launched as a fake generation.
CODE_REWORK_REPLAY_REFUSED = "REWORK_REPLAY_REFUSED"

#: Spec ⑮-b (wf-8d9737): the structured refusal for a gate REJECT whose board
#: ``work.decision.v1`` binding is unavailable -- the verdict record carries no
#: ``decision_message_id`` (or no ``decided_by``/``rationale``), or no verdict
#: record exists at all so only the terminal's one-line facts remain. An
#: unbound verdict must refuse the rework dispatch instead of silently
#: dispatching a task book with an empty binding (the g3 defect this kills).
CODE_REWORK_DECISION_UNBOUND = "REWORK_DECISION_UNBOUND"


def gate_decision_path(generation: int) -> str:
    """Where the gate seals its verdict for one generation (dd_scripts.GATE_PATH)."""
    from fleet_graph.graphs.dd_scripts import GATE_PATH

    return GATE_PATH.format(generation=generation)


def merge_result_path(generation: int) -> str:
    from fleet_graph.graphs.dd_scripts import MERGE_PATH

    return MERGE_PATH.format(generation=generation)


#: The spec's own acceptance declaration: one argv per non-empty line inside a
#: ```dd-acceptance fenced block. Declared in the spec so it is frozen and
#: digest-bound at bootstrap; multiple blocks concatenate in order.
ACCEPTANCE_FENCE = re.compile(r"^```dd-acceptance[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)

STATE_CREATED = "created"
STATE_RUNNING = "running"
STATE_AWAITING_GATE = "awaiting_gate"
STATE_INTERRUPTED = "interrupted"
STATE_COMPLETE = "complete"
# Terminal states are the pipeline's own vocabulary, passed through:
# complete / failed / refused / bounds / fault.

_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class ControlPlaneError(RuntimeError):
    """A refusal with one cause per code, and an honest retryability bit."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.detail, "retryable": self.retryable}


# --- failure classification: the three exits, four classes (R1-c) --------
#
# Every terminal that is not `complete` classifies into exactly one exit, and
# the classification is derived at read time from the run record -- never
# stored as a second truth. Rejection is a fourth *class* but not a fault:
# it exists so a human_gate REJECT is never read downstream as a fault
# (fault signal always 0) while keeping the same exit as implementation.
#
#   environment_contract -> the acceptance context was wrong (missing env
#       piece, wrong acceptance argv, missing setup). Exit: `reconfigure` the
#       acceptance context, then `start` a new generation. This is the exit
#       the legacy engine never had -- reconfigure was a permanent 409 after
#       FAILED, and three developments in a row died of it (wf-13ff9e tick 12).
#   implementation -> the work itself was judged insufficient. The in-graph
#       exit is the existing continuous-review REJECT -> rework loop, which
#       this module deliberately does not touch; at terminal (rework budget
#       exhausted, gate REJECT) the exit is a fresh generation whose graph
#       reworks with a fresh budget.
#   fabrication -> the seal's replay contradicted the actor's own claim
#       (the UNVERIFIED_TEST_CLAIM family). Final. Not reconfigurable, not
#       restartable: an actor that lied about its verification does not get
#       the exam changed or retaken (m-6d5aa4, a 5+-times-recurring behavior).
#   rejected -> human_gate REJECT (`GATE_REJECTED`): a verdict, not a fault.
#       Independent of the three fault classes above, so a rejection is never
#       read as `class="implementation"`/fault by downstream. It keeps the
#       implementation exit (fresh generation rework), so it still exits
#       deterministically on a new generation instead of lingering as a
#       permanent alert.

CLASS_ENVIRONMENT_CONTRACT = "environment_contract"
CLASS_IMPLEMENTATION = "implementation"
CLASS_FABRICATION = "fabrication"
CLASS_REJECTED = "rejected"

EXIT_RECONFIGURE = "reconfigure"
EXIT_REWORK = "rework"
EXIT_NONE = "none"

#: The fabrication family: codes whose one meaning is "the recorded claim and
#: the re-measured reality disagree". Deliberately minimal -- a code lands
#: here only when its taxonomy meaning is a claim/reality mismatch, because
#: this set is the one that closes a development for good.
FABRICATION_CODES = frozenset(
    {
        # Implement seal re-executed the verification commands and the real
        # exit codes differ from the claimed ones.
        "UNVERIFIED_TEST_CLAIM",
        # Worktree bytes, commit blob or recorded blob SHA disagree: the
        # recorded claim does not match what is actually there.
        "ARTIFACT_BLOB_MISMATCH",
    }
)

#: Codes where the work product or the actor's conduct -- not the acceptance
#: context -- is what failed. The remedy is new work, not a new environment.
IMPLEMENTATION_CODES = frozenset(
    {
        "REVIEWER_GIT_MUTATION",
        "UNDECLARED_ARTIFACT",
        "SECRET_SENTINEL_DETECTED",
        "REWORK_LIMIT_REACHED",
    }
)

#: Codes whose meaning is "a human (or the gate) REJECTed the work": a
#: verdict, not a fault. These classify as `CLASS_REJECTED`, independent of
#: environment_contract / implementation / fabrication, so a REJECT is never
#: emitted as (or read downstream as) a fault signal.
REJECTION_CODES = frozenset(
    {
        "GATE_REJECTED",
    }
)

#: The classes a downstream supervision plane reads as a *fault*. `rejected`
#: is deliberately absent: human_gate REJECT is a verdict, not a fault.
FAULT_CLASSES = frozenset({CLASS_ENVIRONMENT_CONTRACT, CLASS_IMPLEMENTATION, CLASS_FABRICATION})

#: Legacy results carry the code only inside the synthesized reason text
#: ("implement failed (PROVIDER_UNAVAILABLE)"); results written before R1-c
#: have no terminal_code field at all.
_REASON_CODE = re.compile(r"\(([A-Z][A-Z0-9_]*)\)")


def classify_failure(
    terminal: str,
    terminal_reason: str = "",
    terminal_code: str = "",
    terminal_detail: str = "",
) -> dict[str, Any] | None:
    """One failure record per non-complete terminal: cause class, the one
    mechanical code where one exists, the raw error in the failing
    collaborator's own words, and an honest retryability bit.

    Everything not provably fabrication or implementation classifies as
    environment/contract: that is the only default that cannot destroy
    information, because its exit (reconfigure + new generation) is reversible
    while the fabrication exit is final.
    """
    if not terminal or terminal == STATE_COMPLETE:
        return None
    code = terminal_code
    if not code:
        found = _REASON_CODE.search(terminal_reason or "")
        code = found.group(1) if found else ""
    raw_error = terminal_reason or ""
    if terminal_detail and terminal_detail not in raw_error:
        raw_error = f"{raw_error}; {terminal_detail}" if raw_error else terminal_detail
    if code in FABRICATION_CODES:
        cls, exit_, retryable = CLASS_FABRICATION, EXIT_NONE, False
    elif code in REJECTION_CODES:
        # human_gate REJECT: a verdict, not a fault. The class is
        # independently "rejected" so downstream never reads it as a fault
        # signal; the exit stays the fresh-generation rework, so a rejection
        # still exits deterministically on a new generation.
        cls, exit_, retryable = CLASS_REJECTED, EXIT_REWORK, True
    elif code in IMPLEMENTATION_CODES:
        cls, exit_, retryable = CLASS_IMPLEMENTATION, EXIT_REWORK, True
    else:
        cls, exit_, retryable = CLASS_ENVIRONMENT_CONTRACT, EXIT_RECONFIGURE, True
    return {
        "class": cls,
        "code": code,
        "raw_error": raw_error,
        "retryable": retryable,
        "exit": exit_,
    }


def derive_development_id(repo: Path, spec_digest: str, target_base_commit: str) -> str:
    """Deterministic, so `create` is idempotent: same admission, same identity."""
    seed = f"{repo}\x1f{spec_digest}\x1f{target_base_commit}".encode()
    return "dev-fg-" + hashlib.sha256(seed).hexdigest()[:12]


def derive_acceptance_commands(spec: bytes) -> list[list[str]]:
    """The argv lists the spec itself declares. Absent block means zero commands."""
    commands: list[list[str]] = []
    for block in ACCEPTANCE_FENCE.findall(spec.decode("utf-8", errors="replace")):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                argv = shlex.split(line)
            except ValueError as exc:
                raise ControlPlaneError(
                    "ACCEPTANCE_DECLARATION_INVALID",
                    f"cannot parse acceptance line {line!r}: {exc}",
                ) from exc
            if argv:
                commands.append(argv)
    return commands


def _parse_command_lines(lines: list[str], *, code: str) -> list[list[str]]:
    """Command lines into argv lists, with shell quoting honoured and refusals
    named. An empty list is a legitimate declaration of "no commands"."""
    commands: list[list[str]] = []
    for line in lines:
        if not isinstance(line, str):
            raise ControlPlaneError(code, f"commands are strings, got {line!r}")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            argv = shlex.split(stripped)
        except ValueError as exc:
            raise ControlPlaneError(code, f"cannot parse command {line!r}: {exc}") from exc
        if argv:
            commands.append(argv)
    return commands


def _validate_env(env: dict[str, str]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not key or "=" in key or not isinstance(value, str):
            raise ControlPlaneError(
                "ACCEPTANCE_ENV_INVALID",
                f"acceptance_env wants string names without '=' and string values, "
                f"got {key!r}={value!r}",
            )
        validated[key] = value
    return validated


def validate_timeouts(timeouts: dict[str, Any] | None) -> dict[str, int]:
    """Normalize and validate the per-stage run-fence overrides.

    ``{stage_id -> positive whole seconds}``. A stage id the contract does not
    declare, or a value that is not a positive integer, is refused by name
    rather than silently dropped: a timeout for a stage nobody runs is a typo
    that would read as "fenced at 7200" in an audit trail. Empty or ``None``
    keeps the runner's 3600s default for every stage -- existing behavior.
    """
    if not timeouts:
        return {}
    from fleet_graph.dd.lifecycle import Lifecycle

    declared = set(Lifecycle.load().stages)
    validated: dict[str, int] = {}
    for stage_id, seconds in timeouts.items():
        if not isinstance(stage_id, str) or stage_id not in declared:
            raise ControlPlaneError(
                "STAGE_TIMEOUT_UNKNOWN",
                f"{stage_id!r} is not a declared stage; per-stage timeouts may name "
                f"only {sorted(declared)}",
            )
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            raise ControlPlaneError(
                "STAGE_TIMEOUT_INVALID",
                f"timeout for {stage_id!r} must be a positive integer number of seconds, "
                f"got {seconds!r}",
            )
        validated[stage_id] = seconds
    return validated


# --- M4 stage seats: the record is the single source -----------------------
#
# S2.3/S3 收尾. A stage's model used to be server-side policy: `dd serve
# --stage-model` injected a fleet-wide override into every launched run, and
# that second source silently shadowed the role registry (fr ran five days on
# deepseek-v4-pro while its registry seat said claude-opus-5). The override is
# retired; seats now come from exactly one place -- the admission record. The
# committed ``config/stage-seats.json`` is the local projection of the role
# registry (the registry itself is agent-runtime's, closed out by wf-9b5931):
# its factory defaults fill every seat a dispatch did not name explicitly,
# and its allowed set is what ``development_create`` validates against.

STAGE_SEATS_FILE = (
    Path(__file__).resolve().parent.parent.parent.parent / "config" / "stage-seats.json"
)

#: Where a seat value came from, recorded per stage in the admission record.
SEAT_SOURCE_REGISTRY_DEFAULT = "registry-default"
SEAT_SOURCE_LINE_EXPLICIT = "line-explicit"

#: Structured seat refusals. Both mean "the unit was not created".
CODE_STAGE_SEAT_STAGE_UNKNOWN = "STAGE_SEAT_STAGE_UNKNOWN"
CODE_STAGE_SEAT_NOT_ALLOWED = "STAGE_SEAT_NOT_ALLOWED"
CODE_STAGE_SEAT_REGISTRY_UNREADABLE = "STAGE_SEAT_REGISTRY_UNREADABLE"


def load_stage_seat_registry(path: str | Path | None = None) -> dict[str, Any]:
    """The committed registry projection: ``{"default_seats", "allowed_seats"}``.

    Fail-closed on purpose: a missing or malformed projection means the
    factory values are unknowable, and freezing guessed seats into a record
    is exactly the drift this module exists to kill. The refusal names the
    file so the operator's next step is obvious.
    """
    seat_path = Path(path) if path is not None else STAGE_SEATS_FILE
    try:
        raw = json.loads(seat_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlPlaneError(
            CODE_STAGE_SEAT_REGISTRY_UNREADABLE,
            f"the stage-seat registry projection {seat_path} is missing or "
            f"malformed ({exc}); seats cannot be resolved fail-closed",
        ) from exc
    defaults = raw.get("default_seats")
    allowed = raw.get("allowed_seats")
    if (
        not isinstance(defaults, dict)
        or not isinstance(allowed, list)
        or not all(isinstance(seat, str) and seat for seat in allowed)
    ):
        raise ControlPlaneError(
            CODE_STAGE_SEAT_REGISTRY_UNREADABLE,
            f"the stage-seat registry projection {seat_path} must carry "
            '{"default_seats": {...}, "allowed_seats": [str, ...]}',
        )
    return {
        "default_seats": {str(stage): str(seat) for stage, seat in defaults.items() if str(seat)},
        "allowed_seats": frozenset(allowed),
    }


def _seat_eligible_stages() -> set[str]:
    """The lifecycle's llm stages -- the only stages a seat can apply to.

    ``configure`` / ``acceptance`` / ``human_gate`` / ``merger`` run
    in-process; a "seat" for them is a typo that would read as policy.
    """
    from fleet_graph.dd.lifecycle import Lifecycle

    return {stage for stage, spec in Lifecycle.load().stages.items() if spec.is_llm}


def validate_stage_seats(
    stage_models: dict[str, Any] | None, registry: dict[str, Any]
) -> dict[str, str]:
    """The explicitly dispatched seats, validated against the projection.

    A stage the lifecycle does not dispatch to an agent, or a seat value the
    registry does not allow, refuses by name -- the unit is not created
    (单不建立). Values must be non-empty strings.
    """
    if not stage_models:
        return {}
    if not isinstance(stage_models, dict):
        raise ControlPlaneError(
            CODE_STAGE_SEAT_STAGE_UNKNOWN,
            f"stage_models must be a dict of stage -> seat, got {stage_models!r}",
        )
    eligible = _seat_eligible_stages()
    allowed = registry["allowed_seats"]
    validated: dict[str, str] = {}
    for stage, seat in stage_models.items():
        if not isinstance(stage, str) or stage not in eligible:
            raise ControlPlaneError(
                CODE_STAGE_SEAT_STAGE_UNKNOWN,
                f"{stage!r} is not a stage an agent seat applies to; stage_models "
                f"may name only {sorted(eligible)}",
            )
        if not isinstance(seat, str) or not seat.strip():
            raise ControlPlaneError(
                CODE_STAGE_SEAT_NOT_ALLOWED,
                f"seat for {stage!r} must be a non-empty string, got {seat!r}",
            )
        seat = seat.strip()
        if seat not in allowed:
            raise ControlPlaneError(
                CODE_STAGE_SEAT_NOT_ALLOWED,
                f"seat {seat!r} for stage {stage!r} is not in the registry's "
                f"allowed set {sorted(allowed)}; fix the seat or extend the "
                "registry projection (config/stage-seats.json)",
            )
        validated[stage] = seat
    return validated


def resolve_stage_seats(
    stage_models: dict[str, Any] | None, registry: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    """Every seat the development will run under, plus where each came from.

    Registry factory defaults fill the llm stages a dispatch did not name;
    an explicit ``stage_models`` entry wins and is recorded as
    ``line-explicit``. Both mappings are frozen into record.json at
    admission -- the record is the single source the launch reads.
    """
    explicit = validate_stage_seats(stage_models, registry)
    seats: dict[str, str] = {}
    sources: dict[str, str] = {}
    for stage in sorted(_seat_eligible_stages()):
        if stage in explicit:
            seats[stage] = explicit[stage]
            sources[stage] = SEAT_SOURCE_LINE_EXPLICIT
        elif stage in registry["default_seats"]:
            seats[stage] = registry["default_seats"][stage]
            sources[stage] = SEAT_SOURCE_REGISTRY_DEFAULT
    return seats, sources


def build_h0_handoff(
    *, development_id: str, spec_digest: str, target_base_commit: str, remote_url: str
) -> dict[str, Any]:
    """The chain-root handoff. Canonical JSON; its digest seeds the receipt chain.

    Every field is anchored elsewhere (the spec digest and target base in the
    bootstrap commit, the remote in the record), so the digest is
    independently recomputable -- nothing here is invented at read time.
    """
    return {
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": development_id,
        "kind": "root-handoff",
        "remote_url": remote_url,
        "spec_digest": spec_digest,
        "target_base_commit": target_base_commit,
    }


def _inherited_environment() -> dict[str, str]:
    """What a launched run inherits from the control plane's own environment.

    - PATH: transient units start from the user manager's environment, not
      this process's, and agent-run is a `#!/usr/bin/env bun` script -- the
      same lesson scheduler/daemon.py:line_environment already carries
      (measured again here: `env: 'bun': No such file or directory`).
    - FLEET_GRAPH_BUS_TOKEN_FILE: the gate needs a credential, and a *path*
      in a transient unit's argv points at a 0600 file rather than being one.
      A raw FLEET_GRAPH_BUS_TOKEN value is deliberately never forwarded:
      `--setenv` travels through argv, and a token in argv is a token in
      `/proc`. Production runs on the token file (findings §26).
    - FLEET_GRAPH_COST_OBS_DIR / FLEET_GRAPH_MANAGEMENT_COST: the
      cost-observability wiring the launched run needs to collect. Both are
      non-secret site config (a textfile directory and a per-order float);
      forwarding them here is what lets an operator turn the data plane on
      via the MCP unit's env file instead of editing the launch argv.

    This is a whitelist, which is itself load-bearing for R4-3's credential
    separation: FLEET_GRAPH_DECISION_TOKEN_FILE (the decision publisher's
    credential) is not on it and must never be -- a dd run that inherits the
    decision credential is a pipeline that can approve itself (pinned by
    test).
    """
    import os

    env = {"PATH": os.environ.get("PATH", "")}
    token_file = os.environ.get("FLEET_GRAPH_BUS_TOKEN_FILE")
    if token_file:
        env["FLEET_GRAPH_BUS_TOKEN_FILE"] = token_file
    for key in ("FLEET_GRAPH_COST_OBS_DIR", "FLEET_GRAPH_MANAGEMENT_COST"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _systemd_unit_is_active(unit: str) -> bool:
    proc = subprocess.run(
        ["systemctl", "--user", "is-active", unit], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip() in {"active", "activating"}


@dataclass(frozen=True)
class DdLaunchSpec:
    """The argv for one detached `dd run`, shaped for TransientLauncher.

    Duck-typed against scheduler/launcher.py's LaunchSpec: the launcher only
    reads `argv()`, `log_file` and `unit_name`. The credential discipline is
    the launcher's too -- only the token *file path* crosses into the unit's
    environment, never a token value, and never through argv.
    """

    development_id: str
    dev_root: Path
    workspace: Path
    plugin_binding: Path
    remote_url: str
    remote_ref: str
    root_digest: str
    #: The frozen admission target, carried straight from the record. The
    #: runner must receive it as an explicit `--target-base` rather than
    #: re-inferring it from `.dev-dispatch/development.json`: on a worktree
    #: whose lineage already contains an older metadata commit (a prior
    #: development re-writing the same file), the `--diff-filter=A` anchor
    #: points at someone else's introduction and misreports an untouched
    #: identity as edited (the false `IDENTITY_EDITED` startup refusal).
    target_base_commit: str
    acceptance_commands: list[list[str]] = field(default_factory=list)
    #: The reconfigurable acceptance context (R1-c): setup commands run before
    #: acceptance, and an env overlay for both.
    setup_commands: list[list[str]] = field(default_factory=list)
    acceptance_env: dict[str, str] = field(default_factory=dict)
    board_card: str = ""
    #: The bounded principal that dispatched this development (a line folder or
    #: a human subject), forwarded to the runner as `--dispatched-by` and
    #: recorded on every dd-worker run as the `dispatched_by` label. Empty means
    #: no finer provenance was recorded and the runner falls back to the
    #: dispatcher. Never a run_id/uuid.
    dispatched_by: str = ""
    resume: bool = False
    launch_seq: int = 1
    #: Which generation this launch runs. The thread id, the derived run ids,
    #: and the gate's bus idempotency key all carry it, so a rerun of the same
    #: development cannot collide with its previous generation's identities.
    generation: int = 1
    #: Where this generation's run artifacts live. Generation 1 keeps the
    #: development root itself (existing on-disk layout); later generations
    #: get their own subdirectory so a rerun never overwrites history.
    run_root: Path | None = None
    #: The development's frozen seats (stage -> seat), read from the admission
    #: record at launch time -- the single source (M4). There is no second
    #: stage-model source to shadow the role registry any more: the control
    #: plane holds no seat policy of its own.
    stage_models: dict[str, str] = field(default_factory=dict)
    #: Per-stage run-fence overrides (stage_id -> seconds), forwarded from the
    #: admission record so the launched `dd run` fences each stage with its own
    #: timeout instead of the 3600s default. Empty means the runner's default
    #: applies to every stage -- existing behavior unchanged.
    timeouts: dict[str, int] = field(default_factory=dict)
    #: The gate REJECT verdict a rework generation starts from (wf-8d9737
    #: rework contract A), frozen by `start` at `GATE_REJECT_FILE` under the
    #: generation's run root. Empty means the launch is not a gate rework and
    #: carries no `--gate-reject-file` -- byte-identical to before.
    gate_reject_file: str = ""
    working_directory: str = DEFAULT_WORKING_DIRECTORY
    executable: str = DEFAULT_EXECUTABLE
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def unit_name(self) -> str:
        """The sequence keeps a relaunch from colliding with a unit systemd is
        still tearing down; the thread identity underneath stays derived from
        the development id alone."""
        return f"{UNIT_PREFIX}-{self.development_id}-r{self.launch_seq}"

    @property
    def log_file(self) -> Path:
        return self.dev_root / LOG_FILE

    def argv(self) -> list[str]:
        argv = [
            "systemd-run",
            "--user",
            "--collect",
            "--unit",
            self.unit_name,
            f"--working-directory={self.working_directory}",
        ]
        for key, value in sorted(self.environment.items()):
            argv += [f"--setenv={key}={value}"]
        argv += [
            f"--property=StandardOutput=append:{self.log_file}",
            f"--property=StandardError=append:{self.log_file}",
            self.executable,
            "dd",
            "run",
            "--development",
            self.development_id,
            "--workspace",
            str(self.workspace),
            "--plugin-binding",
            str(self.plugin_binding),
            "--remote-url",
            self.remote_url,
            "--remote-ref",
            self.remote_ref,
            "--root-digest",
            self.root_digest,
            # The admitted, persisted target base is forwarded verbatim so the
            # runner never has to infer it from the worktree's `.dev-dispatch`
            # history (which is both fragile and, post-bootstrap, writable by
            # the graded party). A genuine metadata mutation is still caught by
            # the admission and audit checks, and by the review sealer's
            # committed-identity-versus-dispatch binding.
            "--target-base",
            self.target_base_commit,
            "--run-root",
            str(self.run_root or self.dev_root),
            # The generation names the thread ({dev}:g{n}); run ids and the
            # gate's bus idempotency key derive from it too, so a fresh
            # generation never collides with the identities of the last one.
            "--generation",
            str(self.generation),
            # On disk and derived from the development id: the contract that
            # makes a kill-restart re-enter the same thread instead of
            # re-dispatching sealed stages. Shared across generations -- the
            # thread id inside carries the generation.
            "--checkpoint",
            str(self.dev_root / CHECKPOINT_FILE),
            # The durable MR is the goal; the merge stage still runs only
            # after the gate lets it.
            "--publish-merge",
        ]
        for command in self.acceptance_commands:
            argv += ["--accept", shlex.join(command)]
        for command in self.setup_commands:
            argv += ["--setup", shlex.join(command)]
        for key, value in sorted(self.acceptance_env.items()):
            argv += ["--accept-env", f"{key}={value}"]
        for stage, model in sorted(self.stage_models.items()):
            argv += ["--stage-model", f"{stage}={model}"]
        for stage, seconds in sorted(self.timeouts.items()):
            argv += ["--stage-timeout", f"{stage}={seconds}"]
        if self.gate_reject_file:
            argv += ["--gate-reject-file", self.gate_reject_file]
        if self.board_card:
            argv += ["--board-card", self.board_card]
        if self.dispatched_by:
            argv += ["--dispatched-by", self.dispatched_by]
        if self.resume:
            # Deliberately valueless: the gate re-reads the board on resume,
            # so whoever relaunches the unit cannot cast the verdict by it.
            argv += ["--resume"]
        return argv


class DdControlPlane:
    """Admission, launch, and read-side assembly for dd developments."""

    def __init__(
        self,
        *,
        root: Path = DEFAULT_DD_ROOT,
        plugin_binding: Path = DEFAULT_PLUGIN_BINDING,
        worktree_roots: tuple[str, ...] = DEFAULT_WORKTREE_ROOTS,
        working_directory: str = DEFAULT_WORKING_DIRECTORY,
        executable: str = DEFAULT_EXECUTABLE,
        launcher: Any = None,
        unit_probe: Callable[[str], bool] = _systemd_unit_is_active,
        board_factory: Callable[[], Any] | None = None,
        environment: dict[str, str] | None = None,
        scope_boundary: ScopeBoundary | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        from fleet_graph.scheduler.launcher import TransientLauncher

        self.root = Path(root)
        self.plugin_binding = Path(plugin_binding)
        self.worktree_roots = tuple(worktree_roots)
        self.working_directory = working_directory
        self.executable = executable
        self.launcher = launcher if launcher is not None else TransientLauncher()
        self.unit_probe = unit_probe
        self._board_factory = board_factory if board_factory is not None else self._default_board
        self.environment = (
            dict(environment) if environment is not None else _inherited_environment()
        )
        #: The scope boundary admission refuses against (B1). Data, not a literal
        #: scattered across call sites -- a rescope edits this, not the checks.
        self.scope_boundary = scope_boundary if scope_boundary is not None else default_boundary()
        self.clock = clock

    # --- admission -------------------------------------------------------

    def create(
        self,
        repo_path: str,
        target_base: str | None = None,
        spec_text: str | None = None,
        spec_path: str | None = None,
        dispatched_by: str = "",
        timeouts: dict[str, int] | None = None,
        stage_models: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Admit one development: derive everything, bootstrap, record.

        Idempotent: the same (repo, spec, base) admission returns the same
        development, with `already_admitted` set instead of a second identity.

        `dispatched_by` is the bounded principal (a line folder or a human
        subject) that dispatched this development; it is recorded and forwarded
        to the runner as the `dispatched_by` worker-run label. Empty means no
        finer provenance was recorded.

        `timeouts` optionally overrides the per-stage run fence (stage_id ->
        positive seconds); it is validated against the contract's stage ids and
        recorded for audit. Not passed (or empty) keeps the 3600s default for
        every stage -- existing behavior unchanged.

        `stage_models` (M4) is the seat parameter channel: `{llm_stage ->
        seat}`, validated against the registry projection before anything is
        created -- an unknown stage or a disallowed seat refuses with a
        structured code and the unit is not established. Every llm stage is
        frozen into record.json as `seats` (explicit entries recorded as
        `line-explicit`, the rest as the registry's factory defaults), and
        that record -- not any server-side global -- is what every launch of
        this development runs under. Seats freeze at first admission: a
        re-admission of the same (repo, spec, base) returns the existing
        record unchanged.
        """
        repo = self._admit_repo(repo_path)
        spec = self._read_spec(spec_text, spec_path)
        # M4: seats are validated before a single byte is written -- a bad
        # seat must refuse the admission, not poison a bootstrapped worktree.
        registry = load_stage_seat_registry()
        seats, seat_sources = resolve_stage_seats(stage_models, registry)
        # B1: admit nothing that actively crosses the declared scope boundary.
        # The refusal names the scope rule, so a crossing is a scope decision
        # rather than whatever downstream failure happened to fire first. The
        # admitted verdict is persisted on the record as the B3 scope evidence.
        scope_verdict = self._require_scope(spec.decode("utf-8", errors="replace"))
        base = self._default_target_base(repo, target_base)
        spec_digest = digest_of(spec)
        development_id = derive_development_id(repo, spec_digest, base)
        dev_root = self.root / development_id
        dispatched_by = (dispatched_by or "").strip()
        validated_timeouts = validate_timeouts(timeouts)

        existing = self._read_record_if_any(development_id)
        if existing is not None:
            if existing.get("spec_digest") != spec_digest or existing.get("repo_path") != str(repo):
                raise ControlPlaneError(
                    "ADMISSION_RECORD_MISMATCH",
                    f"{development_id} already admitted with a different spec or repo; "
                    "a changed spec is a new development in a fresh worktree",
                )
            if not existing.get("card_entity_id"):
                # The bus was down (or refused) at first admission; the card
                # publish is idempotency-keyed, so healing it here cannot fork.
                card = self._publish_card(
                    development_id, repo, str(existing.get("remote_ref") or "")
                )
                if card:
                    existing["card_entity_id"] = card
                    write_json_durable(dev_root / RECORD_FILE, existing)
            return self._creation_result(existing, already_admitted=True)

        self._refuse_foreign_binding(repo, development_id)
        remote_url = self._origin_url(repo)
        remote_ref = f"refs/heads/dd/{development_id}"
        acceptance_commands = derive_acceptance_commands(spec)

        bootstrap_commit = self._bootstrap(repo, development_id, spec, base)

        h0 = build_h0_handoff(
            development_id=development_id,
            spec_digest=spec_digest,
            target_base_commit=base,
            remote_url=remote_url,
        )
        h0_bytes = canonical_bytes(h0)
        root_handoff_digest = digest_of(h0_bytes)

        dev_root.mkdir(parents=True, exist_ok=True)
        (dev_root / H0_FILE).write_bytes(h0_bytes)

        card_entity_id = self._publish_card(development_id, repo, remote_ref)

        record = {
            "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
            "development_id": development_id,
            "repo_path": str(repo),
            "remote_url": remote_url,
            "remote_ref": remote_ref,
            "target_base_commit": base,
            "spec_digest": spec_digest,
            "spec_size_bytes": len(spec),
            "scope_verdict": scope_verdict,
            "bootstrap_commit": bootstrap_commit,
            "root_handoff_digest": root_handoff_digest,
            "acceptance_commands": acceptance_commands,
            "card_entity_id": card_entity_id,
            "dispatched_by": dispatched_by,
            #: The per-stage run-fence overrides, as validated. Empty (or never
            #: passed) keeps the runner's 3600s default for every stage -- this
            #: field is then present but empty, and existing behavior is
            #: byte-identical. Persisted so the audit trail says what fence the
            #: order actually ran under.
            "timeouts": validated_timeouts,
            #: M4 seat single source: every llm stage's seat, frozen at
            #: admission. `seats_source` records where each seat came from
            #: (`line-explicit` / `registry-default`). Every launch reads its
            #: seats from THIS mapping -- there is no server-side stage-model
            #: override any more -- and the launched argv is the record's
            #: `--stage-model stage=seat` pairs, so launches.jsonl and the
            #: record can never disagree.
            "seats": seats,
            "seats_source": seat_sources,
            "plugin_binding_path": str(self.plugin_binding),
            "created_at": iso(self.clock()),
        }
        write_json_durable(dev_root / RECORD_FILE, record)
        self.rebuild_status(development_id)
        return self._creation_result(record, already_admitted=False)

    def _creation_result(self, record: dict[str, Any], *, already_admitted: bool) -> dict[str, Any]:
        return {
            "development_id": record["development_id"],
            "already_admitted": already_admitted,
            "bootstrap": {
                "commit": record["bootstrap_commit"],
                "spec_digest": record["spec_digest"],
                "target_base_commit": record["target_base_commit"],
                "root_handoff_digest": record["root_handoff_digest"],
            },
            "remote": {"url": record["remote_url"], "ref": record["remote_ref"]},
            "acceptance_commands": record["acceptance_commands"],
            "seats": dict(record.get("seats") or {}),
            "seats_source": dict(record.get("seats_source") or {}),
            "card_entity_id": record["card_entity_id"],
            "gate_enabled": bool(record["card_entity_id"]),
        }

    def _admit_repo(self, repo_path: str) -> Path:
        if not repo_path or not str(repo_path).startswith("/"):
            raise ControlPlaneError(
                "REPO_PATH_INVALID", f"repo_path must be absolute: {repo_path!r}"
            )
        repo = Path(repo_path).resolve()
        if not any(
            repo == Path(root) or Path(root) in repo.parents for root in self.worktree_roots
        ):
            # Fail closed: the whitelist is the safety piece, not a convenience.
            raise ControlPlaneError(
                "WORKTREE_ROOT_NOT_ALLOWED",
                f"{repo} is outside the admitted worktree roots {list(self.worktree_roots)}",
            )
        if not repo.is_dir():
            raise ControlPlaneError("REPO_NOT_FOUND", f"{repo} is not a directory")
        inside = run_git(repo, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise ControlPlaneError(
                "REPO_NOT_A_GIT_WORKTREE", f"{repo} is not inside a git work tree"
            )
        top = run_git(repo, "rev-parse", "--show-toplevel", check=True).stdout.strip()
        if Path(top) != repo:
            raise ControlPlaneError(
                "REPO_NOT_A_WORKTREE_ROOT", f"{repo} is not the top of its work tree ({top})"
            )
        dirty = run_git(repo, "status", "--porcelain", check=True).stdout.strip()
        if dirty:
            raise ControlPlaneError(
                "WORKTREE_DIRTY",
                f"{repo} has uncommitted changes; admission freezes committed state only",
            )
        return repo

    def _read_spec(self, spec_text: str | None, spec_path: str | None) -> bytes:
        if bool(spec_text) == bool(spec_path):
            raise ControlPlaneError(
                "SPEC_INPUT_INVALID", "pass exactly one of spec_text or spec_path"
            )
        if spec_text is not None:
            spec = spec_text.encode("utf-8")
        else:
            try:
                spec = Path(str(spec_path)).read_bytes()
            except OSError as exc:
                raise ControlPlaneError(
                    "SPEC_PATH_UNREADABLE", f"cannot read spec at {spec_path}: {exc}"
                ) from exc
        if not spec.strip():
            raise ControlPlaneError("SPEC_EMPTY", "the approved spec is empty")
        return spec

    def _require_scope(self, text: str) -> dict[str, Any]:
        """Refuse a spec whose active content crosses the B1-B3 boundary (B1).

        The scope rule is the authority, and the refusal is attributable to it:
        ``SCOPE_BOUNDARY_VIOLATION`` carries the boundary's own id and each
        observed crossing, never a bare "something failed downstream". On a
        clean admit, the verdict is returned so the caller can persist it as
        the B3 scope-evidence artifact.
        """
        try:
            verdict = require_scope(text, self.scope_boundary)
        except ScopeViolationError as exc:
            raise ControlPlaneError("SCOPE_BOUNDARY_VIOLATION", str(exc)) from exc
        return verdict.as_dict()

    def _scope_crossings(self, text: str) -> list[dict[str, Any]]:
        """List the active border crossings in a handoff/receipt body, if any.

        A handoff that only *mentions* B4 in a deferral context is not a
        crossing; one that actively adds a phase or revives katana work is.
        The returned entries carry the boundary's own reference and label, so a
        quarantined artifact is attributable rather than a bare "looked weird".
        """
        verdict = evaluate_text(text, self.scope_boundary)
        if verdict.admitted:
            return []
        return [
            {"reference": v.reference, "label": v.label, "excerpt": v.excerpt}
            for v in verdict.violations
        ]

    def _default_target_base(self, repo: Path, target_base: str | None) -> str:
        """Explicit base -> committed identity -> HEAD, in that order.

        The §25 composition lesson at admission level: after a bootstrap
        commit, HEAD has moved past the base the spec was approved against.
        A re-admission that defaulted to HEAD would derive a *different*
        development id for the same admission, so the committed identity --
        tamper-anchored by `committed_target_base` -- wins over HEAD.
        """
        if target_base:
            return self._resolve_target_base(repo, target_base)
        try:
            committed = committed_target_base(repo)
        except IdentityChanged as changed:
            raise ControlPlaneError("IDENTITY_EDITED", str(changed)) from changed
        return committed or self._resolve_target_base(repo, None)

    def _resolve_target_base(self, repo: Path, target_base: str | None) -> str:
        ref = target_base or "HEAD"
        resolved = run_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if resolved.returncode != 0:
            raise ControlPlaneError(
                "TARGET_BASE_UNRESOLVED",
                f"cannot resolve {ref!r} in {repo}: {resolved.stderr.strip()[:200]}",
            )
        commit = resolved.stdout.strip()
        if not _HEX40.fullmatch(commit):
            raise ControlPlaneError("TARGET_BASE_UNRESOLVED", f"{ref!r} resolved to {commit!r}")
        return commit

    def _refuse_foreign_binding(self, repo: Path, development_id: str) -> None:
        committed = run_git(repo, "show", f"HEAD:{DEVELOPMENT_PATH}")
        if committed.returncode != 0:
            return
        try:
            bound = str(json.loads(committed.stdout).get("development_id") or "")
        except ValueError:
            bound = ""
        if bound and bound != development_id:
            raise ControlPlaneError(
                "REPO_BOUND_TO_OTHER_DEVELOPMENT",
                f"{repo} already carries the identity of {bound}; "
                "one worktree serves one development",
            )

    def _origin_url(self, repo: Path) -> str:
        origin = run_git(repo, "remote", "get-url", "origin")
        if origin.returncode != 0 or not origin.stdout.strip():
            raise ControlPlaneError(
                "REPO_HAS_NO_ORIGIN",
                f"{repo} has no `origin` remote; the durable ref needs somewhere to live",
            )
        return origin.stdout.strip()

    def _bootstrap(self, repo: Path, development_id: str, spec: bytes, base: str) -> str:
        """Write and commit the attempt context, unless it is already committed."""
        try:
            committed = committed_target_base(repo)
        except IdentityChanged as changed:
            raise ControlPlaneError("IDENTITY_EDITED", str(changed)) from changed
        if committed is not None:
            # Bootstrap already happened for this development (idempotent
            # re-admission after a lost record); the anchor commit stands.
            if committed != base:
                raise ControlPlaneError(
                    "TARGET_BASE_CONFLICT",
                    f"the committed identity freezes base {committed[:12]}, "
                    f"admission asked for {base[:12]}",
                )
            return self._introducing_commit(repo)

        context = build_attempt_context(
            development_id=development_id, spec=spec, target_base_commit=base
        )
        context.write(repo)
        for args in (
            ("add", "--", ".dev-dispatch"),
            (
                "-c",
                "user.name=Dev Dispatch",
                "-c",
                "user.email=dev-dispatch@example.invalid",
                "commit",
                "-q",
                "-m",
                f"dev-dispatch: bootstrap {development_id}",
            ),
        ):
            proc = run_git(repo, *args)
            if proc.returncode != 0:
                raise ControlPlaneError(
                    "BOOTSTRAP_COMMIT_FAILED",
                    f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}",
                    retryable=True,
                )
        return run_git(repo, "rev-parse", "HEAD", check=True).stdout.strip()

    def _introducing_commit(self, repo: Path) -> str:
        history = run_git(
            repo, "log", "--diff-filter=A", "--format=%H", "--", DEVELOPMENT_PATH, check=True
        )
        introduced = [line for line in history.stdout.split() if line]
        if not introduced:
            raise ControlPlaneError(
                "BOOTSTRAP_ANCHOR_MISSING", f"{DEVELOPMENT_PATH} has no introducing commit"
            )
        return introduced[-1]

    def _default_board(self) -> Any:
        try:
            from fleet_graph.bus.board import Board
            from fleet_graph.bus.client import BusClient

            return Board(BusClient())
        except Exception:
            # No credential, no bus: admission still works, the gate is then
            # disabled and says so, rather than half-wired.
            return None

    def _publish_card(self, development_id: str, repo: Path, remote_ref: str) -> str:
        board = self._board_factory()
        if board is None:
            return ""
        try:
            # The exact work.card.v1 schema the board enforces: title/status/
            # intent required, additionalProperties false (measured 2026-08-27).
            result = board.publish_card(
                {
                    "title": f"dd {development_id}",
                    "status": "doing",
                    "intent": f"dev-dispatch development in {repo}",
                    "development_id": development_id,
                    "links": [remote_ref],
                },
                idempotency_key=f"dd-card:{development_id}",
            )
        except Exception:
            # Best-effort: admission must survive a downed bus. The gate then
            # stays disabled and the result says so; a later create heals it.
            return ""
        return result.entity_id

    # --- records and status ----------------------------------------------

    def _dev_root(self, development_id: str) -> Path:
        return self.root / development_id

    def _read_record_if_any(self, development_id: str) -> dict[str, Any] | None:
        path = self._dev_root(development_id) / RECORD_FILE
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _record(self, development_id: str) -> dict[str, Any]:
        record = self._read_record_if_any(development_id)
        if record is None:
            raise ControlPlaneError(
                "DEVELOPMENT_NOT_FOUND", f"no admission record for {development_id}"
            )
        return record

    def _generation(self, record: dict[str, Any]) -> int:
        """The development's current generation; records from before R1-c are g1."""
        try:
            return max(1, int(record.get("generation") or 1))
        except (TypeError, ValueError):
            return 1

    def _gen_root(self, development_id: str, generation: int) -> Path:
        """Where one generation's run artifacts live. Generation 1 stays at the
        development root itself -- the layout every pre-R1-c development on
        disk already has -- and later generations get `g{n}/` so a rerun
        appends history instead of overwriting it."""
        root = self._dev_root(development_id)
        return root if generation <= 1 else root / f"g{generation}"

    def _launches(self, development_id: str) -> list[dict[str, Any]]:
        path = self._dev_root(development_id) / LAUNCHES_FILE
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _generation_launched(self, development_id: str, generation: int) -> bool:
        """Whether this generation ever launched (entries before R1-c are g1)."""
        return any(
            int(entry.get("generation") or 1) == generation
            for entry in self._launches(development_id)
        )

    def _resume_launched(self, development_id: str, generation: int) -> bool:
        """Whether this generation has a *resume* launch entry.

        The fresh launch that first carried the generation to the gate is not a
        resume. Distinguishing the two is what makes the claim/act-window guard
        reachable: an ``awaiting_gate`` development at generation N necessarily
        already has its fresh generation-N launch entry, so ``_generation_launched``
        is true the moment the gate is first consulted -- including when the only
        thing on disk is the O_EXCL claim a SIGKILLed resume left behind. Only a
        recorded ``mode == "resume"`` entry proves that recovery actually ran.
        """
        return any(
            int(entry.get("generation") or 1) == generation and entry.get("mode") == "resume"
            for entry in self._launches(development_id)
        )

    def _read_result(self, development_id: str, generation: int = 1) -> dict[str, Any] | None:
        path = self._gen_root(development_id, generation) / RESULT_FILE
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def _failure_of(self, result: dict[str, Any] | None) -> dict[str, Any] | None:
        if result is None:
            return None
        return classify_failure(
            str(result.get("terminal") or ""),
            str(result.get("terminal_reason") or ""),
            str(result.get("terminal_code") or ""),
            str(result.get("terminal_detail") or ""),
        )

    def _unit_active(self, development_id: str) -> str | None:
        launches = self._launches(development_id)
        if not launches:
            return None
        unit = str(launches[-1].get("unit") or "")
        if unit and self.unit_probe(unit):
            return unit
        return None

    def _checkpoint_state(self, development_id: str, generation: int = 1) -> dict[str, Any] | None:
        """The latest durable graph state for one generation's thread."""
        path = self._dev_root(development_id) / CHECKPOINT_FILE
        if not path.is_file():
            return None
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            with SqliteSaver.from_conn_string(str(path)) as saver:
                found = saver.get_tuple(
                    {"configurable": {"thread_id": f"{development_id}:g{generation}"}}
                )
        except Exception:
            return None
        if found is None:
            return None
        values = found.checkpoint.get("channel_values") or {}
        return dict(values) if isinstance(values, dict) else None

    def rebuild_status(self, development_id: str) -> dict[str, Any]:
        """Recompute the status cache from git + checkpoint + run artifacts.

        This is the proof the cache is a cache: everything in `status.json`
        comes from here, and nothing reads the file except the list fast path.
        """
        record = self._record(development_id)  # refuses unknown ids before anything else
        generation = self._generation(record)
        result = self._read_result(development_id, generation)
        active_unit = self._unit_active(development_id)
        checkpoint = self._dev_root(development_id) / CHECKPOINT_FILE

        stage = ""
        terminal = ""
        terminal_reason = ""
        head_commit = ""
        awaiting: dict[str, Any] | None = None
        gate_refused: dict[str, Any] | None = None
        if result is not None:
            stage = str(result.get("stage") or "")
            terminal = str(result.get("terminal") or "")
            terminal_reason = str(result.get("terminal_reason") or "")
            head_commit = str(result.get("head_commit") or "")
            raw_awaiting = result.get("awaiting")
            awaiting = dict(raw_awaiting) if isinstance(raw_awaiting, dict) else None
            raw_gate_refused = result.get("gate_refused")
            gate_refused = dict(raw_gate_refused) if isinstance(raw_gate_refused, dict) else None

        if active_unit:
            state = STATE_RUNNING
        elif awaiting:
            state = STATE_AWAITING_GATE
        elif terminal:
            state = terminal
        elif checkpoint.is_file():
            # A durable thread with no result and no unit: killed mid-run.
            state = STATE_INTERRUPTED
            values = self._checkpoint_state(development_id, generation) or {}
            stage = str(values.get("stage") or stage)
            head_commit = str(values.get("head_commit") or head_commit)
        else:
            state = STATE_CREATED

        status = {
            "development_id": development_id,
            "state": state,
            "generation": generation,
            "stage": stage,
            "terminal": terminal,
            "terminal_reason": terminal_reason,
            "head_commit": head_commit,
            #: The bounded principal (a line folder or a human subject) that
            #: dispatched this development. Copied from the authoritative
            #: admission record -- never recomputed from worker-run argv
            #: labels, which are only a label projection -- and fail-soft to
            #: an empty string when missing. This is what lets the read model
            #: attribute a development to the line that dispatched it.
            "dispatched_by": str(record.get("dispatched_by") or ""),
            # The failure record: cause class, one mechanical code, the raw
            # error in the failing collaborator's own words, retryability, and
            # which of the three exits is open. Derived, never stored twice.
            "failure": self._failure_of(result),
            "awaiting": awaiting,
            # A resumable gate refusal: the gate saw a verdict it could not
            # interpret, suspended rather than ended, and is still waiting for
            # a proper one. Surfaced so the operator sees why an otherwise
            # "awaiting_gate" development is not progressing.
            "gate_refused": gate_refused,
            "active_unit": active_unit or "",
            "launches": len(self._launches(development_id)),
        }
        write_json_durable(self._dev_root(development_id) / STATUS_FILE, status)
        return status

    # --- start / gate ----------------------------------------------------

    def start(self, development_id: str) -> dict[str, Any]:
        """Launch the development detached: resume the in-flight generation,
        or -- after a retryable terminal or a reconfigure -- start the next one.

        The three failure exits gate this door: a fabrication terminal refuses
        (final), any other terminal starts generation n+1 fresh, and a
        non-terminal development resumes its own generation's thread.
        """
        record = self._record(development_id)
        active = self._unit_active(development_id)
        if active:
            return {
                "development_id": development_id,
                "started": False,
                "already_running": True,
                "unit": active,
            }
        development_root = self._dev_root(development_id)
        generation = self._generation(record)
        result = self._read_result(development_id, generation)
        terminal = str((result or {}).get("terminal") or "")
        if terminal == STATE_COMPLETE:
            raise ControlPlaneError(
                "DEVELOPMENT_COMPLETE",
                f"{development_id} is complete; a finished development has nothing to restart",
            )
        failure = self._failure_of(result)
        if failure is not None and failure["class"] == CLASS_FABRICATION:
            raise ControlPlaneError(
                "FABRICATION_FINAL",
                f"{development_id} g{generation} failed as {failure['code']}: the seal's "
                f"replay contradicted the actor's claim ({failure['raw_error'][:300]}); "
                "a fabricated development is final and does not restart",
            )
        pending = bool(record.get("reconfigured_pending_start"))
        bump = bool(terminal) or (pending and self._generation_launched(development_id, generation))
        if bump or pending:
            if bump:
                generation += 1
                record["generation"] = generation
            record.pop("reconfigured_pending_start", None)
            write_json_durable(development_root / RECORD_FILE, record)
        if bump:
            return self._launch(record, resume=False, generation=generation)
        resume = (development_root / CHECKPOINT_FILE).is_file() and self._generation_launched(
            development_id, generation
        )
        # A dead unit on a previously-launched generation is the stale-running
        # contradiction the recovery path exists to heal: the development was
        # persisted as running, its recorded unit died, and this start resumes
        # the same generation in place. That is a *resumed recovery*, distinct
        # from a gate resume (whose result still carries an ``awaiting`` note)
        # and from a terminal resolution -- mark it so callers can tell them
        # apart.
        recovered = resume and not bool((result or {}).get("awaiting"))
        return self._launch(record, resume=resume, generation=generation, recovered=recovered)

    def _launch(
        self,
        record: dict[str, Any],
        *,
        resume: bool,
        generation: int = 1,
        recovered: bool = False,
    ) -> dict[str, Any]:
        development_id = str(record["development_id"])
        dev_root = self._dev_root(development_id)
        run_root = self._gen_root(development_id, generation)
        if not self.plugin_binding.is_file():
            raise ControlPlaneError(
                "PLUGIN_BINDING_UNREADABLE",
                f"no plugin binding at {self.plugin_binding}; the capability "
                "check is fail-closed and will not be skipped",
            )
        # Rework contract A/B (wf-8d9737): a generation whose predecessor was
        # gate-REJECTed is a rework generation, and its launch must carry the
        # rejecting verdict so the implement prompt can be assembled with it.
        # A launch that cannot carry it (record unreadable or contradicting
        # the terminal) is refused here rather than opened as a fake new
        # generation.
        gate_reject_file = ""
        if generation > 1:
            gate_reject = self._seal_gate_rework(record, generation)
            if gate_reject is not None:
                path = run_root / GATE_REJECT_FILE
                write_json_durable(path, gate_reject)
                gate_reject_file = str(path)
                with contextlib.suppress(OSError):
                    event_path = run_root / EVENTS_FILE
                    event_path.parent.mkdir(parents=True, exist_ok=True)
                    with event_path.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "at": iso(self.clock()),
                                    "event": "gate_rework_dispatch",
                                    "development_id": development_id,
                                    "generation": generation,
                                    "rejected_generation": gate_reject.get("rejected_generation"),
                                    "decision_message_id": gate_reject.get("decision_message_id"),
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            + "\n"
                        )
        run_root.mkdir(parents=True, exist_ok=True)
        seq = len(self._launches(development_id)) + 1
        spec = DdLaunchSpec(
            development_id=development_id,
            dev_root=dev_root,
            workspace=Path(str(record["repo_path"])),
            plugin_binding=Path(str(record["plugin_binding_path"])),
            remote_url=str(record["remote_url"]),
            remote_ref=str(record["remote_ref"]),
            root_digest=str(record["root_handoff_digest"]),
            target_base_commit=str(record["target_base_commit"]),
            acceptance_commands=[list(c) for c in record.get("acceptance_commands") or []],
            setup_commands=[list(c) for c in record.get("setup_commands") or []],
            acceptance_env=dict(record.get("acceptance_env") or {}),
            board_card=str(record.get("card_entity_id") or ""),
            dispatched_by=str(record.get("dispatched_by") or ""),
            resume=resume,
            launch_seq=seq,
            generation=generation,
            run_root=run_root,
            # M4 seat single source: the seats the record froze at admission,
            # never a server-side global. The launched `dd run` argv carries
            # exactly these pairs, so launches.jsonl's measured argv and
            # record.seats agree by construction.
            stage_models=dict(record.get("seats") or {}),
            timeouts=dict(record.get("timeouts") or {}),
            gate_reject_file=gate_reject_file,
            working_directory=self.working_directory,
            executable=self.executable,
            environment=dict(self.environment),
        )
        launched = self.launcher.launch(spec)
        entry = {
            "seq": seq,
            "unit": spec.unit_name,
            "mode": "resume" if resume else "fresh",
            "generation": generation,
            "recovered": recovered,
            "at": iso(self.clock()),
            "started": launched.started,
            "detail": launched.detail,
            "argv": spec.argv(),
        }
        with (dev_root / LAUNCHES_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if not launched.started and not getattr(self.launcher, "dry_run", False):
            raise ControlPlaneError(
                "LAUNCH_FAILED", f"{spec.unit_name}: {launched.detail}", retryable=True
            )
        self.rebuild_status(development_id)
        return {
            "development_id": development_id,
            "started": launched.started,
            "already_running": False,
            "unit": spec.unit_name,
            "mode": entry["mode"],
            "recovered": recovered,
            "generation": generation,
            "thread_id": f"{development_id}:g{generation}",
            "checkpoint": str(dev_root / CHECKPOINT_FILE),
        }

    def _seal_gate_rework(self, record: dict[str, Any], generation: int) -> dict[str, Any] | None:
        """The gate REJECT verdict generation `generation` must rework from.

        Rework contract A (wf-8d9737): when a development is started into
        generation N+1 after its generation N ended ``GATE_REJECTED``, the
        launch carries that rejecting verdict -- decision message id,
        decided_by, rationale -- so the implement prompt can be assembled with
        it. The verdict is read from the gate decision record at
        `gate_decision_path(N)`: committed first, then the worktree copy the
        gate's own refusal left behind (which is committed here, so the
        verdict becomes a durable part of the chain).

        Spec ⑮-b (wf-8d9737): the verdict must be *bound* -- the three fields
        the rework consumes (`decision_message_id`, `decided_by`, `rationale`)
        all non-empty, sourced from the board ``work.decision.v1`` the gate
        actually consumed, and carried verbatim. Two shapes cannot serve and
        refuse the start with ``REWORK_DECISION_UNBOUND`` instead of
        dispatching an empty-binding task book:

        - a verdict record that says REJECT but is missing any of the three
          fields (an unbound message id is exactly the g3 live defect), and
        - a GATE_REJECTED terminal with no sealed verdict record anywhere
          (a legacy result predating the seal) -- the terminal's one-line
          facts are no longer a success-path substitute for the board
          decision; the old ``terminal-facts`` fallback is gone.

        Returns None when generation N was not a gate rejection at all (no
        record anywhere, no GATE_REJECTED terminal) -- the ordinary reconfigure
        or retry path, untouched. A record that exists but cannot serve -- not
        readable, or not a REJECT while the terminal says otherwise -- raises
        ``REWORK_REPLAY_REFUSED``: starting the generation without its
        mandated rationale input is exactly the fake-rework defect this
        contract exists to kill, so the launch is refused instead.
        """
        development_id = str(record["development_id"])
        rejected = generation - 1
        repo = Path(str(record["repo_path"]))
        prior_result = self._read_result(development_id, rejected)
        prior_failure = self._failure_of(prior_result)
        was_rejected = prior_failure is not None and prior_failure["code"] in REJECTION_CODES

        decision = self._committed_gate_decision(record, rejected)
        source_record = "committed"
        if decision is None:
            path = repo / gate_decision_path(rejected)
            if path.is_file():
                source_record = "worktree"
                try:
                    decision = dict(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    decision = {}
                if str(decision.get("decision") or "").strip().upper() == "REJECT":
                    # The gate sealed its refusal into the worktree but the
                    # pipeline refused before any materialize step could
                    # commit it. Commit the reserved-path record now so the
                    # verdict is durable and the next read takes the standard
                    # committed path.
                    committed = self._commit_gate_record(
                        repo, gate_decision_path(rejected), rejected
                    )
                    if not committed:
                        raise ControlPlaneError(
                            CODE_REWORK_REPLAY_REFUSED,
                            f"{development_id} g{generation} cannot start as a gate rework: "
                            f"the rejecting verdict at {gate_decision_path(rejected)} could "
                            "not be committed; no new implement dispatch will be assembled "
                            "without it (missing: gate-reject-rationale)",
                            retryable=True,
                        )
        if decision is None:
            if not was_rejected:
                return None
            # Spec ⑮-b: a GATE_REJECTED terminal with no sealed verdict record
            # anywhere (a legacy result predating the seal) has no board
            # binding to rework from. The terminal's one-line facts were the
            # old ``terminal-facts`` fallback; that fallback dispatched task
            # books with an empty binding (the g3 shape) and is now a refusal,
            # not a success path.
            raise ControlPlaneError(
                CODE_REWORK_DECISION_UNBOUND,
                f"{development_id} g{generation} cannot start as a gate rework: the "
                f"GATE_REJECTED terminal of g{rejected} has no sealed verdict record at "
                f"{gate_decision_path(rejected)}, so the board work.decision.v1 binding "
                "(decision_message_id / decided_by / rationale) is unavailable; no new "
                "implement dispatch will be assembled from terminal-facts (source: none)",
            )
        if str(decision.get("decision") or "").strip().upper() != "REJECT":
            if not was_rejected:
                return None
            raise ControlPlaneError(
                CODE_REWORK_REPLAY_REFUSED,
                f"{development_id} g{generation} cannot start as a gate rework: the gate "
                f"record for g{rejected} says "
                f"{str(decision.get('decision') or '')!r} while its terminal was "
                "GATE_REJECTED; no new implement dispatch will be assembled against "
                "contradicting verdicts (missing: gate-reject-rationale)",
            )
        # Spec ⑮-b: the verdict must be bound to the board work.decision.v1
        # the gate actually consumed. An empty binding field means the
        # rework's authoritative input is unavailable -- refuse the dispatch
        # (observably, by code) instead of sealing and dispatching an empty
        # task book.
        binding = {
            "decision_message_id": str(decision.get("decision_message_id") or "").strip(),
            "decided_by": str(decision.get("decided_by") or "").strip(),
            "rationale": str(decision.get("rationale") or "").strip(),
        }
        unbound = sorted(name for name, value in binding.items() if not value)
        if unbound:
            raise ControlPlaneError(
                CODE_REWORK_DECISION_UNBOUND,
                f"{development_id} g{generation} cannot start as a gate rework: the "
                f"rejecting verdict at {gate_decision_path(rejected)} is not bound to "
                f"its board work.decision.v1 (empty: {', '.join(unbound)}); no new "
                "implement dispatch will be assembled with an empty binding",
            )
        return {
            "development_id": development_id,
            "rejected_generation": rejected,
            "decision": "REJECT",
            "decision_message_id": str(decision.get("decision_message_id") or ""),
            "decided_by": str(decision.get("decided_by") or ""),
            "rationale": str(decision.get("rationale") or ""),
            "question_note_id": str(decision.get("question_note_id") or ""),
            # The source of truth is the board work.decision.v1 the gate
            # consumed; `source_record` names where the sealed copy was read
            # from (committed chain, or the worktree copy now committed).
            "source": "board:work.decision.v1",
            "source_record": source_record,
        }

    def _commit_gate_record(self, repo: Path, relative: str, rejected: int) -> bool:
        for args in (
            ("add", "--", relative),
            (
                "-c",
                "user.name=Dev Dispatch",
                "-c",
                "user.email=dev-dispatch@example.invalid",
                "commit",
                "-q",
                "-m",
                f"dev-dispatch: seal gate reject g{rejected}",
            ),
        ):
            proc = run_git(repo, *args)
            if proc.returncode != 0:
                return False
        return True

    # --- reconfigure: the environment/contract exit -----------------------

    def reconfigure(
        self,
        development_id: str,
        acceptance_env: dict[str, str] | None = None,
        acceptance_argv: list[str] | None = None,
        setup: list[str] | None = None,
    ) -> dict[str, Any]:
        """Change the acceptance context -- and nothing else -- of a development.

        This is the environment/contract exit the legacy engine never had:
        reconfigure there was a permanent 409 once a development FAILED, so an
        acceptance environment problem killed the whole development. Here it
        is callable in FAILED and in every non-terminal state.

        The scope is enforced by construction: the only parameters that exist
        are the acceptance context (env overlay, acceptance argv, setup
        commands). There is no spec parameter, no implementation parameter,
        no role patch -- the spec stays frozen under its bootstrap digest and
        a changed spec remains what it always was: a new development.

        A fabrication terminal (the UNVERIFIED_TEST_CLAIM family) refuses:
        an actor that lied about its verification does not get the exam
        changed. `complete` refuses too; there is nothing left to accept.
        """
        record = self._record(development_id)
        generation = self._generation(record)
        result = self._read_result(development_id, generation)
        terminal = str((result or {}).get("terminal") or "")
        if terminal == STATE_COMPLETE:
            raise ControlPlaneError(
                "DEVELOPMENT_COMPLETE",
                f"{development_id} is complete; its acceptance context no longer matters",
            )
        failure = self._failure_of(result)
        if failure is not None and failure["class"] == CLASS_FABRICATION:
            raise ControlPlaneError(
                "FABRICATION_FINAL",
                f"{development_id} g{generation} failed as {failure['code']}: the seal's "
                f"replay contradicted the actor's claim ({failure['raw_error'][:300]}); "
                "a fabricated development is final and cannot be reconfigured",
            )
        if acceptance_env is None and acceptance_argv is None and setup is None:
            raise ControlPlaneError(
                "RECONFIGURE_EMPTY",
                "pass at least one of acceptance_env, acceptance_argv, setup",
            )

        changes: dict[str, Any] = {}
        if acceptance_argv is not None:
            changes["acceptance_commands"] = _parse_command_lines(
                acceptance_argv, code="ACCEPTANCE_DECLARATION_INVALID"
            )
        if setup is not None:
            changes["setup_commands"] = _parse_command_lines(
                setup, code="SETUP_DECLARATION_INVALID"
            )
        if acceptance_env is not None:
            changes["acceptance_env"] = _validate_env(acceptance_env)

        record.update(changes)
        history = list(record.get("reconfigures") or [])
        history.append(
            {
                "at": iso(self.clock()),
                "generation": generation,
                "changed": sorted(changes),
            }
        )
        record["reconfigures"] = history
        record["reconfigured_pending_start"] = True
        write_json_durable(self._dev_root(development_id) / RECORD_FILE, record)
        self.rebuild_status(development_id)

        next_generation = (
            generation + 1
            if terminal or self._generation_launched(development_id, generation)
            else generation
        )
        return {
            "development_id": development_id,
            "reconfigured": True,
            "applied": changes,
            "generation": generation,
            "next_start_generation": next_generation,
            "spec_digest": record["spec_digest"],
            "scope": "acceptance context only; the spec and the implementation "
            "are untouchable by construction",
        }

    def gate(
        self,
        development_id: str,
        resume: bool = False,
        action_key: str | None = None,
    ) -> dict[str, Any]:
        """The gate's state, and -- on request -- a valueless resume.

        There is deliberately no decision input anywhere on this path.
        Verdicts travel only as `work.decision.v1` on the board, published by
        a human; on resume the graph re-reads the board itself.

        ``action_key`` is the decision bridge's durable exactly-once claim.
        When supplied, the gate persists a ``(action_key, generation)``
        uniqueness constraint *before* launching, so a duplicate transport call
        (the bridge replaying a SIGKILLed resume) returns ``already_resumed``
        instead of launching a second real recovery for the same decision.
        """
        record = self._record(development_id)
        status = self.rebuild_status(development_id)
        awaiting = status.get("awaiting") or None
        generation = self._generation(record)

        decision = self._committed_gate_decision(record, generation)
        gate_refused = status.get("gate_refused") or None
        gate_report: dict[str, Any] = {
            "development_id": development_id,
            "state": status["state"],
            "pending": bool(awaiting) and decision is None,
            "awaiting": awaiting,
            "decision": decision,
            # A resumable refusal: the gate saw a verdict it could not
            # interpret and is still waiting for a proper one -- reported so
            # the operator knows a malformed verdict, not an absent one, is
            # what is holding the line.
            "gate_refused": gate_refused,
            "ruling": "decisions travel only as work.decision.v1 on the board; "
            "this tool carries none",
        }
        if awaiting and decision is None:
            gate_report["decision_on_board"] = self._decision_on_board(awaiting)
        if not resume:
            return gate_report

        if status["state"] == STATE_RUNNING:
            raise ControlPlaneError(
                "ALREADY_RUNNING", f"{development_id} is running as {status['active_unit']}"
            )
        if not (self._dev_root(development_id) / CHECKPOINT_FILE).is_file():
            raise ControlPlaneError(
                "CHECKPOINT_MISSING",
                f"{development_id} has no durable checkpoint; there is no thread to resume",
            )
        # A previous resume already claimed this exact (action_key, generation).
        # If the recovery it guards actually launched, this is a duplicate
        # transport call and the same logical success, not a second launch. If
        # it has *not* yet launched, the earlier attempt crashed in the
        # claim/act window: the claim file exists but no launch entry was ever
        # written. Reporting ``already_resumed`` then would assert a recovery
        # that never ran, so carry it out now instead -- the interrupted
        # recovery completes exactly once. (``_claim_resume_action`` runs inside
        # this condition: it must claim before a fresh launch below.)
        if action_key and not self._claim_resume_action(development_id, generation, action_key):
            if not self._resume_launched(development_id, generation):
                # Claim/act window: the interrupted recovery completes now.
                pass
            elif status["state"] != STATE_AWAITING_GATE:
                # The single transferred per its verdict semantics, so the
                # decision truly was consumed: the same action key dedupes.
                gate_report["resume"] = {
                    "development_id": development_id,
                    "generation": generation,
                    "already_resumed": True,
                }
                gate_report["already_resumed"] = True
                return gate_report
            else:
                # M3.1 defect 2: the earlier resume launched but the single is
                # still parked at the gate -- the verdict was never consumed
                # (the unit died, e.g. TEMPFAIL). A one-shot claim burned by a
                # *failed* resume would refuse the same verdict forever. Return
                # the claim so the redelivery re-attempts the resume; only a
                # consumed verdict dedupes (the branch above).
                self._release_resume_claim(development_id, generation, action_key)
                if not self._claim_resume_action(development_id, generation, action_key):
                    raise ControlPlaneError(
                        "RESUME_CLAIM_CONTESTED",
                        f"{development_id} g{generation} resume claim for this action key "
                        "was re-taken concurrently; retry the delivery",
                        retryable=True,
                    )
        gate_report["resume"] = self._launch(record, resume=True, generation=generation)
        return gate_report

    def publish_gate_decision(
        self,
        development_id: str,
        *,
        decision: str,
        decided_by: str,
        reason: str = "",
        action_key: str = "",
    ) -> dict[str, Any]:
        """Deliver one verdict to the single's decision read model (the board).

        M3.1 defect 1 (S10 裁决送达必须落地): the gate resume is valueless by
        design -- the resumed graph re-reads the board -- so a delivery that
        only resumed never gave the single the verdict. Delivering means
        publishing the verdict as a ``work.decision.v1`` answering the
        single's pending question note (a ref to the note is what makes
        ``Board.decision_for`` resolve it), with ``decided_by`` the already-
        authorized principal and the delivery's reason as the rationale. The
        publish is idempotency-keyed on the delivery's action key, so a
        redelivered verdict republishes nothing. The caller then resumes: the
        graph consumes the verdict -- REJECT terminalises the single as
        ``refused``, APPROVE proceeds through merge.

        The Board class itself still publishes no decisions (the structural
        rule stands): this goes through the board's bus client, the same
        channel the human Q&A verdicts ride. No board, or no pending question
        to answer, is a structured refusal -- never a valueless resume that
        silently drops the verdict.
        """
        record = self._record(development_id)
        status = self.rebuild_status(development_id)
        awaiting = status.get("awaiting") or {}
        question_note_id = str(awaiting.get("question_note_id") or "")
        card_entity_id = str(awaiting.get("card_entity_id") or "")
        if not question_note_id:
            raise ControlPlaneError(
                "GATE_TICKET_UNRESOLVED",
                f"{development_id} carries no pending question note; "
                "there is no question for a verdict to answer",
            )
        board = self._board_factory()
        if board is None:
            raise ControlPlaneError(
                "GATE_BOARD_UNAVAILABLE",
                "no board is configured; the verdict cannot reach the single's decision read model",
            )
        from fleet_graph.bus.board import DECISION_KIND, WORK_NOTES

        payload = {
            "card_entity_id": card_entity_id,
            "question": "",
            "decision": decision,
            "decided_by": decided_by,
            "rationale": reason,
        }
        idempotency_key = action_key or (
            f"dd-gate:{development_id}:g{self._generation(record)}:{decision}"
        )
        published = board.client.publish(
            WORK_NOTES,
            DECISION_KIND,
            payload,
            idempotency_key,
            refs=[{"target_entity": question_note_id}],
        )
        return {
            "development_id": development_id,
            "question_note_id": question_note_id,
            "card_entity_id": card_entity_id,
            "decision": decision,
            "decided_by": decided_by,
            "message_id": str(getattr(published, "message_id", "") or ""),
            "idempotency_key": idempotency_key,
        }

    def _resume_claim_path(self, development_id: str, generation: int, action_key: str) -> Path:
        digest = hashlib.sha256(action_key.encode("utf-8")).hexdigest()
        return (
            self._dev_root(development_id) / "resume-claims" / f"g{generation}" / f"{digest}.json"
        )

    def _claim_resume_action(self, development_id: str, generation: int, action_key: str) -> bool:
        """Atomically claim ``(action_key, generation)``; False when already claimed.

        ``O_EXCL`` is the persistent unique constraint: two concurrent resumes of
        the same decision cannot both claim, and the claim survives a restart, so
        the decision bridge's SIGKILL replay can distinguish "already recovered"
        from "recover now" without launching twice.
        """
        path = self._resume_claim_path(development_id, generation, action_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "development_id": development_id,
                    "generation": generation,
                    "action_key": action_key,
                    "at": iso(self.clock()),
                },
                handle,
                sort_keys=True,
            )
        return True

    def _release_resume_claim(self, development_id: str, generation: int, action_key: str) -> None:
        """Return a burned claim after a resume that did not consume (M3.1
        defect 2).

        The claim exists to make a resume exactly-once against duplicate
        transport calls -- not to spend the verdict's only delivery on a unit
        that died unconsumed. Unlinking the claim lets the same action key be
        claimed again; the release is traced into the generation's
        ``events.jsonl`` (best-effort) so the return is auditable like every
        other gate action.
        """
        path = self._resume_claim_path(development_id, generation, action_key)
        with contextlib.suppress(OSError):
            path.unlink()
        with contextlib.suppress(OSError):
            event_path = self._gen_root(development_id, generation) / EVENTS_FILE
            event_path.parent.mkdir(parents=True, exist_ok=True)
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "at": iso(self.clock()),
                            "event": "resume_claim_released",
                            "development_id": development_id,
                            "generation": generation,
                            "action_key": action_key,
                            "reason": "previous resume did not consume the verdict",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )

    def _committed_gate_decision(
        self, record: dict[str, Any], generation: int = 1
    ) -> dict[str, Any] | None:
        found = run_git(
            Path(str(record["repo_path"])), "show", f"HEAD:{gate_decision_path(generation)}"
        )
        if found.returncode != 0:
            return None
        try:
            return dict(json.loads(found.stdout))
        except ValueError:
            return None

    def _decision_on_board(self, awaiting: dict[str, Any]) -> bool | None:
        board = self._board_factory()
        if board is None:
            return None
        try:
            from fleet_graph.bus.board import GateTicket

            ticket = GateTicket.from_dict(
                {
                    "question_note_id": str(awaiting.get("question_note_id") or ""),
                    "card_entity_id": str(awaiting.get("card_entity_id") or ""),
                }
            )
            return board.decision_for(ticket) is not None
        except Exception:
            return None

    # --- read side -------------------------------------------------------

    def get(self, development_id: str) -> dict[str, Any]:
        record = self._record(development_id)
        status = self.rebuild_status(development_id)
        return {
            **status,
            "repo_path": record["repo_path"],
            "worktree_path": record["repo_path"],
            "remote_url": record["remote_url"],
            "remote_ref": record["remote_ref"],
            "target_base_commit": record["target_base_commit"],
            "spec_digest": record["spec_digest"],
            "bootstrap_commit": record["bootstrap_commit"],
            "root_handoff_digest": record["root_handoff_digest"],
            "acceptance_commands": record["acceptance_commands"],
            "setup_commands": record.get("setup_commands", []),
            "acceptance_env": record.get("acceptance_env", {}),
            "timeouts": record.get("timeouts", {}),
            "reconfigures": record.get("reconfigures", []),
            "card_entity_id": record.get("card_entity_id", ""),
            "created_at": record.get("created_at", ""),
            "adoptions": [
                adopted.as_dict()
                for adopted in self._load_adoption_ledger(development_id).records()
            ],
            "recoveries": [
                recovery.as_dict()
                for recovery in self._load_recovery_exit(development_id).records()
            ],
        }

    def list(
        self, state: str | None = None, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        """O(n) over the development directories -- the ruled-on trade."""
        ids = (
            sorted(
                entry.name
                for entry in self.root.iterdir()
                if entry.is_dir() and (entry / RECORD_FILE).is_file()
            )
            if self.root.is_dir()
            else []
        )
        if cursor:
            ids = [name for name in ids if name > cursor]
        rows: list[dict[str, Any]] = []
        next_cursor = None
        for name in ids:
            status_path = self._dev_root(name) / STATUS_FILE
            status: dict[str, Any] | None = None
            if status_path.is_file():
                try:
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                except ValueError:
                    status = None
            # A terminal state is immutable, so its cache is trustworthy; a
            # cached "running"/"created" row can be stale the moment the unit
            # exits (measured: a failed run listed as running), so anything
            # non-terminal is recomputed rather than served from the file.
            if status is None or not status.get("terminal"):
                status = self.rebuild_status(name)
            elif "dispatched_by" not in status:
                # A terminal cache written before `dispatched_by` entered the
                # read model carries no provenance; backfill it from the
                # authoritative record (never from worker-run labels) so the
                # row still attributes the development to its dispatching line.
                status = {
                    **status,
                    "dispatched_by": str(self._record(name).get("dispatched_by") or ""),
                }
                write_json_durable(status_path, status)
            if state and status.get("state") != state:
                continue
            rows.append(status)
            if len(rows) >= max(1, limit):
                next_cursor = name
                break
        return {"developments": rows, "cursor": next_cursor}

    def events(
        self,
        development_id: str,
        after: str | None = None,
        limit: int = 100,
        generation: int | None = None,
    ) -> dict[str, Any]:
        record = self._record(development_id)
        current = self._generation(record)
        selected_generation = generation if generation is not None else current
        if not 1 <= selected_generation <= current:
            raise ControlPlaneError(
                "GENERATION_UNKNOWN",
                f"{development_id} has generations 1..{current}, not {selected_generation}",
            )
        path = self._gen_root(development_id, selected_generation) / EVENTS_FILE
        entries: list[dict[str, Any]] = []
        if path.is_file():
            for index, line in enumerate(
                (raw for raw in path.read_text(encoding="utf-8").splitlines() if raw), start=1
            ):
                entries.append({"event_id": f"e{index}", **json.loads(line)})
        threshold = 0
        if after:
            try:
                threshold = int(str(after).lstrip("e"))
            except ValueError as exc:
                raise ControlPlaneError(
                    "EVENT_CURSOR_INVALID", f"after must look like e12, got {after!r}"
                ) from exc
        selected = [e for e in entries if int(e["event_id"].lstrip("e")) > threshold]
        return {
            "development_id": development_id,
            "generation": selected_generation,
            "events": selected[: max(1, limit)],
            "head_event_id": entries[-1]["event_id"] if entries else None,
        }

    def record_gate_refusal(
        self,
        development_id: str,
        *,
        code: str,
        reason: str,
        exit_code: str = "",
    ) -> dict[str, Any]:
        """Durably trace a gate refusal cast on the *delivery* path (M3 S10).

        The decision MCP's dd delivery calls this when a resume's success cannot
        be read back as consumption (or the frozen workspace vanished before any
        unit started). It has two observable effects, both read back by the
        existing read side:

        - an ``events.jsonl`` line carrying ``event: gate_refused`` (so the
          defiance is visible in the append-only trail, not only in systemd
          journal);
        - a ``gate_refused`` fact folded into ``result.json``, which
          ``rebuild_status`` already surfaces -- so ``get()``/``status.json``
          report the refusal instead of the previous "unit died, single never
          changed" hole.

        Best-effort writes: a trace that cannot land never changes the refusal
        itself.
        """
        record = self._record(development_id)
        generation = self._generation(record)
        run_root = self._gen_root(development_id, generation)
        at = iso(self.clock())
        payload = {"code": code, "reason": reason, "exit_code": exit_code, "at": at}

        event_path = run_root / EVENTS_FILE
        with contextlib.suppress(OSError):
            event_path.parent.mkdir(parents=True, exist_ok=True)
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"at": at, "event": "gate_refused", **payload},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )

        result = self._read_result(development_id, generation) or {}
        result.setdefault("development_id", development_id)
        result["gate_refused"] = payload
        with contextlib.suppress(OSError):
            write_json_durable(run_root / RESULT_FILE, result)
        return payload

    # --- evidence --------------------------------------------------------

    def evidence(self, development_id: str) -> dict[str, Any]:
        """Assemble the evidence entries from git + checkpoint + receipts, live.

        Nothing here is a stored summary: the chain digests come from the
        sealed receipt files and the state the checkpoint carries, the
        acceptance and gate records are read out of the commits that carry
        them, and the remote head is resolved now. supervise/audit.py is the
        first consumer, through the same field names it already reads.

        One entry per generation, oldest first, and the receipt chain stays
        continuous across them: generation n's chain seeds on generation
        n-1's tail commit and tail digest (generation 1 seeds on the H0
        handoff), and revisions number cumulatively -- so a rerun appends to
        one auditable chain rather than starting a parallel history.
        """
        record = self._record(development_id)
        repo = Path(str(record["repo_path"]))
        current = self._generation(record)

        entries: list[dict[str, Any]] = []
        seed_commit = str(record["bootstrap_commit"])
        seed_digest = str(record["root_handoff_digest"])
        revision_base = 0
        for generation in range(1, current + 1):
            values = self._checkpoint_state(development_id, generation) or {}
            result = self._read_result(development_id, generation) or {}
            history = list(values.get("history") or result.get("history") or [])
            receipt_digests = dict(values.get("receipt_digests") or {})

            chain = self._receipt_chain(
                record,
                repo,
                history,
                receipt_digests,
                generation=generation,
                seed_commit=seed_commit,
                seed_digest=seed_digest,
                revision_base=revision_base,
            )
            acceptance = next((r for r in chain if r["stage"] == "acceptance"), None)
            gate = self._committed_gate_decision(record, generation)
            merge = self._committed_json(repo, merge_result_path(generation))

            head_commit = str(
                (chain[-1]["output_commit"] if chain else "") or values.get("head_commit") or ""
            )
            ancestor_ok = False
            if head_commit:
                ancestor_ok = (
                    run_git(
                        repo,
                        "merge-base",
                        "--is-ancestor",
                        str(record["target_base_commit"]),
                        head_commit,
                    ).returncode
                    == 0
                )
            remote_verified = self._remote_ref_matches(record, head_commit)
            terminal = str(result.get("terminal") or "")

            bootstrap: dict[str, Any] = {
                "output_commit": seed_commit,
                "receipt_digest": seed_digest,
                "spec_digest": record["spec_digest"],
                "h0": self._h0(development_id) if generation == 1 else None,
            }
            if generation > 1:
                bootstrap["seeded_from_generation"] = generation - 1

            entries.append(
                {
                    "revision": revision_base + len(chain),
                    "generation": generation,
                    "verified": bool(
                        terminal == "complete"
                        and acceptance is not None
                        and remote_verified
                        and ancestor_ok
                    ),
                    "remote_main_verified": remote_verified,
                    "accepted_commit_ancestor": ancestor_ok,
                    "target_base_commit": record["target_base_commit"],
                    "bootstrap": bootstrap,
                    "receipt_chain": chain,
                    "gate": gate,
                    "merge": merge,
                    "terminal": terminal,
                    "failure": self._failure_of(result or None),
                }
            )
            if chain:
                seed_commit = str(chain[-1]["output_commit"])
                if chain[-1]["receipt_digest"]:
                    seed_digest = str(chain[-1]["receipt_digest"])
            revision_base += len(chain)
        return {
            "development_id": development_id,
            "state": self.rebuild_status(development_id)["state"],
            "generation": current,
            "evidence": entries,
            "b3_evidence_chain": self.b3_evidence_chain(development_id),
        }

    def _h0(self, development_id: str) -> dict[str, Any] | None:
        path = self._dev_root(development_id) / H0_FILE
        if not path.is_file():
            return None
        h0 = json.loads(path.read_text(encoding="utf-8"))
        return {"payload": h0, "digest_recomputed": digest_of(canonical_bytes(h0))}

    def _committed_json(self, repo: Path, relative: str) -> dict[str, Any] | None:
        found = run_git(repo, "show", f"HEAD:{relative}")
        if found.returncode != 0:
            return None
        try:
            return dict(json.loads(found.stdout))
        except ValueError:
            return None

    def _remote_ref_matches(self, record: dict[str, Any], head_commit: str) -> bool:
        if not head_commit:
            return False
        listed = run_git(
            Path(str(record["repo_path"])),
            "ls-remote",
            str(record["remote_url"]),
            str(record["remote_ref"]),
        )
        if listed.returncode != 0:
            return False
        heads = [line.split()[0] for line in listed.stdout.splitlines() if line.strip()]
        return bool(heads) and heads[0] == head_commit

    def _receipt_chain(
        self,
        record: dict[str, Any],
        repo: Path,
        history: list[dict[str, Any]],
        receipt_digests: dict[str, str],
        *,
        generation: int = 1,
        seed_commit: str | None = None,
        seed_digest: str | None = None,
        revision_base: int = 0,
    ) -> list[dict[str, Any]]:
        from fleet_graph.dd.dispatch import derive_attempt_id
        from fleet_graph.dd.upstream_constants import compute_json_digest

        development_id = str(record["development_id"])
        state_root = self._gen_root(development_id, generation) / "state"
        chain: list[dict[str, Any]] = []
        previous_output = seed_commit or str(record["bootstrap_commit"])
        previous_digest = seed_digest or str(record["root_handoff_digest"])

        sealed = [
            entry
            for entry in history
            if entry.get("output_commit") and entry.get("event") is not None
        ]
        for revision, entry in enumerate(sealed, start=revision_base + 1):
            stage = str(entry.get("stage") or "")
            output_commit = str(entry.get("output_commit") or "")
            attempt_id = derive_attempt_id(
                development_id, generation, int(entry.get("attempt") or 1)
            )
            receipt, parent_from_receipt, file_digest = self._sealed_receipt(
                state_root, attempt_id, stage, repo, output_commit, generation
            )
            if receipt is None:
                # A script stage with nothing sealed on file reconstructs the
                # WorkspaceSealer receipt it produced -- the exact shape whose
                # canonical digest the next plugin dispatch named as parent.
                receipt = {
                    "stage": stage,
                    "input_commit": previous_output,
                    "output_commit": output_commit,
                }
            # Which digest the *next* link actually names: the sealer re-reads
            # a persisted receipt's exact bytes (dd_materializer.receipt_digest),
            # so a stage with a file on disk chains by its byte digest; a stage
            # with no file chains by the canonical-JSON digest the dispatch
            # builder computes over the in-memory receipt. Measured live on
            # dev-fg-55126095a185: the review receipts name the implement
            # receipt's byte digest, not its canonical one.
            digest = file_digest or receipt_digests.get(stage) or compute_json_digest(receipt)
            chain.append(
                {
                    "revision": revision,
                    "stage": stage,
                    "attempt": int(entry.get("attempt") or 1),
                    "verdict": str(entry.get("event") or ""),
                    "input_commit": previous_output,
                    "output_commit": output_commit,
                    "receipt_digest": digest,
                    "parent_handoff_receipt_digest": parent_from_receipt or previous_digest,
                    # "receipt" means the sealed file attested it;
                    # "derived" means the link is closed by construction and
                    # carries no independent attestation.
                    "parent_source": "receipt" if parent_from_receipt else "derived",
                    "receipt": receipt,
                }
            )
            previous_output = output_commit
            if digest:
                previous_digest = digest
        return chain

    def _sealed_receipt(
        self,
        state_root: Path,
        attempt_id: str,
        stage: str,
        repo: Path,
        output_commit: str,
        generation: int = 1,
    ) -> tuple[dict[str, Any] | None, str, str]:
        """(receipt, its own parent claim, its persisted bytes' digest).

        The byte digest is what a later receipt names as parent -- the sealer
        re-reads exactly those bytes -- so it is returned alongside the parsed
        receipt rather than recomputed from an equivalent object.
        """
        filenames = {
            "implement": "implement-receipt.json",
            "continuous_review": "continuous-review-receipt.json",
            "final_review": "final-review-receipt.json",
        }
        name = filenames.get(stage)
        if name is not None:
            path = state_root / "receipts" / attempt_id / name
            if path.is_file():
                raw = path.read_bytes()
                try:
                    receipt = dict(json.loads(raw.decode("utf-8")))
                except ValueError:
                    receipt = None
                if receipt is not None:
                    crossings = self._scope_crossings(raw.decode("utf-8", errors="replace"))
                    if crossings:
                        receipt["scope_violations"] = crossings
                    return (
                        receipt,
                        str(receipt.get("parent_handoff_receipt_digest") or ""),
                        "sha256:" + hashlib.sha256(raw).hexdigest(),
                    )

        committed = {
            "acceptance": ACCEPTANCE_RECORD_PATH,
            "human_gate": gate_decision_path(generation),
            "merger": merge_result_path(generation),
        }.get(stage)
        if committed and output_commit:
            found = run_git(repo, "show", f"{output_commit}:{committed}")
            if found.returncode == 0:
                try:
                    payload = dict(json.loads(found.stdout))
                except ValueError:
                    payload = None
                if payload is not None:
                    receipt = dict(payload)
                    crossings = self._scope_crossings(found.stdout)
                    if crossings:
                        receipt["scope_violations"] = crossings
                    if stage == "acceptance":
                        # The subject is the tree that carries the frozen
                        # record; the audit checks it out and re-runs exactly
                        # those argvs.
                        receipt["subject_commit"] = output_commit
                        receipt["artifacts"] = [
                            {
                                "path": committed,
                                "digest": "sha256:"
                                + hashlib.sha256(
                                    self._git_show_bytes(repo, f"{output_commit}:{committed}")
                                ).hexdigest(),
                            }
                        ]
                    return receipt, "", ""
        return None, "", ""

    def _git_show_bytes(self, repo: Path, spec: str) -> bytes:
        from fleet_graph.dd.git import git_argv
        from fleet_graph.dd.vendor import git_ops

        proc = subprocess.run(
            git_argv(repo, "show", spec),
            capture_output=True,
            env=git_ops.safe_git_environment(),
            check=False,
        )
        return proc.stdout if proc.returncode == 0 else b""

    # --- B2: automatic adoption and MCP human recovery ---------------------

    def _adoption_path(self, development_id: str) -> Path:
        return self._dev_root(development_id) / ADOPTIONS_FILE

    def _recoveries_path(self, development_id: str) -> Path:
        return self._dev_root(development_id) / RECOVERIES_FILE

    def _load_adoption_ledger(self, development_id: str) -> AdoptionLedger:
        path = self._adoption_path(development_id)
        records: list[AdoptionRecord] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                raw = json.loads(line)
                records.append(
                    AdoptionRecord(
                        signature=str(raw.get("signature") or ""),
                        kind=str(raw.get("kind") or ""),
                        source=str(raw.get("source") or ""),
                        target_ref=str(raw.get("target_ref") or ""),
                        digest=str(raw.get("digest") or ""),
                        sequence=int(raw.get("sequence") or 0),
                        mechanism=str(raw.get("mechanism") or ADOPTION_MECHANISM),
                    )
                )
        return AdoptionLedger(records)

    def _load_recovery_exit(self, development_id: str) -> HumanRecoveryExit:
        path = self._recoveries_path(development_id)
        records: list[RecoveryDecision] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                raw = json.loads(line)
                records.append(
                    RecoveryDecision(
                        target_ref=str(raw.get("target_ref") or ""),
                        decision=str(raw.get("decision") or ""),
                        decided_by=str(raw.get("decided_by") or ""),
                        question_note_id=str(raw.get("question_note_id") or ""),
                        at=str(raw.get("at") or ""),
                        digest=str(raw.get("digest") or ""),
                        mechanism=str(raw.get("mechanism") or RECOVERY_MECHANISM),
                    )
                )
        # The exit's own authenticator is the fail-closed floor; the *real*
        # governance check -- a human decision on the board -- runs in `recover`.
        return HumanRecoveryExit(records=records)

    def adopt(
        self,
        development_id: str,
        discoveries: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Adopt the discovered in-flight/recoverable work automatically (B2).

        ``discoveries`` is a batch of discoveries the caller found in flight:
        ``{signature, kind, source, target_ref}``. The not-yet-adopted subset
        is picked out by ``AdoptionLedger.discover`` and adopted; items already
        on the trail are skipped unchanged. That discovery-then-adopt split is
        the whole "automatic rather than manual bookkeeping" property, and it
        is idempotent: replaying the same batch yields the same sealed records
        and appends nothing -- a replayed discovery cannot duplicate adopted
        work or fork its history.
        """
        self._record(development_id)  # refuses unknown ids before anything else
        ledger = self._load_adoption_ledger(development_id)
        seen: dict[str, tuple[Discovery, str]] = {}
        for item in discoveries or []:
            signature = str(item.get("signature") or "")
            if not signature or signature in seen:
                continue
            seen[signature] = (
                Discovery(
                    signature=signature,
                    kind=str(item.get("kind") or ""),
                    source=str(item.get("source") or ""),
                ),
                str(item.get("target_ref") or ""),
            )
        pending = ledger.discover(discovery for discovery, _ in seen.values())
        adopted: list[dict[str, Any]] = []
        for discovery in pending:
            _, target_ref = seen[discovery.signature]
            try:
                record = ledger.adopt(discovery, target_ref)
            except AdoptionError as exc:
                raise ControlPlaneError("ADOPTION_INVALID", str(exc)) from exc
            adopted.append(record.as_dict())
        if adopted:
            with self._adoption_path(development_id).open("a", encoding="utf-8") as handle:
                for record in adopted:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.rebuild_status(development_id)
        adopted_signatures = {record["signature"] for record in adopted}
        skipped = [signature for signature in seen if signature not in adopted_signatures]
        return {
            "development_id": development_id,
            "adopted": adopted,
            "skipped": skipped,
        }

    def _head_commit(self, record: dict[str, Any]) -> str:
        proc = run_git(Path(str(record["repo_path"])), "rev-parse", "HEAD")
        if proc.returncode != 0:
            raise ControlPlaneError(
                "HEAD_UNRESOLVED",
                f"cannot resolve HEAD in {record['repo_path']}: {proc.stderr.strip()[:200]}",
            )
        return proc.stdout.strip()

    def _governance_decision(self, record: dict[str, Any], question_note_id: str) -> Any:
        """The human decision on the board for one question note, or None.

        This is the governance path the recovery exit delegates authentication
        to: the exit never casts a decision, it reads the one a human already
        put on the board. No board -> no decision -> refuse.
        """
        from fleet_graph.bus.board import GateTicket

        board = self._board_factory()
        if board is None:
            return None
        try:
            ticket = GateTicket(
                question_note_id=question_note_id,
                card_entity_id=str(record.get("card_entity_id") or ""),
            )
            return board.decision_for(ticket)
        except Exception:
            return None

    def recover(
        self,
        development_id: str,
        *,
        target_ref: str = "",
        question_note_id: str = "",
    ) -> dict[str, Any]:
        """Record a human recovery decision and actually resume only from it (B2).

        The MCP surface carries *no verdict*: this tool takes a target and the
        question note, reads the human's decision off the board (the governance
        path), seals it -- with its immutable target reference and a digest --
        into the append-only recovery trail, and then *actually* relaunches or
        re-enters the suspended thread from that recorded decision alone. No
        board decision -> refuse, so the recovery can never become a bypass
        around the gate.

        The resume is truthful: it reports whether a launch occurred (or the
        thread was already running and mechanically identified), and any raw
        launch failure -- it never fabricates ``resumed=true``. A re-invocation
        for an already-recorded target re-uses the existing sealed record and
        does not create a duplicate live thread.
        """
        record = self._record(development_id)
        if not target_ref:
            target_ref = self._head_commit(record)

        exit_ = self._load_recovery_exit(development_id)
        existing = exit_.recorded_for(target_ref)
        if existing is None:
            decision = self._governance_decision(record, question_note_id)
            if decision is None:
                raise ControlPlaneError(
                    "HUMAN_DECISION_MISSING",
                    "human recovery needs the decision for this question note on the "
                    "board (work.decision.v1); the exit cannot cast a decision itself",
                )
            try:
                sealed = exit_.record(
                    target_ref=target_ref,
                    decision=str(getattr(decision, "decision", "") or ""),
                    decided_by=str(getattr(decision, "decided_by", "") or ""),
                    question_note_id=question_note_id,
                    at=iso(self.clock()),
                )
            except RecoveryError as exc:
                raise ControlPlaneError("RECOVERY_REFUSED", str(exc)) from exc
            with self._recoveries_path(development_id).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sealed.as_dict(), ensure_ascii=False) + "\n")
        else:
            # Replayed recovery: the immutable record already attests to this
            # decision, so it is re-used rather than re-sealed. Sealing twice
            # would fork the trail; launching again would duplicate a live thread.
            sealed = existing

        resume = self._resume_recovery(record, target_ref)
        self.rebuild_status(development_id)
        return {
            "development_id": development_id,
            "recovery": sealed.as_dict(),
            "resume": resume,
        }

    def _resume_recovery(self, record: dict[str, Any], target_ref: str) -> dict[str, Any]:
        """Actually relaunch/re-enter the suspended thread; truthful fields only.

        ``record`` must already carry an authenticated, target-bound recovery
        record for ``target_ref`` (the caller seals or re-reads one before
        calling here). ``resumed`` is True only when a real launch occurred or
        the same thread is already running and mechanically identified; a
        launch failure is returned as ``resumed=False`` with the raw detail,
        never dressed up as success.
        """
        development_id = str(record["development_id"])
        generation = self._generation(record)
        thread_id = f"{development_id}:g{generation}"
        checkpoint = str(self._dev_root(development_id) / CHECKPOINT_FILE)

        active = self._unit_active(development_id)
        if active:
            return {
                "resumed": True,
                "launched": False,
                "already_running": True,
                "unit": active,
                "thread_id": thread_id,
                "generation": generation,
                "checkpoint": checkpoint,
            }
        try:
            launched = self._launch(record, resume=True, generation=generation)
        except ControlPlaneError as exc:
            if exc.code == "LAUNCH_FAILED":
                return {
                    "resumed": False,
                    "launched": False,
                    "already_running": False,
                    "thread_id": thread_id,
                    "generation": generation,
                    "checkpoint": checkpoint,
                    "launch_failure": exc.detail,
                }
            raise
        return {
            "resumed": bool(launched["started"]),
            "launched": bool(launched["started"]),
            "already_running": bool(launched.get("already_running")),
            "unit": launched["unit"],
            "thread_id": launched["thread_id"],
            "generation": launched["generation"],
            "checkpoint": launched["checkpoint"],
            "mode": launched["mode"],
        }

    def recoveries(self, development_id: str) -> dict[str, Any]:
        """The sealed recovery decisions on record for one development."""
        self._record(development_id)
        return {
            "development_id": development_id,
            "recoveries": [
                record.as_dict() for record in self._load_recovery_exit(development_id).records()
            ],
        }

    def adoptions(self, development_id: str) -> dict[str, Any]:
        """The adopted work items on record for one development."""
        self._record(development_id)
        return {
            "development_id": development_id,
            "adoptions": [
                record.as_dict() for record in self._load_adoption_ledger(development_id).records()
            ],
        }

    def b3_evidence_chain(self, development_id: str) -> dict[str, Any]:
        """Assemble and validate the B3 phenomenon->mechanism->evidence chain.

        The links are built from the real artifacts the behaviours produced --
        the persisted scope verdict, the adoption trail, and the recovery trail
        -- not from re-typed claims. Each link's ``evidence_mechanism`` is read
        off the artifact itself (its recorded ``mechanism`` / ``rule_id``), so
        a substituted unrelated event is a mismatch the validator names, rather
        than something a test author could satisfy by spelling the same literal
        twice.
        """
        from fleet_graph.dd.upstream_constants import compute_json_digest

        record = self._record(development_id)
        links: list[EvidenceLink] = []

        scope_verdict = record.get("scope_verdict") or {}
        if scope_verdict.get("admitted"):
            links.append(
                EvidenceLink(
                    kind=KIND_SCOPE,
                    phenomenon="a spec that actively crosses the boundary is refused",
                    mechanism=RULE_ID,
                    evidence_mechanism=str(scope_verdict.get("rule_id") or ""),
                    subject_ref=str(record.get("spec_digest") or ""),
                    digest=compute_json_digest(scope_verdict),
                )
            )

        for adopted in self._load_adoption_ledger(development_id).records():
            links.append(
                EvidenceLink(
                    kind=KIND_ADOPTION,
                    phenomenon="replaying the same discovery yields one adopted record",
                    mechanism=ADOPTION_MECHANISM,
                    evidence_mechanism=adopted.mechanism,
                    subject_ref=adopted.target_ref,
                    digest=adopted.digest,
                )
            )

        for recovery in self._load_recovery_exit(development_id).records():
            links.append(
                EvidenceLink(
                    kind=KIND_HUMAN_RECOVERY,
                    phenomenon="suspended work resumes only from a recorded decision",
                    mechanism=RECOVERY_MECHANISM,
                    evidence_mechanism=recovery.mechanism,
                    subject_ref=recovery.target_ref,
                    digest=recovery.digest,
                )
            )

        chain = EvidenceChain(tuple(links))
        reasons = chain.validate()
        return {
            "development_id": development_id,
            "valid": not reasons,
            "reasons": list(reasons),
            "links": [
                {
                    "kind": link.kind,
                    "phenomenon": link.phenomenon,
                    "mechanism": link.mechanism,
                    "evidence_mechanism": link.evidence_mechanism,
                    "subject_ref": link.subject_ref,
                    "digest": link.digest,
                }
                for link in chain.links
            ],
        }


__all__ = [
    "ACCEPTANCE_FENCE",
    "CHECKPOINT_FILE",
    "CLASS_ENVIRONMENT_CONTRACT",
    "CLASS_FABRICATION",
    "CLASS_IMPLEMENTATION",
    "CLASS_REJECTED",
    "DEFAULT_DD_ROOT",
    "DEFAULT_EXECUTABLE",
    "DEFAULT_PLUGIN_BINDING",
    "DEFAULT_WORKING_DIRECTORY",
    "DEFAULT_WORKTREE_ROOTS",
    "EVENTS_FILE",
    "EXIT_NONE",
    "EXIT_RECONFIGURE",
    "EXIT_REWORK",
    "FABRICATION_CODES",
    "FAULT_CLASSES",
    "H0_FILE",
    "IMPLEMENTATION_CODES",
    "LAUNCHES_FILE",
    "RECORD_FILE",
    "REJECTION_CODES",
    "RESULT_FILE",
    "STATUS_FILE",
    "UNIT_PREFIX",
    "ControlPlaneError",
    "DdControlPlane",
    "DdLaunchSpec",
    "build_h0_handoff",
    "classify_failure",
    "derive_acceptance_commands",
    "derive_development_id",
    "gate_decision_path",
    "merge_result_path",
]
