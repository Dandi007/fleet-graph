"""The counting guards: bounds, exact repeat, and the near-repeat breaker."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

import pytest

from fleet_graph.graphs.guards import (
    DEFAULT_REPEAT_SIMILARITY,
    LineBounds,
    LineGuards,
    PromptVerdict,
    bigram_set,
    prompt_sha256,
    prompt_similarity,
)

PUMP_SRC = Path("/data/code/self/goal-agent/src")


@pytest.fixture
def guards() -> LineGuards:
    return LineGuards()


class TestSimilarityFunction:
    def test_identical_text_is_one(self) -> None:
        assert prompt_similarity("do the thing", "do the thing") == 1.0

    def test_whitespace_is_ignored(self) -> None:
        assert prompt_similarity("do the thing", "do  the\nthing") == 1.0

    def test_disjoint_text_is_low(self) -> None:
        assert prompt_similarity("alpha beta", "zulu yankee") < 0.1

    def test_empty_versus_empty_is_one(self) -> None:
        assert prompt_similarity("", "") == 1.0

    def test_empty_versus_text_is_zero(self) -> None:
        assert prompt_similarity("", "something") == 0.0

    def test_single_character_degrades_gracefully(self) -> None:
        assert bigram_set("a") == {"a"}
        assert bigram_set("") == set()

    def test_is_symmetric(self) -> None:
        a, b = "check the quota alert", "check quota alerting"
        assert prompt_similarity(a, b) == prompt_similarity(b, a)


class TestEquivalenceWithThePump:
    """Compare against goal-agent's own implementation where it is present.

    The threshold is calibrated against this exact function; a subtly
    different one would move where real lines stop.
    """

    CASES: ClassVar[list[tuple[str, str]]] = [
        ("do the thing", "do the thing"),
        ("do the thing", "do  the\tthing"),
        ("", ""),
        ("", "x"),
        ("a", "b"),
        ("检查配额告警链路", "检查配额告警链路是否通"),
        (
            "retry job 4471 after the gateway probe went green",
            "retry job 4471 once the gateway probe is green",
        ),
        ("open PR for the quota fix", "open a PR for the quota fix"),
        ("alpha beta gamma", "zulu yankee xray"),
    ]

    @pytest.mark.parametrize(("a", "b"), CASES)
    def test_scores_match(self, a: str, b: str) -> None:
        if not PUMP_SRC.is_dir():
            pytest.skip("goal-agent source not present on this machine")
        sys.path.insert(0, str(PUMP_SRC))
        try:
            from goal_agent.pump import prompt_similarity as reference
        except ImportError:
            pytest.skip("goal-agent pump not importable")
        finally:
            sys.path.remove(str(PUMP_SRC))
        assert prompt_similarity(a, b) == pytest.approx(reference(a, b))


class TestBounds:
    def test_within_limit_is_allowed(self, guards: LineGuards) -> None:
        assert guards.bounds_exceeded(10) is None

    def test_past_max_rounds_stops(self, guards: LineGuards) -> None:
        assert "max_rounds" in (guards.bounds_exceeded(11) or "")

    def test_deadline_stops(self) -> None:
        guards = LineGuards(bounds=LineBounds(deadline_at=100.0))
        assert guards.bounds_exceeded(1, now=101.0) == "deadline exceeded"
        assert guards.bounds_exceeded(1, now=99.0) is None

    def test_deadline_ignored_when_unset(self, guards: LineGuards) -> None:
        assert guards.bounds_exceeded(1, now=1e12) is None


class TestExactRepeat:
    """INV-9."""

    def test_first_sighting_is_fresh(self, guards: LineGuards) -> None:
        check = guards.check_prompt("do the thing", 1)
        assert check.verdict is PromptVerdict.FRESH
        assert check.sha256 == prompt_sha256("do the thing")

    def test_second_sighting_is_refused(self, guards: LineGuards) -> None:
        first = guards.check_prompt("do the thing", 1)
        guards.accept_prompt(first, "do the thing", 1)
        # Something else in between, so this is not merely the previous prompt.
        second = guards.check_prompt("a completely different instruction", 2)
        guards.accept_prompt(second, "a completely different instruction", 2)

        repeat = guards.check_prompt("do the thing", 3)
        assert repeat.verdict is PromptVerdict.DUPLICATE
        assert repeat.first_seen_round == 1
        assert repeat.injectable is False

    def test_recording_a_refused_prompt_is_a_bug(self, guards: LineGuards) -> None:
        check = guards.check_prompt("x", 1)
        guards.accept_prompt(check, "x", 1)
        again = guards.check_prompt("x", 2)
        with pytest.raises(ValueError, match="refusing to record"):
            guards.accept_prompt(again, "x", 2)


class TestNearRepeat:
    """INV-9b -- the breaker plan.md's port list omitted."""

    LONG = (
        "Continue working the quota alert line. Check the exporter rule fires, "
        "confirm the alert reaches the channel, and record the evidence in the "
        "work folder."
    )

    def test_reworded_merry_go_round_is_caught(self, guards: LineGuards) -> None:
        """Literal text differs, sha differs, and the coordinator never
        self-reports no_progress. Only this catches it."""
        reworded = self.LONG.replace("record", "note")

        check = guards.check_prompt(self.LONG, 1)
        guards.accept_prompt(check, self.LONG, 1)

        assert prompt_sha256(reworded) != prompt_sha256(self.LONG), "sha dedup would miss this"
        second = guards.check_prompt(reworded, 2)
        assert second.verdict is PromptVerdict.NO_PROGRESS
        assert second.similarity >= DEFAULT_REPEAT_SIMILARITY

    def test_padding_a_prompt_does_not_escape_it(self, guards: LineGuards) -> None:
        check = guards.check_prompt(self.LONG, 1)
        guards.accept_prompt(check, self.LONG, 1)
        padded = self.LONG + " Report back when done."
        assert guards.check_prompt(padded, 2).verdict is PromptVerdict.NO_PROGRESS

    def test_a_genuinely_new_fact_passes(self, guards: LineGuards) -> None:
        """New ticket ids, errors and command output must not trip the breaker."""
        followup = (
            "The probe returned HTTP 502 from channel 13 at 04:12; investigate the "
            "gateway upstream and retry job 4471 once it is green."
        )
        check = guards.check_prompt(self.LONG, 1)
        guards.accept_prompt(check, self.LONG, 1)

        second = guards.check_prompt(followup, 2)
        assert second.verdict is PromptVerdict.FRESH
        assert second.similarity < DEFAULT_REPEAT_SIMILARITY

    def test_the_breaker_is_length_sensitive(self, guards: LineGuards) -> None:
        """Documents a real limit of the inherited threshold, measured not assumed.

        Jaccard over character bigrams dilutes a fixed-size edit across a
        longer text. One substituted word scores 0.929 in a ~180-char prompt
        (trips) but 0.878 in a ~68-char one (escapes). So INV-9b protects long
        coordinator prompts well and short ones poorly -- a short prompt can
        be reworded every round and slip past both INV-9 and INV-9b.

        Ported faithfully at the pump's 0.90 rather than "improved" here:
        the threshold decides when live lines stop, and retuning it belongs
        with evidence from real traffic, not a synthetic probe.
        """
        short = "Please check whether the quota alert pipeline is working end to end."
        short_reworded = short.replace("alert pipeline is working", "alerting pipeline works")

        assert prompt_similarity(self.LONG, self.LONG.replace("record", "note")) >= 0.90
        assert prompt_similarity(short, short_reworded) < 0.90

    def test_only_the_previous_prompt_is_compared(self, guards: LineGuards) -> None:
        """INV-9b is about adjacent rounds; older prompts are INV-9's job."""
        a = "Check whether the quota alert pipeline is working end to end."
        b = "Completely unrelated: rotate the gateway relay token and confirm."
        near_a = "Check whether the quota alerting pipeline works end to end."

        for text, rnd in ((a, 1), (b, 2)):
            check = guards.check_prompt(text, rnd)
            guards.accept_prompt(check, text, rnd)

        # near_a resembles round 1, not round 2, so INV-9b lets it through.
        assert guards.check_prompt(near_a, 3).verdict is PromptVerdict.FRESH

    def test_threshold_is_configurable(self) -> None:
        guards = LineGuards(bounds=LineBounds(repeat_similarity=0.1))
        check = guards.check_prompt("alpha beta", 1)
        guards.accept_prompt(check, "alpha beta", 1)
        assert guards.check_prompt("alpha gamma", 2).verdict is PromptVerdict.NO_PROGRESS


class TestStreaks:
    def test_noop_streak_trips_at_the_limit(self, guards: LineGuards) -> None:
        assert guards.streak_exceeded() is None
        for _ in range(3):
            guards.record_noop()
        assert "without progress" in (guards.streak_exceeded() or "")

    def test_progress_resets_the_noop_streak(self, guards: LineGuards) -> None:
        guards.record_noop()
        guards.record_noop()
        guards.record_progress()
        assert guards.noop_streak == 0
        assert guards.streak_exceeded() is None

    def test_timeout_streak_trips_at_its_own_limit(self, guards: LineGuards) -> None:
        guards.record_timeout()
        assert guards.streak_exceeded() is None
        guards.record_timeout()
        assert "timeouts" in (guards.streak_exceeded() or "")

    def test_a_good_turn_resets_the_timeout_streak(self, guards: LineGuards) -> None:
        guards.record_timeout()
        guards.record_turn_ok()
        assert guards.timeout_streak == 0


class TestNoSemanticInterpretation:
    """INV-3: this layer counts and compares text. It never reads meaning."""

    def test_verdict_words_in_a_prompt_change_nothing(self, guards: LineGuards) -> None:
        for text in ("done", "blocked", "STOP THE LINE", "verdict: done"):
            assert guards.check_prompt(text, 1).verdict is PromptVerdict.FRESH
            guards.prompts_seen.clear()
            guards.prev_prompt = None
