"""Mechanised audit of one development or one goal line. The human still decides.

This is r4-design §2's `gather_evidence` + `rerun_acceptance`, runnable by
hand: every check a supervisor was doing with a scratchpad, executed as
commands whose exit codes go in the report. Three disciplines are load-bearing:

- **The target base is recomputed from git, never read from the agent's own
  account** -- the identity file is read at the bootstrap commit the receipt
  chain starts from (falling back to `dd/bootstrap.py`'s introducing-commit
  anchor), and edits since bootstrap are a refusal.
- **The re-run executes the frozen argv from the receipt-bound artifact, on a
  one-shot detached worktree, with no existence guards.** A missing file is a
  red result, not a skip; zero frozen commands is a failure; the worktree is
  removed on every path out (§38f/§39b/§39c). When the receipt froze no
  artifact at all (old-engine evidence), the argv falls back -- in order -- to
  the acceptance `record_json` command_results, then the union of the chain's
  `verification_record.verification_commands`; both are the audited party's
  own account, so the assertion name and the report mark the degradation
  explicitly. All sources empty is still a failure.
- **Reads only.** The old engine is consulted over GET; git is driven through
  `dd/git.py`'s guarded argv because the worktrees involved were written by
  agents. The single write this module can perform is an `evidence` note to
  the board, and publishing a decision stays structurally impossible.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from fleet_graph.bus.board import NOTE_KIND, WORK_NOTES
from fleet_graph.bus.client import BusClient, PublishResult
from fleet_graph.dd.bootstrap import DEVELOPMENT_PATH, IdentityChanged, committed_target_base
from fleet_graph.dd.git import run_git
from fleet_graph.dd.vendor import git_ops

DEFAULT_ENGINE_URL = "http://127.0.0.1:7460"
DEFAULT_RUN_ROOT = Path("/data/fleet-graph/runs")

# Paths that are dispatch metadata rather than the product being graded.
METADATA_PREFIXES = (".dev-dispatch/", ".dd-evidence/")

COMMAND_TIMEOUT_SECONDS = 1800
TAIL = 2000

# Synthetic exit codes for commands that never got to return one, following
# shell convention: 124 timeout, 127 command not found.
EXIT_TIMEOUT = 124
EXIT_NOT_FOUND = 127

TERMINAL_VOCABULARY = frozenset({"done", "blocked", "bounds", "fault", "killed"})


class AuditError(RuntimeError):
    """The audit could not run as asked (unreachable engine, bad target)."""


@dataclass(frozen=True)
class Assertion:
    """One mechanical check: what ran, what it returned, what that means."""

    name: str
    ok: bool
    command: str
    exit_code: int
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "command": self.command,
            "exit_code": self.exit_code,
            "detail": self.detail,
        }


@dataclass
class AuditReport:
    target: str
    kind: str  # "development" | "goal_line"
    assertions: list[Assertion] = field(default_factory=list)
    acceptance_results: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    # Set after a successful publish; not part of the audit verdict itself.
    evidence_note_id: str = ""

    @property
    def ok(self) -> bool:
        return all(a.ok for a in self.assertions)

    def record(self, assertion: Assertion) -> Assertion:
        self.assertions.append(assertion)
        return assertion

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "kind": self.kind,
            "ok": self.ok,
            "assertions": [a.as_dict() for a in self.assertions],
            "acceptance_results": self.acceptance_results,
            "gaps": self.gaps,
            "evidence_note_id": self.evidence_note_id,
        }

    def fingerprint(self) -> str:
        """Content fingerprint for the idempotency key. Excludes the note id:
        the same findings must dedup to the same note across re-runs."""
        content = {k: v for k, v in self.as_dict().items() if k != "evidence_note_id"}
        payload = json.dumps(content, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class EvidenceSource(Protocol):
    """What the audit needs from the old engine. GETs only, by construction."""

    def development(self, development_id: str) -> dict[str, Any]: ...

    def evidence(self, development_id: str) -> dict[str, Any]: ...


class OldEngineClient:
    """Read-only client for the legacy controller (127.0.0.1:7460).

    There is deliberately no method here that can POST: the R4 discipline is
    that the supervision face never writes into the thing it is auditing.
    """

    def __init__(self, base_url: str = DEFAULT_ENGINE_URL, *, timeout: float = 30.0) -> None:
        import httpx

        # trust_env=False for the same reason as bus/client.py: loopback
        # traffic must not be routed through the host's SOCKS proxy.
        self._client = httpx.Client(timeout=timeout, trust_env=False)
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str) -> dict[str, Any]:
        response = self._client.get(f"{self.base_url}{path}")
        if response.status_code != 200:
            raise AuditError(f"GET {path} -> HTTP {response.status_code}: {response.text[:300]}")
        return response.json()

    def development(self, development_id: str) -> dict[str, Any]:
        return self._get(f"/v1/developments/{development_id}")

    def evidence(self, development_id: str) -> dict[str, Any]:
        return self._get(f"/v1/developments/{development_id}/evidence")


class GraphEngineSource:
    """EvidenceSource over the new engine's in-process control plane.

    Reads only: `get` recomputes status from run artifacts and `evidence`
    assembles the entry live from git + checkpoint + receipts. The evidence
    entry names no bootstrap-era `receipt_digest` chain the audit does not
    already understand -- the field names are the ones `audit_development`
    reads, and the fleet-graph-native fallbacks (`_check_identity_native_anchor`,
    `results[].command` frozen argvs) cover the shapes that differ.
    """

    def __init__(self, plane: Any = None) -> None:
        if plane is None:
            from fleet_graph.dd.control_plane import DdControlPlane

            plane = DdControlPlane()
        self._plane = plane

    def development(self, development_id: str) -> dict[str, Any]:
        return self._plane.get(development_id)

    def evidence(self, development_id: str) -> dict[str, Any]:
        return self._plane.evidence(development_id)


# --- development audit ----------------------------------------------------


def _git_assertion(
    report: AuditReport, name: str, repo: Path, *args: str, ok_detail: str
) -> tuple[Assertion, str]:
    proc = run_git(repo, *args)
    ok = proc.returncode == 0
    detail = ok_detail if ok else (proc.stderr or proc.stdout).strip()[:400]
    assertion = Assertion(
        name=name,
        ok=ok,
        command=f"git -C {repo} {' '.join(args)}",
        exit_code=proc.returncode,
        detail=detail,
    )
    report.record(assertion)
    return assertion, proc.stdout


def _git_show_bytes(repo: Path, spec: str) -> tuple[int, bytes]:
    """`git show` with byte-exact output -- digests are over bytes, not text."""
    from fleet_graph.dd.git import git_argv

    proc = subprocess.run(
        git_argv(repo, "show", spec),
        capture_output=True,
        env=git_ops.safe_git_environment(),
    )
    return proc.returncode, proc.stdout


def _pick_evidence_entry(payload: dict[str, Any]) -> dict[str, Any] | None:
    entries = payload.get("evidence") or []
    if not entries:
        return None
    return max(entries, key=lambda entry: entry.get("revision", 0))


def _check_receipt_chain(report: AuditReport, development_id: str, entry: dict[str, Any]) -> None:
    source = f"GET /v1/developments/{development_id}/evidence"
    chain = sorted(entry.get("receipt_chain") or [], key=lambda r: r.get("revision", 0))
    bootstrap_digest = str((entry.get("bootstrap") or {}).get("receipt_digest") or "")

    breaks: list[str] = []
    expected = bootstrap_digest
    previous: dict[str, Any] | None = None
    for record in chain:
        parent = str(record.get("parent_handoff_receipt_digest") or "")
        if parent != expected:
            breaks.append(
                f"rev{record.get('revision')} {record.get('stage')}: parent "
                f"{parent[:18]}… != expected {expected[:18]}…"
            )
        expected = str(record.get("receipt_digest") or "")
        if previous is not None and record.get("input_commit") != previous.get("output_commit"):
            breaks.append(
                f"rev{record.get('revision')} {record.get('stage')}: input "
                f"{str(record.get('input_commit'))[:12]} != prior output "
                f"{str(previous.get('output_commit'))[:12]}"
            )
        previous = record

    report.record(
        Assertion(
            name="receipt_chain_linked",
            ok=not breaks and bool(chain),
            command=source,
            exit_code=200,
            detail=(
                f"{len(chain)} 段 receipt 链自 bootstrap digest 起逐段闭合，commit 逐段连续"
                if chain and not breaks
                else ("; ".join(breaks) if breaks else "receipt_chain 为空")
            ),
        )
    )


def _acceptance_receipt(entry: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        record for record in entry.get("receipt_chain") or [] if record.get("stage") == "acceptance"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.get("revision", 0))


def _frozen_argvs(frozen: dict[str, Any]) -> tuple[list[list[str]], int]:
    """The frozen command list, plus how many entries were skips.

    Two shapes exist: the old engine's `.dd-evidence/acceptance.json` uses
    `command_results[].argv`, fleet-graph's own AcceptanceStage writes
    `results[].command`. Anything marked skipped counts against the audit --
    a skip in the frozen record means that command was never actually graded.
    """
    entries = frozen.get("command_results")
    key = "argv"
    if entries is None:
        entries = frozen.get("results")
        key = "command"
    argvs: list[list[str]] = []
    skipped = 0
    for record in entries or []:
        if record.get("skipped"):
            skipped += 1
            continue
        argv = record.get(key)
        if isinstance(argv, list) and argv:
            argvs.append([str(part) for part in argv])
    return argvs, skipped


def _run_frozen_commands(worktree: Path, argvs: list[list[str]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for argv in argvs:
        # No `[ -f ] &&` guards, ever: if the frozen command names a file that
        # is not in this tree, the command itself goes red, which is the answer.
        try:
            proc = subprocess.run(
                argv,
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
            exit_code, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
        except FileNotFoundError as exc:
            exit_code, stdout, stderr = EXIT_NOT_FOUND, "", f"command not found: {exc}"
        except subprocess.TimeoutExpired:
            exit_code, stdout, stderr = (
                EXIT_TIMEOUT,
                "",
                f"timed out after {COMMAND_TIMEOUT_SECONDS}s",
            )
        results.append(
            {
                "command": argv,
                "exit_code": exit_code,
                "stdout_tail": stdout[-TAIL:],
                "stderr_tail": stderr[-TAIL:],
            }
        )
    return results


def audit_development(
    development_id: str,
    *,
    engine: EvidenceSource,
    repo: Path,
) -> AuditReport:
    """Run the whole §37d checklist against one development, mechanically."""
    report = AuditReport(target=development_id, kind="development")
    evidence_path = f"GET /v1/developments/{development_id}/evidence"

    detail = engine.development(development_id)
    evidence_payload = engine.evidence(development_id)
    entry = _pick_evidence_entry(evidence_payload)
    report.record(
        Assertion(
            name="evidence_present",
            ok=entry is not None,
            command=evidence_path,
            exit_code=200,
            detail=(
                f"state={detail.get('state')} revision={entry.get('revision')}"
                if entry is not None
                else "evidence 列表为空"
            ),
        )
    )
    if entry is None:
        return report

    # verified 是控制面的推导位：terminal==complete AND acceptance AND
    # remote_verified AND ancestor（dd/control_plane.py）。它在 gate 放行前
    # 构造性恒 False——把 "verified is True" 当放行前置会让第四道闸结构性
    # 不可达（e1-msg_01M120E6FV13238KW42WPWHRMF 实锤：15/16 绿仅此一红）。
    # 断言改为与 state 的推导一致性：完成态要求位已立；未完成态要求
    # 可先真的三个构成件全真、且位诚实地还没立（此时 verified=True 反而
    # 说明推导被篡改或读错了库）。
    # 未完成家族是封闭枚举（graph 引擎 + 老引擎两套态名都显式列出）；名单外
    # 的任何 state 一律按完成态要求位已立——未知态 fail-closed，不放行。
    state = str(detail.get("state") or "")
    pre_completion = state in {
        "created",
        "running",
        "awaiting_gate",
        "interrupted",
        "AWAITING_GATE",
        "REVIEWING_CONTINUOUS",
        "IMPLEMENTING",
    }
    constituents_ok = bool(entry.get("remote_main_verified")) and bool(
        entry.get("accepted_commit_ancestor")
    )
    if pre_completion:
        verified_consistent = constituents_ok and entry.get("verified") is not True
    else:
        verified_consistent = entry.get("verified") is True
    report.record(
        Assertion(
            name="verified_bit",
            ok=verified_consistent,
            command=evidence_path,
            exit_code=200,
            detail=(
                f"state={state} verified={entry.get('verified')} "
                f"remote_main_verified={entry.get('remote_main_verified')} "
                f"accepted_commit_ancestor={entry.get('accepted_commit_ancestor')}"
            ),
        )
    )
    _check_receipt_chain(report, development_id, entry)

    acceptance = _acceptance_receipt(entry)
    report.record(
        Assertion(
            name="acceptance_receipt_present",
            ok=acceptance is not None,
            command=evidence_path,
            exit_code=200,
            detail=(
                f"acceptance verdict={acceptance.get('verdict')} rev={acceptance.get('revision')}"
                if acceptance is not None
                else "receipt_chain 中没有 acceptance receipt"
            ),
        )
    )
    if acceptance is None:
        return report

    subject_commit = str(acceptance["receipt"].get("subject_commit") or "")
    evidence_commit = str(acceptance.get("output_commit") or "")
    subject_ok, _ = _git_assertion(
        report,
        "accepted_commit_in_git",
        repo,
        "cat-file",
        "-e",
        f"{subject_commit}^{{commit}}",
        ok_detail=f"acceptance subject {subject_commit[:12]} 在 {repo} 中可解析",
    )
    if not subject_ok.ok:
        report.gaps.append(
            f"{repo} 没有 acceptance subject commit {subject_commit[:12]}；"
            "先 git fetch 对应 durable ref 再重跑审计"
        )
        return report

    claimed_base = str(detail.get("target_base_commit") or entry.get("target_base_commit") or "")
    tmp_root = Path(tempfile.mkdtemp(prefix="fg-supervise-audit-"))
    worktree = tmp_root / "worktree"
    try:
        added, _ = _git_assertion(
            report,
            "throwaway_worktree_added",
            repo,
            "worktree",
            "add",
            "--detach",
            str(worktree),
            subject_commit,
            ok_detail=f"detached worktree @ {subject_commit[:12]}（一次性，审后即除）",
        )
        if not added.ok:
            return report

        bootstrap_commit = str((entry.get("bootstrap") or {}).get("output_commit") or "")
        _check_identity_and_base(
            report, worktree, development_id, claimed_base, bootstrap_commit=bootstrap_commit
        )
        _git_assertion(
            report,
            "target_base_is_ancestor",
            repo,
            "merge-base",
            "--is-ancestor",
            claimed_base,
            subject_commit,
            ok_detail=f"target base {claimed_base[:12]} 是 subject 的祖先（git 现算）",
        )
        _check_diff_manifest(report, repo, claimed_base, subject_commit)
        _check_worktree_binding(report, detail, development_id)
        _rerun_acceptance(report, repo, worktree, entry, acceptance, evidence_commit)
    finally:
        removed = run_git(repo, "worktree", "remove", "--force", str(worktree))
        if removed.returncode != 0:
            # Belt and braces: the tree must not outlive the audit even if
            # git's bookkeeping refuses (§38f).
            shutil.rmtree(worktree, ignore_errors=True)
            run_git(repo, "worktree", "prune")
        shutil.rmtree(tmp_root, ignore_errors=True)
    return report


def _check_identity_and_base(
    report: AuditReport,
    worktree: Path,
    development_id: str,
    claimed_base: str,
    *,
    bootstrap_commit: str = "",
) -> None:
    """Identity binding and target base, both anchored in git history.

    The anchor is the bootstrap commit the receipt chain starts from: its
    identity file is what every digest downstream chains over, so the graded
    party can rewrite the working copy but not that commit. On old-engine
    repos many developments share one lineage and each bootstrap rewrites the
    identity file in place, so `--diff-filter=A` (the fleet-graph-native
    anchor `committed_target_base` uses) finds someone *else's* introduction
    -- measured on adl0_b1, whose oldest A-commit belongs to adl0_m2a2. When
    the evidence names no bootstrap commit, the native anchor is the fallback.
    """
    if not bootstrap_commit:
        _check_identity_native_anchor(report, worktree, development_id, claimed_base)
        return

    anchored, _ = _git_assertion(
        report,
        "bootstrap_anchor_in_history",
        worktree,
        "merge-base",
        "--is-ancestor",
        bootstrap_commit,
        "HEAD",
        ok_detail=f"bootstrap commit {bootstrap_commit[:12]}（receipt 链起点）是 subject 的祖先",
    )
    show_anchor = f"git -C {worktree} show {bootstrap_commit}:{DEVELOPMENT_PATH}"
    anchor_proc = run_git(worktree, "show", f"{bootstrap_commit}:{DEVELOPMENT_PATH}")
    anchor_identity: dict[str, Any] = {}
    if anchored.ok and anchor_proc.returncode == 0:
        try:
            anchor_identity = json.loads(anchor_proc.stdout)
        except ValueError:
            anchor_identity = {}

    recomputed = str(anchor_identity.get("target_base_commit") or "")
    report.record(
        Assertion(
            name="target_base_recomputed",
            ok=bool(recomputed) and recomputed == claimed_base,
            command=show_anchor,
            exit_code=anchor_proc.returncode,
            detail=(
                f"git 现算（bootstrap commit 内识别文件）target base="
                f"{recomputed[:12] or '<无法读取>'}，引擎自述={claimed_base[:12]}"
            ),
        )
    )
    bound_id = str(anchor_identity.get("development_id") or "")
    report.record(
        Assertion(
            name="identity_binding",
            ok=bool(bound_id) and bound_id == development_id,
            command=show_anchor,
            exit_code=anchor_proc.returncode,
            detail=(
                f"{DEVELOPMENT_PATH} @ bootstrap 绑定 development_id="
                f"{bound_id or '<无法读取>'}，被审对象={development_id}"
            ),
        )
    )

    head_proc = run_git(worktree, "show", f"HEAD:{DEVELOPMENT_PATH}")
    report.record(
        Assertion(
            name="identity_unedited_since_bootstrap",
            ok=head_proc.returncode == 0 and head_proc.stdout == anchor_proc.stdout,
            command=f"git -C {worktree} show HEAD:{DEVELOPMENT_PATH}",
            exit_code=head_proc.returncode,
            detail=(
                "subject 树中的识别文件与 bootstrap commit 逐字节一致"
                if head_proc.returncode == 0 and head_proc.stdout == anchor_proc.stdout
                else "识别文件在 bootstrap 之后被改写——拒绝采信其内容"
            ),
        )
    )


def _check_identity_native_anchor(
    report: AuditReport, worktree: Path, development_id: str, claimed_base: str
) -> None:
    """Fallback for evidence that names no bootstrap commit (fleet-graph dd)."""
    show = f"git -C {worktree} show HEAD:{DEVELOPMENT_PATH}"
    try:
        recomputed = committed_target_base(worktree)
    except IdentityChanged as changed:
        report.record(
            Assertion(
                name="target_base_recomputed",
                ok=False,
                command=show,
                exit_code=1,
                detail=str(changed)[:400],
            )
        )
    else:
        report.record(
            Assertion(
                name="target_base_recomputed",
                ok=recomputed is not None and recomputed == claimed_base,
                command=show,
                exit_code=0,
                detail=(
                    f"git 现算 target base={str(recomputed)[:12]}，引擎自述={claimed_base[:12]}"
                ),
            )
        )

    identity_proc = run_git(worktree, "show", f"HEAD:{DEVELOPMENT_PATH}")
    bound_id = ""
    if identity_proc.returncode == 0:
        try:
            bound_id = str(json.loads(identity_proc.stdout).get("development_id") or "")
        except ValueError:
            bound_id = ""
    report.record(
        Assertion(
            name="identity_binding",
            ok=bool(bound_id) and bound_id == development_id,
            command=show,
            exit_code=identity_proc.returncode,
            detail=(
                f"{DEVELOPMENT_PATH} 绑定 development_id={bound_id or '<无法读取>'}，"
                f"被审对象={development_id}"
            ),
        )
    )


def _check_diff_manifest(report: AuditReport, repo: Path, base: str, subject: str) -> None:
    proc = run_git(repo, "diff", "--name-only", f"{base}..{subject}")
    command = f"git -C {repo} diff --name-only {base[:12]}..{subject[:12]}"
    if proc.returncode != 0:
        report.record(
            Assertion(
                name="diff_manifest",
                ok=False,
                command=command,
                exit_code=proc.returncode,
                detail=(proc.stderr or proc.stdout).strip()[:400],
            )
        )
        return
    files = [line for line in proc.stdout.splitlines() if line.strip()]
    metadata = [f for f in files if f.startswith(METADATA_PREFIXES)]
    product = [f for f in files if not f.startswith(METADATA_PREFIXES)]
    report.record(
        Assertion(
            name="diff_manifest",
            ok=True,
            command=command,
            exit_code=0,
            detail=(
                f"共 {len(files)} 个文件：产品 {len(product)}、dispatch 元数据 {len(metadata)}。"
                f"产品清单: {', '.join(product[:20])}" + ("…" if len(product) > 20 else "")
            ),
        )
    )


def _check_worktree_binding(
    report: AuditReport, detail: dict[str, Any], development_id: str
) -> None:
    """If the engine's worktree is still on disk, it must be bound to *this*
    development -- §19's green-tests-in-someone-else's-worktree failure."""
    worktree_path = str(detail.get("worktree_path") or "")
    command = f"git -C {worktree_path} show HEAD:{DEVELOPMENT_PATH}"
    if not worktree_path or not Path(worktree_path).is_dir():
        report.record(
            Assertion(
                name="worktree_binding",
                ok=True,
                command=command or "(engine 未报 worktree_path)",
                exit_code=0,
                detail="引擎 worktree 已回收；身份绑定由 identity_binding（git 内锚定）覆盖",
            )
        )
        return
    proc = run_git(Path(worktree_path), "show", f"HEAD:{DEVELOPMENT_PATH}")
    bound_id = ""
    if proc.returncode == 0:
        try:
            bound_id = str(json.loads(proc.stdout).get("development_id") or "")
        except ValueError:
            bound_id = ""
    report.record(
        Assertion(
            name="worktree_binding",
            ok=bound_id == development_id,
            command=command,
            exit_code=proc.returncode,
            detail=f"在盘 worktree 绑定 {bound_id or '<无法读取>'}，被审对象 {development_id}",
        )
    )


def _fallback_frozen_argvs(
    entry: dict[str, Any],
) -> tuple[str, str, list[list[str]], int, str] | None:
    """Self-attested argv sources, tried in order, for when the acceptance
    receipt froze no artifact (old-engine evidence has no `artifacts` field).

    Returns (assertion_name, command, argvs, skipped, detail) or None if every
    source is empty. Both sources live inside the audited party's own evidence
    chain: using them is an explicit degradation -- the audit re-runs what the
    receipts *say* ran instead of an independently frozen record, and the
    report must say so (the assertion name carries the source, the caller adds
    the degradation gap). It never upgrades the audit to first-hand freezing.
    """
    # Tier 2: the controller-persisted acceptance record (old engine
    # `acceptances[].record_json`, whose inner shape is command_results[].argv).
    for acceptance in reversed(list(entry.get("acceptances") or [])):
        raw = acceptance.get("record_json")
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            continue
        argvs, skipped = _frozen_argvs(record)
        if argvs or skipped:
            return (
                "frozen_acceptance_from_record_json",
                "GET evidence → acceptances[].record_json → command_results[].argv",
                argvs,
                skipped,
                f"receipt 无 artifacts；argv 兜底取自 acceptance record_json 的 "
                f"command_results（{len(argvs)} 条）——被审方自述链，非一手冻结",
            )

    # Tier 3: union (order-preserving, deduped) of every receipt's
    # verification_record.verification_commands argv across the chain.
    argvs = []
    seen: set[tuple[str, ...]] = set()
    for record in entry.get("receipt_chain") or []:
        verification = (record.get("receipt") or {}).get("verification_record") or {}
        for cmd in verification.get("verification_commands") or []:
            argv = cmd.get("argv") if isinstance(cmd, dict) else None
            if isinstance(argv, list) and argv:
                normalized = [str(part) for part in argv]
                key = tuple(normalized)
                if key not in seen:
                    seen.add(key)
                    argvs.append(normalized)
    if argvs:
        return (
            "frozen_acceptance_from_verification_record",
            "GET evidence → receipt_chain[].receipt"
            ".verification_record.verification_commands[].argv",
            argvs,
            0,
            f"receipt 无 artifacts 且无可用 record_json；argv 兜底取自 receipt_chain "
            f"各 verification_record 的并集去重（{len(argvs)} 条）——被审方自述链，非一手冻结",
        )
    return None


def _rerun_acceptance(
    report: AuditReport,
    repo: Path,
    worktree: Path,
    entry: dict[str, Any],
    acceptance: dict[str, Any],
    evidence_commit: str,
) -> None:
    artifacts = (acceptance.get("receipt") or {}).get("artifacts") or []
    frozen_ref = next(
        (a for a in artifacts if str(a.get("path", "")).endswith("acceptance.json")),
        artifacts[0] if artifacts else None,
    )
    source_note = ""
    if frozen_ref is not None:
        frozen_path = str(frozen_ref.get("path") or "")
        spec = f"{evidence_commit}:{frozen_path}"
        returncode, blob = _git_show_bytes(repo, spec)
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        declared = str(frozen_ref.get("digest") or "")
        report.record(
            Assertion(
                name="frozen_acceptance_digest",
                ok=returncode == 0 and digest == declared,
                command=f"git -C {repo} show {spec} | sha256sum",
                exit_code=returncode,
                detail=f"现算 {digest[:24]}… vs receipt 声明 {declared[:24]}…",
            )
        )
        if returncode != 0:
            # A declared-but-unreadable (or tampered) frozen artifact is a hard
            # red -- the fallback sources are only for evidence that never
            # froze an artifact at all.
            return

        try:
            frozen = json.loads(blob.decode("utf-8"))
        except ValueError:
            report.record(
                Assertion(
                    name="acceptance_rerun",
                    ok=False,
                    command=f"git -C {repo} show {spec}",
                    exit_code=1,
                    detail="冻结的 acceptance 文件不是合法 JSON",
                )
            )
            return

        argvs, skipped = _frozen_argvs(frozen)
        source_command = f"git -C {repo} show {spec}"
    else:
        fallback = _fallback_frozen_argvs(entry)
        if fallback is None:
            report.record(
                Assertion(
                    name="frozen_acceptance_digest",
                    ok=False,
                    command="(acceptance receipt artifacts)",
                    exit_code=1,
                    detail=(
                        "acceptance receipt 没有 artifacts，record_json 与 "
                        "verification_record 兜底源也全空——冻结 argv 无处可取"
                    ),
                )
            )
            return
        name, source_command, argvs, skipped, detail = fallback
        report.record(
            Assertion(name=name, ok=True, command=source_command, exit_code=200, detail=detail)
        )
        source_note = "（argv 兜底取自 receipt 自述，非一手冻结）"
        report.gaps.append(
            f"frozen argv 降级来源（{name}）：复跑命令取自被审方自述链，"
            "复跑结果仍是机械事实，但 audit 不因此升格为一手验证"
        )

    report.record(
        Assertion(
            name="acceptance_no_skips",
            ok=skipped == 0,
            command=source_command,
            exit_code=0,
            detail=(
                f"冻结记录 {len(argvs)} 条命令，0 条 SKIP"
                if skipped == 0
                else f"冻结记录含 {skipped} 条 SKIP——SKIP 显式计为失败"
            ),
        )
    )
    if not argvs:
        report.record(
            Assertion(
                name="acceptance_rerun",
                ok=False,
                command=source_command,
                exit_code=1,
                detail="冻结记录里没有任何可执行的 acceptance 命令：零条命令 = 失败",
            )
        )
        return

    results = _run_frozen_commands(worktree, argvs)
    report.acceptance_results.extend(results)
    failed = [r for r in results if r["exit_code"] != 0]
    report.record(
        Assertion(
            name="acceptance_rerun",
            ok=not failed,
            command="; ".join(" ".join(r["command"]) for r in results)[:400],
            exit_code=max((r["exit_code"] for r in results), default=1),
            detail=(
                f"{len(results)} 条冻结 argv 在一次性 worktree 上全绿{source_note}"
                if not failed
                else f"{len(failed)}/{len(results)} 条失败: "
                + "; ".join(" ".join(r["command"])[:80] for r in failed)
                + source_note
            ),
        )
    )


# --- goal line audit ------------------------------------------------------


def audit_goal_line(folder_id: str, *, run_root: Path) -> AuditReport:
    """Mechanical read of one goal line's terminal record and round history."""
    report = AuditReport(target=folder_id, kind="goal_line")
    line_root = run_root / folder_id
    terminal_path = line_root / "terminal.json"
    read_cmd = f"cat {terminal_path}"

    if not terminal_path.is_file():
        report.record(
            Assertion(
                name="terminal_present",
                ok=False,
                command=read_cmd,
                exit_code=1,
                detail="terminal.json 不存在——线还在跑或 run root 不对",
            )
        )
        return report
    try:
        terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        report.record(
            Assertion(
                name="terminal_present",
                ok=False,
                command=read_cmd,
                exit_code=1,
                detail=f"terminal.json 不是合法 JSON: {exc}",
            )
        )
        return report

    terminal_value = terminal.get("terminal")
    mechanical_ok = (
        terminal_value in TERMINAL_VOCABULARY
        and isinstance(terminal.get("pump_fault"), bool)
        and isinstance(terminal.get("rounds"), int)
    )
    report.record(
        Assertion(
            name="terminal_mechanical_fields",
            ok=mechanical_ok,
            command=read_cmd,
            exit_code=0,
            detail=(
                f"terminal={terminal_value!r} pump_fault={terminal.get('pump_fault')!r} "
                f"rounds={terminal.get('rounds')!r}"
            ),
        )
    )
    report.record(
        Assertion(
            name="folder_binding",
            ok=terminal.get("folder_id") == folder_id,
            command=read_cmd,
            exit_code=0,
            detail=(
                f"terminal.json 属于 folder_id={terminal.get('folder_id')!r}，"
                f"被审对象={folder_id!r}"
            ),
        )
    )

    waiting_on = terminal.get("waiting_on")
    report.record(
        Assertion(
            name="waiting_on",
            ok=terminal_value != "blocked" or waiting_on is not None,
            command=read_cmd,
            exit_code=0,
            detail=(
                f"waiting_on={waiting_on!r}"
                if waiting_on is not None
                else (
                    "blocked 却无 waiting_on 机器字段——无法机械判定阻塞类别（R0c 字段未落）"
                    if terminal_value == "blocked"
                    else "非 blocked 终局，waiting_on 不适用"
                )
            ),
        )
    )

    rounds_path = line_root / "rounds.jsonl"
    recorded: list[dict[str, Any]] = []
    if rounds_path.is_file():
        for raw in rounds_path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                try:
                    recorded.append(json.loads(raw))
                except ValueError:
                    recorded.append({"unparseable": raw[:120]})
    claimed_rounds = terminal.get("rounds")
    report.record(
        Assertion(
            name="rounds_consistency",
            ok=isinstance(claimed_rounds, int) and len(recorded) >= claimed_rounds,
            command=f"wc -l {rounds_path}",
            exit_code=0 if rounds_path.is_file() else 1,
            detail=(
                f"terminal 自述 {claimed_rounds!r} 轮，rounds.jsonl 实录 {len(recorded)} 条；"
                f"最近 rounds: {json.dumps(recorded[-3:], ensure_ascii=False)[:400]}"
            ),
        )
    )
    return report


# --- board evidence note --------------------------------------------------


def render_note(report: AuditReport) -> str:
    lines = [
        f"supervise audit {report.target} ({report.kind}): "
        f"{'全绿' if report.ok else '有红'}——机械审计，人仍拍板。",
    ]
    for assertion in report.assertions:
        mark = "PASS" if assertion.ok else "FAIL"
        lines.append(
            f"[{mark}] {assertion.name}: {assertion.detail[:160]} "
            f"({assertion.command[:120]} -> {assertion.exit_code})"
        )
    for result in report.acceptance_results:
        lines.append(f"  rerun: {' '.join(result['command'])[:120]} -> {result['exit_code']}")
    for gap in report.gaps:
        lines.append(f"[GAP] {gap}")
    return "\n".join(lines)[:3800]


def publish_report(
    client: BusClient,
    report: AuditReport,
    *,
    card_entity_id: str,
    question_note_id: str = "",
) -> PublishResult:
    """One evidence note, idempotent on (target, report content).

    Published through the raw client rather than `Board.note` because the note
    must also reference the question under audit; the payload shape is
    byte-for-byte the board's `work.note.v1` evidence contract.
    """
    refs = [{"target_entity": card_entity_id}]
    if question_note_id:
        refs.append({"target_entity": question_note_id})
    return client.publish(
        WORK_NOTES,
        NOTE_KIND,
        {
            "card_entity_id": card_entity_id,
            "note": render_note(report),
            "note_type": "evidence",
        },
        f"supervise-audit:{report.target}:{report.fingerprint()}",
        refs=refs,
    )


__all__ = [
    "Assertion",
    "AuditError",
    "AuditReport",
    "EvidenceSource",
    "GraphEngineSource",
    "OldEngineClient",
    "audit_development",
    "audit_goal_line",
    "publish_report",
    "render_note",
]
