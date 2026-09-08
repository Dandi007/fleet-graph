# DD01 SPEC: Goal request kernel replacement

Status: prepared for coordinator decision, NOT dispatched, NOT implemented or reviewed. Exactly one product repository, one immutable SPEC, one DD and one PR. This is the first bounded development slice, not the entire frozen-design delivery.

## Subject and frozen inputs

repo_path: /data/code/fleet-comparison/self/wf-2bf703-fleet-graph-dd01
remote: origin (https://github.com/Dandi007/fleet-graph.git)
target_base: ff8a15c1117bfa506c84031814a07d2b26c096f8
target_ref: refs/heads/release/fleet-compare-self
Existing preparation branch: dd/fleet-compare-self/wf-2bf703-goal-kernel. Preserve it; the current harness owns its own generated DD audit branch under dd/fleet-compare-self/.

Inputs in WF wf-2bf703: goal.md (current experiment constraints), inputs/design.md v2.1 (sha256:1f1cdc570a878441cf8c42db3330235168571aa32d94aa22fbddd397cb49ce81), inputs/protocol.md (sha256:9aa4da67047e44dac04f4635d8bc44c40ec25a71691cf3a65d7a4caf2209e33c), inputs/review-notes.md (sha256:cdb75bd4ac8da01a0f3b4e2e7ea05cfe59f15828060fda74a65b38880fb391ce), inputs/decision-history.md GO-40 through GO-65. WF is accessed only through MCP; no physical data-root assumptions. Protocol field names are proposals, not claims about current harness capabilities.

## Objective and scope

Replace the product's coordinator/worker round-prompt progression with a durable, serial request-to-Goal-call kernel at the existing composition seam. One accepted request produces one ReAct call and one Stop action List. Multiple DDs remain independently progressing; a DD result/review event does not wait for other DDs. Dependency decisions remain with Goal Agent, not a new dependency scheduler. DD internals remain linear. Use the existing LangGraph dependency and existing injected service boundaries; do not build a parallel toy engine whose old path still carries this scope.

Code inventory: src/fleet_graph/graphs/runner.py build_line; graphs/goal_line.py build_goal_line_graph; graphs/stop_response.py; graphs/dd_subgraph.py DevelopmentGateway, ControlPlaneGateway, DdSubgraph; graphs/adapters.py AgentRunCoordinator/AgentSessionWorker; executors/agent_run.py and executors/agent_session.py; goal/service.py; state/work_folder.py. Inventory is a starting map, not a mandate to preserve obsolete APIs. DD01 must wire the new request kernel into the product composition path and remove/replace the superseded round/prompt path for the same responsibility. The fixed driver outside this repository must remain untouched.

## Required behavior

1. Durable per-goal requests include stable request identity, caller, request kind, current input, goal version and reply association. Enroll, message, steer and DD result/review inputs enter this single queue. Persist acceptance before acknowledging. Stable ordered requests are not combined into a multi-request user prompt. Historical material is addressable via pointers, not automatically appended to current user input.
2. At most one Goal call per goal may be in flight. Requests arriving during a call remain queued. Receive A review, then B review and external message while A is running: after A, independently deliver B then message. DD advancement is not globally blocked while Goal processes A. Retain separate per-DD identities and current-state projections.
3. Validate Stop List structurally before executing effects. Provide typed dispatch, approve/reject, add_repo, reply and waiting/blocked/done intent routing with injectable effect ports. Business guards include one repo per dispatch and version-bound review; unsupported downstream capabilities return explicit not-ready/failure receipts, never simulated success. Reply is an actual requested side effect with a separate delivery result associated with the caller/request, not merely printed text.
4. Persist action intent and independent result linked to request/run/list index. Replay may re-observe but must not duplicate an already confirmed dispatch/reply. A failed item does not erase earlier successful results. Resume checks the existing external-effect port for uncertain outcomes before retrying; unknown observations remain recoverable and require Goal judgment. Do not claim exactly-once external delivery where the service cannot supply identity or reconciliation.
5. waiting must not suppress already queued requests. blocked can be woken by a new request. Explicit stopped persists message/steer but starts no new call/effect until resume. Graceful stop drains an in-flight result before stopping; immediate-stop requests are routed to the runtime control port and recorded without pretending that absent cancellation support terminated a process. Goal version changes are explicit immutable events; editing WF alone does not change the active version.
6. Emit queryable raw request/call/action/control events with goal, DD where applicable, run/session, ordering and evidence references. Expose pagination that can traverse all recorded payloads; do not retain only summaries or tails. This is design L0 (raw engine events + Runtime Session), not the unrelated L0/L1/L2 infrastructure terminology in old docs. A missing full-session Runtime capability remains explicit, not reconstructed or invented by Fleet.
7. Preserve recoverable in-flight DD and requests on interruption. Do not reset existing release branches or discard worktrees. Keep existing single-repo DD service behind its interface for this slice; full DD pipeline unification, merge implementation and final multi-repo completion are subsequent slices, not passed by this DD.

## P1-P8 working decisions

These are implementation choices for this slice, not new user rulings. Impl records any revised choice and rationale in the same DD evidence.

- P1: ordered per-item action receipts; stable request/run/index identity; at most one terminal intent; reject incompatible terminal intent without discarding completed effects. Same-list add_repo must succeed before its dependent dispatch; long DD completion is not an action-list ordering barrier.
- P2: retain the merge port boundary; actual target-branch serialization and expected-tip merge guard belong to the follow-up lifecycle slice. DD01 cannot approve or report a merge solely from an agent string.
- P3: needs_goal/unknown side effects preserve work and carry explicit evidence; input changes have versions; cleanup only on explicit terminal lifecycle decisions.
- P4: done is an intent, never a terminal shortcut. Until all required delivery receipts exist it remains pending; actual per-repo finalization is follow-up work.
- P5: duplicate enroll/request identity reuses persisted state; verify existing release ownership/base via ports; never reset a branch on replay.
- P6: use one durable ordered journal and one goal ownership fence, with intent/result recovery and explicit incomplete-tail handling. LangGraph cache is reconstructable, not the sole truth. Test crash boundaries with fakes, not real killed engines.
- P7: runtime owns models, harness, schema conformance and session lifecycle. Fleet passes configured contracts through supported adapters and fails explicitly on unsupported capability; do not reimplement runtime in Python.
- P8: raw event query belongs here; full Runtime Session query and asynchronous read-only L1 clerk integration follow after dependency capability confirmation. No clerk failure may enter the DD failure path.

## Dependency boundary

Runtime product baseline bb941d0b44a32849907e11e22c5872c401a1612a supplies one-shot agent-run, roles and structured result envelopes, and agent-session start/send/compact/status/stop. The session API rejects concurrent sends rather than queuing them. One-shot status/detach exists, but no generic detached cancellation CLI was found; generic caller schema/live-session schema parity and complete Session query must not be assumed. The current validate command fails exit 90 (see recovery-054bbe16-evidence.md). Runtime corrections or capability additions require their own repo DD. DD01 may implement against injectable contracts and current known adapters without claiming live compatibility; it must report unsupported portions precisely.

Katana baseline 575d819da8ae94c296082eeb58be9ee63d7d47fc exposes opaque folder-ID fs read/stat/batch and lifecycle save/append_progress. Existing revision, CAS and idempotency parameters are usable; do not reimplement a WF Git store or put engine journal/session state in WF. No Katana change is established as necessary for DD01. If public API insufficiency is proven, hand a separate repo SPEC to coordinator, not a cross-repo patch.

DD01 does not depend on a successful live Runtime invocation for its isolated unit acceptance. Live binding and final development delivery do depend on Runtime baseline repair and capability confirmation. No dependency code/config or gateway/driver modification is allowed in this DD.

## Development acceptance

Impl must add a focused, offline test module tests/test_goal_request_kernel.py and retain/update affected existing tests with a requirement-to-test map. Required checks in the DD independent worktree:

- python3 -m compileall -q src
- python3 -m pytest -q tests/test_goal_request_kernel.py

The second command is a required future acceptance target, not run or claimed to exist in this preparation turn. Use the repository's declared Python 3.11/dependency environment. If dependencies/setup are missing, report an acceptance-environment failure and request the supported reconfiguration path; do not fabricate success or widen checks into live integrations.

Tests must exercise serial A/B/message ordering, DD independence, one-current-request prompt, action partial failure, same-request replay, reply reconciliation, crash before/after effect receipt, stopped versus waiting wakeups, explicit goal versions, stale-version approval refusal, unknown Runtime outcome, and lossless pagination. All runtime/git/PR/WF/network/process ports must be fake or isolated fixtures; no agents, model requests, daemons, systemd, shared services or new engine process. Test modules requiring real services must not be run in this phase. Record exact argv/cwd/time/exit/stdout/stderr and inspected code SHA. CR and FR are performed only by DD; FR verifies these development-stage requirements and lists pending real runtime scenarios rather than demanding forbidden E2E or declaring production acceptance.

Delivery of this DD: one PR targeting release/fleet-compare-self, commits and actual checks tied to SHA, new/retained/replaced test rationale, scope/dependency mapping, and follow-up boundaries. Do not alter acceptance to hide a failing existing test. Any code update invalidates prior version-bound acceptance/review and returns through DD. No merges into main, deployment, shared current changes or new engine execution.

## Deferred verification and later slices

Not satisfied by DD01: full MCP enroll/repo lifecycle, complete linear Impl->acceptance->CR->FR->Goal->merge workflow replacement, conflict rework in original DD/SPEC/PR, real runtime recovery and cancellation, per-repo serialized merges, all-repo finalization, full Session pagination, and asynchronous read-only L1 clerk. Coordinator must dispatch subsequent single-repo work after inspecting dependencies.

Joint phase, only after both groups finish and authorization opens it: real multi-DD concurrency/serial Goal calls; actual reply delivery; runtime crash/adoption and full Session queries; stop/resume against real processes; merge-after-crash reconciliation; multi-repo partial finalization; clerk outage isolation; installation/start/stop/ports/data-root validation. The experiment still forbids actual main merges. A first DD pass is not ready_for_joint_validation for the entire Goal, and compileall is not functional acceptance.