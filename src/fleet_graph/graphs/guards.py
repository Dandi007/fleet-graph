"""The counting guards that decide when a line stops.

Everything here is pure counting and text comparison. Nothing in this module
reads meaning out of a coordinator's answer, runs an acceptance check, or
touches a work folder -- that is INV-3, and it is what keeps the orchestrator
from quietly becoming a second, worse coordinator.

Three breakers, each catching what the previous one misses:

- **INV-8, bounds.** Round count and deadline. Pure arithmetic, no judgement.
- **INV-9, exact repeat.** The same prompt twice means the line is looping;
  refuse to inject it again.
- **INV-9b, near repeat.** This is the one that matters most and the one the
  plan's port list forgot. INV-9 hashes the prompt, so a coordinator that
  rewords the same instruction every round defeats it: the text differs, the
  sha differs, and a self-reported `no_progress` flag stays false. Meanwhile
  the line burns money forever and looks busy. Character-bigram Jaccard
  catches the reworded merry-go-round while staying insensitive to genuinely
  new facts -- a new ticket id, a new error, a new command output all move the
  score well below the threshold.

Transcribed from goal-agent's pump, including the constants: changing them
changes when real lines stop.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum

DEFAULT_MAX_ROUNDS = 10
DEFAULT_NOOP_LIMIT = 3
DEFAULT_TIMEOUT_LIMIT = 2
DEFAULT_REPEAT_SIMILARITY = 0.90


def bigram_set(text: str) -> set[str]:
    """Whitespace-stripped character bigrams. Degrades to a single char below length 2."""
    stripped = "".join(text.split())
    if len(stripped) < 2:
        return {stripped} if stripped else set()
    return {stripped[i : i + 2] for i in range(len(stripped) - 1)}


def prompt_similarity(a: str, b: str) -> float:
    """Character-bigram Jaccard similarity, 0..1. Pure function."""
    set_a, set_b = bigram_set(a), bigram_set(b)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class PromptVerdict(StrEnum):
    FRESH = "fresh"
    DUPLICATE = "duplicate"  # INV-9
    NO_PROGRESS = "no_progress"  # INV-9b


@dataclass(frozen=True)
class PromptCheck:
    verdict: PromptVerdict
    sha256: str
    similarity: float = 0.0
    first_seen_round: int | None = None

    @property
    def injectable(self) -> bool:
        return self.verdict is PromptVerdict.FRESH


@dataclass(frozen=True)
class LineBounds:
    """Stopping criteria. Pure counts and a wall-clock deadline -- INV-8."""

    max_rounds: int = DEFAULT_MAX_ROUNDS
    deadline_at: float | None = None
    noop_limit: int = DEFAULT_NOOP_LIMIT
    timeout_limit: int = DEFAULT_TIMEOUT_LIMIT
    repeat_similarity: float = DEFAULT_REPEAT_SIMILARITY


@dataclass
class LineGuards:
    """Mutable counting state for one line. Safe to rebuild from rounds.jsonl."""

    bounds: LineBounds = field(default_factory=LineBounds)
    prompts_seen: dict[str, int] = field(default_factory=dict)
    prev_prompt: str | None = None
    noop_streak: int = 0
    timeout_streak: int = 0

    # --- INV-8 -----------------------------------------------------------

    def bounds_exceeded(self, round_no: int, now: float | None = None) -> str | None:
        """Return a terminal reason, or None to keep going. Counting only."""
        if round_no > self.bounds.max_rounds:
            return f"round {round_no} exceeds max_rounds {self.bounds.max_rounds}"
        if (
            self.bounds.deadline_at is not None
            and now is not None
            and now > self.bounds.deadline_at
        ):
            return "deadline exceeded"
        return None

    # --- INV-9 / INV-9b --------------------------------------------------

    def check_prompt(self, prompt: str, round_no: int) -> PromptCheck:
        """Decide whether this prompt may be injected.

        Order matters: exact repeat is cheaper and more certain than the
        similarity test, so it is checked first and reported as its own reason.
        """
        digest = prompt_sha256(prompt)
        if digest in self.prompts_seen:
            return PromptCheck(
                PromptVerdict.DUPLICATE,
                digest,
                similarity=1.0,
                first_seen_round=self.prompts_seen[digest],
            )

        if self.prev_prompt is not None:
            score = prompt_similarity(self.prev_prompt, prompt)
            if score >= self.bounds.repeat_similarity:
                return PromptCheck(PromptVerdict.NO_PROGRESS, digest, similarity=score)
            return PromptCheck(PromptVerdict.FRESH, digest, similarity=score)

        return PromptCheck(PromptVerdict.FRESH, digest)

    def accept_prompt(self, check: PromptCheck, prompt: str, round_no: int) -> None:
        """Record a prompt that is actually being injected."""
        if not check.injectable:
            raise ValueError(f"refusing to record a {check.verdict.value} prompt")
        self.prompts_seen[check.sha256] = round_no
        self.prev_prompt = prompt

    # --- streaks ---------------------------------------------------------

    def record_progress(self) -> None:
        self.noop_streak = 0

    def record_noop(self) -> int:
        self.noop_streak += 1
        return self.noop_streak

    def record_turn_ok(self) -> None:
        self.timeout_streak = 0

    def record_timeout(self) -> int:
        self.timeout_streak += 1
        return self.timeout_streak

    def streak_exceeded(self) -> str | None:
        if self.noop_streak >= self.bounds.noop_limit:
            return f"{self.noop_streak} consecutive rounds without progress"
        if self.timeout_streak >= self.bounds.timeout_limit:
            return f"{self.timeout_streak} consecutive worker turn timeouts"
        return None


__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_NOOP_LIMIT",
    "DEFAULT_REPEAT_SIMILARITY",
    "DEFAULT_TIMEOUT_LIMIT",
    "LineBounds",
    "LineGuards",
    "PromptCheck",
    "PromptVerdict",
    "bigram_set",
    "prompt_sha256",
    "prompt_similarity",
]
