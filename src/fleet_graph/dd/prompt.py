"""Rendering the prompt the plugin bundle ships for a stage.

The persona a stage runs under belongs with the contract it serves, and the
plugin bundle is where dd keeps it -- pinned by digest in the capability
manifest, alongside the schema the result is validated against. Dispatching
agent-runtime's role and letting its own persona stand instead is how the two
drift, and they have: the role persona asks for three fields, the bundle's asks
for the outcome, the acceptance commands with their real exit codes, and the
rule that reporting a command you did not run is a contract violation.

So the prompt is read out of the bundle, at the commit the capability check
admitted, and rendered with the dispatch that is already built for the sealer.
The template's placeholders turned out to be exactly that dispatch plus a few
runtime values, which is not a coincidence: both are the same contract.

**An unresolved required placeholder is a fault.** A prompt that still says
`{{input_commit}}` is telling the agent nothing while looking like it told it
something, and the agent's answer would be shaped by a field it never got.
Optional placeholders -- the ones written `{{name?}}` -- resolve to empty.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from fleet_graph.dd.bootstrap import HISTORY_PATH
from fleet_graph.dd.upstream_constants import ReviewPhase

# The contract's own review_phase vocabulary, keyed by dd stage id.
REVIEW_PHASE = {
    "continuous_review": str(ReviewPhase.CONTINUOUS),
    "final_review": str(ReviewPhase.FINAL),
}
# The artifact the implement stage produces; its sealed commit is the one a
# review names as its subject, which stops being the review's own input commit
# as soon as a second review runs after the first.
IMPLEMENT_EVIDENCE = "implementation_evidence"

REVIEW_ID_PREFIX = {
    str(ReviewPhase.CONTINUOUS): "rc-",
    str(ReviewPhase.FINAL): "rf-",
}

#: The greppable anchor every gate-rework implement prompt carries (wf-8d9737
#: rework contract A). The section header is followed by the rejecting
#: verdict's message id, so an acceptance check can mechanically assert both:
#: `grep gate-reject-rationale:` and `grep <decision_message_id>`.
GATE_REJECT_ANCHOR = "gate-reject-rationale:"

PLACEHOLDER = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)(\?)?\}\}")

# Where the bundle keeps each stage's prompt parts, keyed as
# `load_implement_stage_resources` returns them.
IMPLEMENT_PERSONA = "implement/personas/implementer.md"
IMPLEMENT_TEMPLATE = "implement/templates/implement.md"

# **The one thing this has to override.** The bundle's template shows its
# APPLIED example as `{"result": {...}, "effects": []}` -- loop-engine's
# envelope, where the payload is nested under `result`. agent-run validates
# `Envelope.result` itself against `implement-result.v1`, so the fields belong
# at the *top* level. An agent following the bundle's example returns the
# nested shape and is rejected with "missing required field: actor_job_id;
# input_commit; work_head_commit" -- exactly the three top-level fields, having
# seen an object with only `result` and `effects`.
#
# That cost four real runs to find, and it is not the agent's fault: it did the
# work correctly every time and copied the example it was shown. Two harnesses,
# two envelopes; the one that ships the template is not the one dispatching.
RESULT_TRANSPORT = """\
## Result envelope (fleet-graph dispatch -- this overrides the example above)

The APPLIED/DISPUTED/BLOCKED examples above are written for loop-engine's
harness, which nests the payload under a `result` key. **This dispatch does
not.** Put the fields at the top level of `Envelope.result`:

```json
{
  "actor_job_id": "<echoed back verbatim>",
  "input_commit": "<echoed back verbatim>",
  "outcome": "APPLIED",
  "work_head_commit": "<full 40-hex commit you finished on>",
  "verification_record": {
    "verification_commands": [{"argv": ["..."], "exit_code": 0}]
  }
}
```

No outer `result` key, no outer `effects` key -- those belong to the other
harness. For DISPUTED or BLOCKED, replace `work_head_commit` and
`verification_record` with `rebuttal` or `blocker` respectively.

Doing the work and returning a shape the seal cannot read is the same as not
doing it: there is no other way for it to learn what you produced.\
"""


class PromptError(RuntimeError):
    """The prompt cannot be rendered as written. Do not dispatch a half-filled one."""


def as_value(value: Any) -> str:
    """Scalars as themselves, structures as compact JSON.

    The template drops `{{spec_ref}}` and `{{dispatch}}` into prose, so a dict
    has to arrive as something an agent can read back.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return json.dumps(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_template(text: str, values: dict[str, Any]) -> str:
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        name, optional = match.group(1), match.group(2)
        if name in values and values[name] is not None:
            return as_value(values[name])
        if optional:
            return ""
        missing.append(name)
        return match.group(0)

    rendered = PLACEHOLDER.sub(substitute, text)
    if missing:
        raise PromptError(
            f"template has unresolved required placeholders {sorted(set(missing))}; "
            "a prompt that still names a field it does not carry is worse than a short one"
        )
    return rendered


def render_commands(commands: list[list[str]]) -> str:
    """argv lists as something an agent can actually run.

    The template drops this into "Run {{acceptance_commands}} and fix all
    failures". Rendering the raw list left that sentence saying
    `[["python3","-m","pytest"]]`, which is not an instruction anyone can
    follow -- and the persona is explicit that these are argv, never a shell
    string, so the quoting has to survive.
    """
    return ", ".join(shlex.join(command) for command in commands if command) or "(none declared)"


def stage_values(
    stage_dispatch: dict[str, Any],
    *,
    worktree_path: str,
    run_id: str,
    actor_job_id: str,
    acceptance_commands: list[list[str]] | None = None,
    trigger_id: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The dispatch, plus the handful of runtime values the template also names."""
    values: dict[str, Any] = dict(stage_dispatch)
    values["dispatch"] = stage_dispatch
    values["worktree_path"] = worktree_path
    values["run_id"] = run_id
    values["actor_job_id"] = actor_job_id
    values["acceptance_commands"] = render_commands(acceptance_commands or [])
    # loop-engine's own correlation id. fleet-graph has no trigger store, so
    # the run id stands in rather than a value invented to look like one.
    values["trigger_id"] = trigger_id or run_id
    values.update(extra or {})
    return values


def render_stage_prompt(
    resources: dict[str, str],
    persona_key: str,
    template_key: str,
    values: dict[str, Any],
    transport: str = RESULT_TRANSPORT,
) -> str:
    """Persona first, then the rendered template -- the order the workflow uses."""
    for key in (persona_key, template_key):
        if key not in resources:
            raise PromptError(f"the bundle carries no {key!r}")
    persona = render_template(resources[persona_key], values)
    body = render_template(resources[template_key], values)
    parts = [persona.rstrip(), body.strip()]
    if transport:
        parts.append(transport.strip())
    return "\n\n---\n\n".join(parts)


def bundle_resources(loaded: Any) -> dict[str, str]:
    """Decode what `load_*_stage_resources` returned into text by relative path."""
    return {resource.relative_path: resource.content.decode("utf-8") for resource in loaded}


REVIEW_PROMPT = """\
You are the {phase} Reviewer for one `dev-dispatch.attempt-context/v1` attempt.

## The commits under review

- `input_commit`: `{subject_commit}` (this review starts from the previous
  stage's sealed output)
- `subject_commit`: `{subject_commit}` (equal to `input_commit`, by contract)
- `implementation_subject_commit`: `{implementation_subject_commit}` (the
  implement stage's sealed output)
- product commit (`work_head_commit`): `{product_commit}`

`subject_commit` and `implementation_subject_commit` are commits written by the
dev-dispatch materializer. The implement materializer seals its handoff by
stacking a metadata-only commit -- one that changes only the reserved
`.dev-dispatch/` namespace -- on top of the implement actor's product commit
(`work_head_commit`), and the review materializer does the same for the review
artifacts. Two of these SHAs may therefore differ while their product trees are
byte-identical. Verify product consistency mechanically before reviewing, not
by SHA equality:

    git diff --exit-code {product_commit} {subject_commit} -- . ':(exclude).dev-dispatch'

must exit 0. If that diff is non-empty -- the materialization commit actually
changed a product file -- report a `blocker` finding with `REJECT` rather than stopping
silently; a review that stops without a verdict is indistinguishable
from one that never ran. Do not reject merely because two SHAs differ: a
metadata-only materialization is compliant, not a finding.

Review the product changes at `{product_commit}` (carried unchanged into
`{subject_commit}`) against the approved spec at `{spec_path}`, in the worktree
`{worktree_path}`. Read the committed feedback index at `{index_path}` and
every review artifact it references; together with the immutable cross-generation
feedback archive at `{history_path}` (older generations' entries, never erased)
they are the complete feedback history. The live index is scoped to the current
generation's attempt chain, so earlier-generation records appear in the archive,
not in the index. Do not modify anything: this is a read-only review, and a
reviewer that writes to the subject workspace has its verdict discarded.

## Your verdict

`APPROVE` only if the change satisfies the spec. `REJECT` otherwise, with at
least one finding saying why. Reporting a spec as satisfied when it is not is
a contract violation, and so is inventing a finding to look thorough.

## Result envelope (fleet-graph dispatch)

Put one JSON object at the top level of `Envelope.result` -- no outer `result`
key, no outer `effects` key. Echo these back verbatim:

```json
{{
  "contract_version": "{contract_version}",
  "review_id": "{review_id}",
  "attempt_id": "{attempt_id}",
  "review_phase": "{phase}",
  "subject_commit": "{subject_commit}",
  "implementation_subject_commit": "{implementation_subject_commit}",
  "spec_digest": "{spec_digest}",
  "reviewer_job_id": "{reviewer_job_id}"
}}
```

and add these three, which are yours to determine:

- `verdict`: `APPROVE` or `REJECT`
- `findings`: an array, possibly empty, of
  `{{"severity": "blocker"|"major"|"minor"|"note", "summary": "...", "location": "..."}}`
  -- `location` is optional; `severity` and `summary` are not
- `reviewer_model`: the model you are running as

All eleven keys must be present, and nothing else.\
"""


def derive_review_id(attempt_id: str, phase: str) -> str:
    """The canonical review id, exactly as the plugin computes it.

    Not a free value and not something to invent: the sealer recomputes it and
    refuses a receipt whose `review_id` differs
    (`BINDING_MISMATCH: review_id must equal canonical rc-...`). It is a prefix
    on the attempt id -- `rc-` continuous, `rf-` final -- which is why a
    uuid5 of my own devising was wrong however consistently it was derived.
    """
    return f"{REVIEW_ID_PREFIX[phase]}{attempt_id}"


def implement_product_commit(
    dispatch: dict[str, Any], *, implementation_subject_commit: str
) -> str:
    """The commit whose product tree a review is bound to review.

    The implement materializer seals the handoff with a metadata-only commit
    (its `output_commit`) stacked on the actor's product commit (its
    `work_head_commit`). The product commit is what the review subject must be
    product-path-equal to. It travels on the implement receipt, which for a
    continuous review is the parent receipt carried on the dispatch. For a
    final review the parent is the continuous review receipt, which carries no
    `work_head_commit`, so the tree-equal anchor is `implementation_subject_commit`
    -- the implement's sealed output, itself metadata-only with respect to the
    same product tree.
    """
    parent = dispatch.get("parent_receipt") or {}
    work_head = parent.get("work_head_commit")
    return str(work_head) if work_head else implementation_subject_commit


def render_review_prompt(
    stage_dispatch: dict[str, Any],
    *,
    phase: str,
    worktree_path: str,
    reviewer_job_id: str,
    implementation_subject_commit: str,
    spec_path: str,
    index_path: str,
    product_commit: str = "",
    history_path: str = HISTORY_PATH,
) -> str:
    """Our own review prompt, not the bundle's workflow.

    The bundle runs review as several nodes with their own templates, and
    rebuilding that here would be rebuilding its workflow engine. What this
    does is narrower and sufficient: state the task, and state the result
    contract -- because seven of `review.result.v2`'s eleven fields are values
    we already hold and the reviewer only has to echo. Four are its own:
    verdict, findings, reviewer_job_id, reviewer_model.
    """
    return REVIEW_PROMPT.format(
        phase=phase,
        subject_commit=stage_dispatch["input_commit"],
        spec_path=spec_path,
        index_path=index_path,
        history_path=history_path,
        worktree_path=worktree_path,
        contract_version=stage_dispatch["contract_version"],
        review_id=derive_review_id(stage_dispatch["attempt_id"], phase),
        attempt_id=stage_dispatch["attempt_id"],
        implementation_subject_commit=implementation_subject_commit,
        product_commit=product_commit or implementation_subject_commit,
        spec_digest=stage_dispatch["spec_ref"]["digest"],
        reviewer_job_id=reviewer_job_id,
    )


def render_gate_reject_section(payload: dict[str, Any]) -> str:
    """The gate REJECT rationale, as the rework generation's mandated input.

    This is the engine-side injection point of rework contract A (wf-8d9737):
    a generation started after a human_gate REJECT must dispatch its
    implementer with the rejecting verdict mechanically attached -- the
    decision message id on the anchor line and again as a labeled field, the
    verdict face, and the rationale as a verbatim block. Spec ⑮-b: the
    rationale travels *in full*, exactly as the board ``work.decision.v1``
    sealed it (never a summary, never the terminal's one-line face), so every
    rework keyword the board wrote is greppable under the anchor.

    An unbound verdict -- empty ``decision_message_id`` or empty ``rationale``
    -- is a refusal here too: the control plane refuses such launches
    (``REWORK_DECISION_UNBOUND``), and a payload that slips past it must fail
    loudly at the prompt layer rather than render a task book with an empty
    binding.
    """
    message_id = str(payload.get("decision_message_id") or "").strip()
    rationale = str(payload.get("rationale") or "").strip()
    unbound = [
        name
        for name, value in (("decision_message_id", message_id), ("rationale", rationale))
        if not value
    ]
    if unbound:
        raise PromptError(
            f"gate-reject verdict is not bound to its board work.decision.v1 "
            f"(empty: {', '.join(unbound)}); a rework prompt is never assembled "
            "with an empty binding"
        )
    decided_by = str(payload.get("decided_by") or "").strip()
    lines = [
        f"## {GATE_REJECT_ANCHOR} {message_id}",
        "",
        "The human gate REJECTED the previous generation of this development. "
        "This verdict is the authoritative input for this rework generation:",
        "",
        f"- decision: {payload.get('decision') or 'REJECT'!s}",
        f"- decided_by: {decided_by}",
        f"- decision_message_id: {message_id}",
        f"- rejected_generation: {payload.get('rejected_generation', '')}",
        "",
        "rationale (verbatim, full text as the board work.decision.v1 sealed it):",
        "",
        rationale,
        "",
        "Address this rationale in the rework before re-presenting the work.",
    ]
    return "\n".join(lines)


@dataclass
class PluginPromptSource:
    """The implement prompt, read from the bundle the capability check admitted.

    Only implement. The review stages are a *workflow* in the bundle -- three
    templates across several nodes -- and reproducing that here would mean
    rebuilding the plugin's workflow engine inside the orchestration shell,
    which is the opposite of what this refactor is for. Reviews keep the role's
    own persona until there is a reason to do otherwise, and this returns None
    for them so the caller falls back rather than guessing.
    """

    binding: Any
    builder: Any
    worktree_path: str
    acceptance_commands: list[list[str]] = field(default_factory=list)
    verify_worktree_head: bool = True
    #: The gate REJECT verdict this generation must rework from (wf-8d9737
    #: rework contract A), read by the control plane at generation start and
    #: forwarded here. None/empty means the generation is not a gate rework
    #: and nothing is injected -- non-REJECT exits must never see the anchor.
    gate_reject: dict[str, Any] | None = None
    _cache: dict[str, str] | None = None

    def resources(self) -> dict[str, str]:
        if self._cache is None:
            from fleet_graph.dd.vendor.plugin_adapter import load_implement_stage_resources

            self._cache = bundle_resources(
                load_implement_stage_resources(
                    self.binding, verify_worktree_head=self.verify_worktree_head
                )
            )
        return self._cache

    def review_phase_of(self, stage_id: str) -> str | None:
        """`continuous` / `final` -- a third vocabulary, from the contract's own
        `review_phase` enum. dd's stage ids and the role input's stage values
        are the other two."""
        return REVIEW_PHASE.get(stage_id)

    def for_stage(
        self, stage_id: str, dispatch: dict[str, Any], *, run_id: str, actor_job_id: str
    ) -> str | None:
        phase = self.review_phase_of(stage_id)
        if phase is None and stage_id != "implement":
            return None
        stage_dispatch = self.builder.build(
            dispatch, parent_receipt=dispatch.get("parent_receipt") or None
        )
        if phase is not None:
            implementation_subject_commit = (dispatch.get("artifact_commits") or {}).get(
                IMPLEMENT_EVIDENCE
            ) or stage_dispatch["input_commit"]
            return render_review_prompt(
                stage_dispatch,
                phase=phase,
                worktree_path=self.worktree_path,
                reviewer_job_id=actor_job_id,
                implementation_subject_commit=implementation_subject_commit,
                product_commit=implement_product_commit(
                    dispatch, implementation_subject_commit=implementation_subject_commit
                ),
                spec_path=self.builder.spec_path,
                index_path=self.builder.index_path,
            )
        rendered = render_stage_prompt(
            self.resources(),
            IMPLEMENT_PERSONA,
            IMPLEMENT_TEMPLATE,
            stage_values(
                stage_dispatch,
                worktree_path=self.worktree_path,
                run_id=run_id,
                actor_job_id=actor_job_id,
                acceptance_commands=self.acceptance_commands,
            ),
        )
        if self.gate_reject:
            rendered = rendered + "\n\n---\n\n" + render_gate_reject_section(self.gate_reject)
        return rendered


__all__ = [
    "GATE_REJECT_ANCHOR",
    "IMPLEMENT_EVIDENCE",
    "IMPLEMENT_PERSONA",
    "IMPLEMENT_TEMPLATE",
    "RESULT_TRANSPORT",
    "REVIEW_ID_PREFIX",
    "REVIEW_PHASE",
    "REVIEW_PROMPT",
    "PluginPromptSource",
    "PromptError",
    "as_value",
    "bundle_resources",
    "derive_review_id",
    "implement_product_commit",
    "render_commands",
    "render_gate_reject_section",
    "render_review_prompt",
    "render_stage_prompt",
    "render_template",
    "stage_values",
]
