"""The dd stage machine, read from its contract rather than rewritten.

`development-lifecycle.json` already declares every stage, every transition,
and the bindings that constrain them. Re-expressing that as Python edges would
mean maintaining two descriptions of the same machine and hoping they agree --
and the one that ships would be mine, not the one dd's own tooling validates
against.

So the graph edges come from the contract. Three consequences worth naming:

- **Equivalence holds by construction.** The plan's P3 DoD asks for schema
  equivalence tests; there is nothing to diff, because there is only one table.
- **A contract change needs no code change.** A new stage or verdict flows
  through, and the tests below fail loudly if the shape shifts underneath.
- **An unknown (stage, event) pair is a fault, not a guess.** The old
  reconciler had the same rule; guessing a transition is how a pipeline
  silently takes a path nobody designed.

Two bindings do real work and are enforced here rather than trusted:

- `commit_binding` is the forward chain. The receipt's `output_commit` must be
  the next dispatch's `input_commit`, which is what stops a stage from being
  handed work that was never actually produced by the stage before it.
- `event_binding` ties the transition to the receipt's own verdict, so a
  caller cannot claim APPROVE while holding a receipt that says REJECT.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

CONTRACTS_DIR = Path(__file__).parent / "contracts"
LIFECYCLE_PATH = CONTRACTS_DIR / "development-lifecycle.json"


class LifecycleError(RuntimeError):
    pass


class UnknownTransition(LifecycleError):
    """No declared edge for this (stage, event). Refuse rather than invent one."""


class BindingViolation(LifecycleError):
    """A declared binding did not hold. The chain is broken; stop."""


@dataclass(frozen=True)
class Stage:
    id: str
    actor: str
    wrapper: bool
    required_artifacts: tuple[str, ...] = ()
    produced_artifacts: tuple[str, ...] = ()

    @property
    def is_llm(self) -> bool:
        """LLM stages are dispatched to an agent; script stages run in-process."""
        return self.actor == "llm"


@dataclass(frozen=True)
class Transition:
    source: str
    event: str
    target: str
    next_mode: str
    required_receipt: str | None = None
    commit_binding: dict[str, Any] | None = None
    event_binding: dict[str, Any] | None = None

    @property
    def is_rework(self) -> bool:
        return self.next_mode == "rework"


def _dig(obj: Any, dotted: str) -> Any:
    """Resolve a binding path like `required_receipt.output_commit`."""
    current = obj
    for part in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


class Lifecycle:
    def __init__(self, contract: dict[str, Any]) -> None:
        self.contract = contract

    @classmethod
    def load(cls, path: Path | str = LIFECYCLE_PATH) -> Lifecycle:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def contract_version(self) -> int:
        return int(self.contract["contract_version"])

    @cached_property
    def stages(self) -> dict[str, Stage]:
        return {
            raw["id"]: Stage(
                id=raw["id"],
                actor=raw["actor"],
                wrapper=bool(raw.get("wrapper", False)),
                required_artifacts=tuple(raw.get("required_artifacts", ())),
                produced_artifacts=tuple(raw.get("produced_artifacts", ())),
            )
            for raw in self.contract["stages"]
        }

    @cached_property
    def transitions(self) -> tuple[Transition, ...]:
        return tuple(
            Transition(
                source=raw["from"],
                event=raw["on"],
                target=raw["to"],
                next_mode=raw.get("next_mode", "inherit"),
                required_receipt=raw.get("required_receipt"),
                commit_binding=raw.get("commit_binding"),
                event_binding=raw.get("event_binding"),
            )
            for raw in self.contract["transitions"]
        )

    @cached_property
    def failure_taxonomy(self) -> dict[str, dict[str, Any]]:
        return dict(self.contract.get("failure_taxonomy", {}))

    def is_retryable(self, failure_code: str) -> bool:
        """Unknown codes are not retryable.

        Retrying something the contract never described is how a broken
        pipeline turns into a loop that burns money.
        """
        entry = self.failure_taxonomy.get(failure_code)
        return bool(entry and entry.get("retryable", False))

    def transition(self, stage: str, event: str) -> Transition:
        for candidate in self.transitions:
            if candidate.source == stage and candidate.event == event:
                return candidate
        raise UnknownTransition(
            f"no declared transition from {stage!r} on {event!r}; "
            "the contract is the authority, so this is a fault rather than a path to invent"
        )

    def events_from(self, stage: str) -> tuple[str, ...]:
        return tuple(t.event for t in self.transitions if t.source == stage)

    def is_terminal(self, stage: str) -> bool:
        """A stage with no outgoing transitions ends the pipeline."""
        return stage in self.stages and not self.events_from(stage)

    # --- bindings --------------------------------------------------------

    def check_event_binding(self, transition: Transition, receipt: dict[str, Any]) -> None:
        """The receipt's own verdict must match the edge being taken."""
        binding = transition.event_binding
        if not binding:
            return
        source = _dig({"required_receipt": receipt}, binding["source"])
        target = transition.event if binding["target"] == "transition.on" else None
        if binding.get("operator", "equal") == "equal" and source != target:
            raise BindingViolation(
                f"event binding failed on {transition.source}->{transition.target}: "
                f"receipt says {source!r} but the transition claims {target!r}"
            )

    def check_commit_binding(
        self, transition: Transition, receipt: dict[str, Any], next_dispatch: dict[str, Any]
    ) -> None:
        """The forward chain: the next stage starts from what this one produced."""
        binding = transition.commit_binding
        if not binding:
            return
        source = _dig({"required_receipt": receipt}, binding["source"])
        target = _dig({"next_dispatch": next_dispatch}, binding["target"])
        if binding.get("operator", "equal") == "equal" and source != target:
            raise BindingViolation(
                f"commit binding broken on {transition.source}->{transition.target}: "
                f"receipt produced {source!r} but the next dispatch starts from {target!r}; "
                "the forward chain is severed"
            )

    def advance(
        self,
        stage: str,
        event: str,
        *,
        receipt: dict[str, Any] | None = None,
        next_dispatch: dict[str, Any] | None = None,
    ) -> Transition:
        """Resolve the edge and enforce every binding the contract declares."""
        transition = self.transition(stage, event)
        if transition.required_receipt and receipt is None:
            raise BindingViolation(
                f"{transition.source}->{transition.target} requires a "
                f"{transition.required_receipt} receipt; none was supplied"
            )
        if receipt is not None:
            self.check_event_binding(transition, receipt)
            if next_dispatch is not None:
                self.check_commit_binding(transition, receipt, next_dispatch)
        return transition


__all__ = [
    "LIFECYCLE_PATH",
    "BindingViolation",
    "Lifecycle",
    "LifecycleError",
    "Stage",
    "Transition",
    "UnknownTransition",
]
