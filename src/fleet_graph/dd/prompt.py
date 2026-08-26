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
from dataclasses import dataclass, field
from typing import Any

PLACEHOLDER = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)(\?)?\}\}")

# Where the bundle keeps each stage's prompt parts, keyed as
# `load_implement_stage_resources` returns them.
IMPLEMENT_PERSONA = "implement/personas/implementer.md"
IMPLEMENT_TEMPLATE = "implement/templates/implement.md"

# How to hand the result back. The bundle's persona is written for
# loop-engine's harness, which collects the result over its own dependency
# channel, so it says nothing about envelopes. fleet-graph dispatches through
# `agent-run`, and how the answer travels is therefore fleet-graph's to state.
# Leaving it unsaid is what an earlier run did, and the agent did the work and
# then returned nothing a machine could read.
RESULT_TRANSPORT = """\
## Returning your result (fleet-graph dispatch)

Put one JSON object in `Envelope.result` -- not in prose, not in a fenced block
in your commentary. It must carry exactly:

- `actor_job_id`: the `actor_job_id` given above, echoed back verbatim
- `input_commit`: the `input_commit` given above, echoed back verbatim
- `outcome`: `APPLIED`, `DISPUTED` or `BLOCKED`
- `work_head_commit`: the full 40-hex commit you finished on (APPLIED only)
- `verification_record`: `{"verification_commands": [{"argv": [...], "exit_code": N}]}`
  for every acceptance command you ran (APPLIED only)
- `rebuttal` (DISPUTED) or `blocker` (BLOCKED) instead of the two above

Doing the work and returning nothing readable is the same as not doing it: the
deterministic seal has no other way to learn what you produced.\
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
    values["acceptance_commands"] = acceptance_commands or []
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

    def for_stage(
        self, stage_id: str, dispatch: dict[str, Any], *, run_id: str, actor_job_id: str
    ) -> str | None:
        if stage_id != "implement":
            return None
        stage_dispatch = self.builder.build(
            dispatch, parent_receipt=dispatch.get("parent_receipt") or None
        )
        return render_stage_prompt(
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


__all__ = [
    "IMPLEMENT_PERSONA",
    "IMPLEMENT_TEMPLATE",
    "RESULT_TRANSPORT",
    "PluginPromptSource",
    "PromptError",
    "as_value",
    "bundle_resources",
    "render_stage_prompt",
    "render_template",
    "stage_values",
]
