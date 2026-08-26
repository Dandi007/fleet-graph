"""Ignition gating and seat-aware probes -- the babysitter's rules, testable."""

from __future__ import annotations

from typing import Any

import pytest

from fleet_graph.scheduler.ignition import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_TOTAL_CAP,
    IgnitionDecision,
    LineStatus,
    Refusal,
    backoff_seconds,
    decide,
)
from fleet_graph.scheduler.probe import (
    SEAT_PROBES,
    GatewayProber,
    UnknownSeat,
    openai_probe,
    probe_for,
    responses_probe,
)

NOW = 1_787_000_000.0


def status(**kwargs: Any) -> LineStatus:
    base = {"folder_id": "wf-40fa8d", "seat": "opencode-gpt-terra"}
    base.update(kwargs)
    return LineStatus(**base)  # type: ignore[arg-type]


def call(st: LineStatus, **kwargs: Any) -> IgnitionDecision:
    params: dict[str, Any] = {
        "now": NOW,
        "enabled": True,
        "maintenance_stop": False,
        "zero_progress_streak": 0,
        "gateway_healthy": True,
        "total_started": 0,
    }
    params.update(kwargs)
    return decide(st, **params)


class TestIgnitionAllowed:
    def test_a_healthy_idle_line_ignites(self) -> None:
        assert call(status()).ignite is True

    def test_a_line_past_its_cooldown_ignites(self) -> None:
        st = status(last_start_at=NOW - DEFAULT_COOLDOWN_SECONDS - 1)
        assert call(st).ignite is True


class TestTheRoster:
    """Which lines this scheduler may start at all.

    The gate this replaces was an external file holding a live fleet down. It
    expired, and an expired flag is inert -- so "nobody renewed it" ignited
    everything. The roster inverts that: nothing runs unless something says it
    runs.
    """

    def test_a_line_that_is_not_enabled_does_not_ignite(self) -> None:
        decision = call(status(), enabled=False)
        assert decision.ignite is False
        assert decision.refusal is Refusal.LINE_DISABLED

    def test_the_roster_is_asked_before_the_fleet_wide_stop(self) -> None:
        """A line nobody put on the roster is not being held by maintenance;
        reporting `maintenance_stop` for it would send an operator to clear a
        flag that was never what stopped it."""
        decision = call(status(), enabled=False, maintenance_stop=True)
        assert decision.refusal is Refusal.LINE_DISABLED

    def test_being_off_the_roster_outranks_every_other_refusal(self) -> None:
        decision = call(
            status(running=True, terminal="done", last_start_at=NOW),
            enabled=False,
            gateway_healthy=False,
            total_started=DEFAULT_TOTAL_CAP,
        )
        assert decision.refusal is Refusal.LINE_DISABLED

    def test_enabling_a_line_is_not_by_itself_permission_to_run(self) -> None:
        """The roster says "allowed to be considered", not "start it". Every
        other gate still applies -- otherwise switching a line on during an
        incident would walk straight past the emergency stop."""
        decision = call(status(), enabled=True, maintenance_stop=True)
        assert decision.refusal is Refusal.MAINTENANCE_STOP


class TestAStalledLineBacksOff:
    """A line that keeps ending without advancing must not keep restarting.

    The canary blocked on a missing data source that only a human can supply,
    and the scheduler restarted it every 5 minutes to re-derive the same
    conclusion: 9 launches, `rounds.jsonl` still one line. Not silent -- it
    wrote a terminal each time -- but just as useless, and it burns tokens.
    """

    def test_a_clean_line_uses_the_plain_cooldown(self) -> None:
        assert backoff_seconds(300, 0) == 300

    def test_each_fruitless_run_doubles_the_wait(self) -> None:
        assert [backoff_seconds(300, n) for n in (1, 2, 3)] == [600, 1200, 2400]

    def test_the_wait_is_capped(self) -> None:
        """Unbounded doubling reaches "next year" in about a day, which is a
        latch wearing a backoff costume."""
        assert backoff_seconds(300, 40, cap=6 * 3600) == 6 * 3600

    def test_a_stalled_line_is_refused_under_its_own_label(self) -> None:
        """`cooling_down` reads as "it just ran". An operator scanning the log
        should be able to see "this one is stuck" without doing arithmetic on
        timestamps."""
        decision = call(status(last_start_at=NOW - 400), zero_progress_streak=3)
        assert decision.refusal is Refusal.NO_PROGRESS
        assert "without advancing a round" in decision.detail

    def test_the_backoff_does_expire(self) -> None:
        """The whole reason this is a backoff and not a latch: a blocker that
        clears on its own gets picked up with nobody unlatching anything."""
        assert call(status(last_start_at=NOW - 2401), zero_progress_streak=3).ignite is True

    def test_a_line_that_advanced_is_not_penalised(self) -> None:
        assert call(status(last_start_at=NOW - 301), zero_progress_streak=0).ignite is True


class TestRefusals:
    def test_maintenance_stop_wins_over_everything(self) -> None:
        """The fleet-wide off switch must not be defeated by any other state."""
        st = status(running=False, terminal=None)
        decision = call(st, maintenance_stop=True, gateway_healthy=True)
        assert decision.refusal is Refusal.MAINTENANCE_STOP

    def test_already_running_is_refused(self) -> None:
        """Two pumps on one line share a work folder and a worker seat."""
        assert call(status(running=True)).refusal is Refusal.ALREADY_RUNNING

    def test_a_done_line_is_not_restarted(self) -> None:
        assert call(status(terminal="done")).refusal is Refusal.TERMINAL_DONE

    def test_a_blocked_line_may_still_ignite(self) -> None:
        """Only `done` is final; blocked lines are expected to be retried."""
        assert call(status(terminal="blocked")).ignite is True

    def test_cooldown_is_enforced(self) -> None:
        st = status(last_start_at=NOW - 10)
        decision = call(st)
        assert decision.refusal is Refusal.COOLING_DOWN
        assert "s of cooldown left" in decision.detail

    def test_global_cap_stops_a_restart_storm(self) -> None:
        decision = call(status(), total_started=DEFAULT_TOTAL_CAP)
        assert decision.refusal is Refusal.TOTAL_CAP_REACHED

    def test_red_gateway_blocks_ignition(self) -> None:
        assert call(status(), gateway_healthy=False).refusal is Refusal.GATEWAY_RED

    def test_an_unprobeable_seat_is_refused_not_waved_through(self) -> None:
        """Unknown health is not good health."""
        assert call(status(), gateway_healthy=None).refusal is Refusal.NO_PROBE


class TestRefusalOrder:
    def test_a_stopped_fleet_costs_no_probe(self) -> None:
        """Cheap certain refusals come first; the probe is the expensive one."""
        decision = call(status(running=True), maintenance_stop=True, gateway_healthy=False)
        assert decision.refusal is Refusal.MAINTENANCE_STOP

    def test_running_beats_cooldown(self) -> None:
        st = status(running=True, last_start_at=NOW - 1)
        assert call(st).refusal is Refusal.ALREADY_RUNNING


class TestSeatAwareProbes:
    def test_research_seat_uses_the_openai_face(self) -> None:
        spec = probe_for("opencode-dsv4pro")
        assert spec.path == "/v1/chat/completions"
        assert spec.body["model"] == "deepseek-v4-pro"

    def test_subscription_seats_use_the_responses_face(self) -> None:
        for seat in ("opencode-gpt-terra", "opencode-gpt-sol"):
            assert probe_for(seat).path == "/v1/responses"

    def test_responses_probe_must_stream(self) -> None:
        """The subscription channel only accepts streaming; a non-streaming
        probe fails for the wrong reason and reads as a dead upstream."""
        for seat in ("opencode-gpt-terra", "opencode-gpt-sol"):
            assert probe_for(seat).body["stream"] is True

    def test_each_seat_probes_its_own_model(self) -> None:
        """terra alive while sol is dead must not let a sol line ignite."""
        assert probe_for("opencode-gpt-terra").body["model"] == "gpt-5.6-terra"
        assert probe_for("opencode-gpt-sol").body["model"] == "gpt-5.6-sol"

    def test_an_unregistered_seat_raises_rather_than_borrowing(self) -> None:
        with pytest.raises(UnknownSeat, match="rather than"):
            probe_for("opencode-something-new")

    def test_every_registered_seat_has_a_model_and_markers(self) -> None:
        for seat, spec in SEAT_PROBES.items():
            assert spec.body.get("model"), seat
            assert spec.healthy_markers, seat


ENV = {
    "FLEET_GRAPH_GATEWAY_TOKEN": "openai-lane-token",
    "FLEET_GRAPH_GATEWAY_TOKEN_RESPONSES": "responses-lane-token",
}


class FakeTransport:
    def __init__(self, status_code: int = 200, raw: str = "", raises: Exception | None = None):
        self.status_code = status_code
        self.raw = raw
        self.raises = raises
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    def post(self, url: str, body: dict[str, Any], token: str) -> tuple[int, str]:
        self.calls.append((url, body, token))
        if self.raises is not None:
            raise self.raises
        return self.status_code, self.raw


class TestProberBehaviour:
    def test_healthy_openai_response(self) -> None:
        prober = GatewayProber(FakeTransport(200, '{"choices": [{"text": "pong"}]}'), env=ENV)
        assert prober.check("opencode-dsv4pro") is True

    def test_healthy_streaming_responses_frame(self) -> None:
        prober = GatewayProber(FakeTransport(200, 'data: {"status": "completed"}\n\n'), env=ENV)
        assert prober.check("opencode-gpt-terra") is True

    def test_wrong_shaped_body_is_red(self) -> None:
        prober = GatewayProber(FakeTransport(200, '{"error": "no channel available"}'), env=ENV)
        assert prober.check("opencode-gpt-terra") is False

    def test_non_2xx_is_red(self) -> None:
        prober = GatewayProber(FakeTransport(502, "bad gateway"), env=ENV)
        assert prober.check("opencode-dsv4pro") is False

    def test_unreachable_gateway_is_red_not_an_exception(self) -> None:
        prober = GatewayProber(FakeTransport(raises=OSError("connection refused")), env=ENV)
        assert prober.check("opencode-dsv4pro") is False

    def test_probe_targets_the_loopback_gateway(self) -> None:
        transport = FakeTransport(200, '{"choices": []}')
        GatewayProber(transport, env=ENV).check("opencode-dsv4pro")
        assert transport.calls[0][0].startswith("http://127.0.0.1:15722")

    def test_unknown_seat_propagates(self) -> None:
        prober = GatewayProber(FakeTransport(200, ""), env=ENV)
        with pytest.raises(UnknownSeat):
            prober.check("opencode-mystery")


class TestProbeSpecHelpers:
    def test_openai_marker(self) -> None:
        assert openai_probe("m").is_healthy('{"choices": []}') is True
        assert openai_probe("m").is_healthy("{}") is False

    def test_responses_accepts_both_spacings(self) -> None:
        spec = responses_probe("m")
        assert spec.is_healthy('{"status": "completed"}') is True
        assert spec.is_healthy('{"status":"completed"}') is True


class TestCredentialLanes:
    """A face is an endpoint *and* a credential.

    Measured on the live gateway: probing /v1/responses with the OpenAI-lane
    token returns 503 'No available channel for model gpt-5.6-sol under group
    anthropic' while the seat is perfectly healthy. Tokens are scoped to
    channel groups, so the wrong one manufactures a false negative -- the exact
    defect class seat-aware probing exists to prevent.
    """

    def test_each_face_names_its_own_lane(self) -> None:
        assert probe_for("opencode-dsv4pro").token_env == "FLEET_GRAPH_GATEWAY_TOKEN"
        for seat in ("opencode-gpt-terra", "opencode-gpt-sol"):
            assert probe_for(seat).token_env == "FLEET_GRAPH_GATEWAY_TOKEN_RESPONSES"

    def test_the_lane_token_is_the_one_sent(self) -> None:
        transport = FakeTransport(200, 'data: {"status": "completed"}')
        GatewayProber(transport, env=ENV).check("opencode-gpt-terra")
        assert transport.calls[0][2] == "responses-lane-token"

    def test_openai_seat_sends_the_openai_token(self) -> None:
        transport = FakeTransport(200, '{"choices": []}')
        GatewayProber(transport, env=ENV).check("opencode-dsv4pro")
        assert transport.calls[0][2] == "openai-lane-token"

    def test_a_missing_lane_credential_raises_rather_than_probing(self) -> None:
        """Silently falling back to another lane is how the false negative happens."""
        from fleet_graph.scheduler.probe import MissingProbeCredential

        transport = FakeTransport(200, "")
        prober = GatewayProber(transport, env={"FLEET_GRAPH_GATEWAY_TOKEN": "x"})
        with pytest.raises(MissingProbeCredential, match="reports healthy seats as dead"):
            prober.check("opencode-gpt-sol")
        assert transport.calls == []
