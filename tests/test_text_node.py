"""TextNode: both gateway protocol faces, against a recording transport."""

from __future__ import annotations

from typing import Any

import pytest

from fleet_graph.executors.text_node import (
    DEFAULT_GATEWAY_URL,
    EmptyCompletion,
    GatewayError,
    TextNode,
    TextSpec,
    load_gateway_token,
)


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[tuple[int, Any]] = []

    def queue(self, status: int, body: Any) -> None:
        self.responses.append((status, body))

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        self.calls.append({"method": method, "url": url, "headers": headers, "body": json_body})
        return self.responses.pop(0) if self.responses else (200, {})


OPENAI_OK = {
    "model": "deepseek-v4-flash-ga-260731",
    "choices": [{"message": {"role": "assistant", "content": "Paris"}}],
    "usage": {"total_tokens": 108},
}

ANTHROPIC_OK = {
    "model": "claude-haiku-4-5-20251001",
    "content": [{"type": "text", "text": "Paris"}],
    "usage": {"output_tokens": 4},
}


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def node(transport: RecordingTransport) -> TextNode:
    return TextNode(token="tok", transport=transport)


class TestCredentials:
    def test_from_env(self) -> None:
        assert load_gateway_token({"FLEET_GRAPH_GATEWAY_TOKEN": " t \n"}) == "t"

    def test_from_file(self, tmp_path) -> None:
        path = tmp_path / "gw.token"
        path.write_text("filetok\n")
        assert load_gateway_token({"FLEET_GRAPH_GATEWAY_TOKEN_FILE": str(path)}) == "filetok"

    def test_missing_is_an_error_not_a_default(self) -> None:
        with pytest.raises(RuntimeError, match="no gateway credential"):
            load_gateway_token({})


class TestOpenAIFace:
    def test_request_shape(self, node: TextNode, transport: RecordingTransport) -> None:
        transport.queue(200, OPENAI_OK)
        node.complete(TextSpec(model="deepseek-v4-flash", system="be terse", temperature=0.2), "hi")
        call = transport.calls[0]
        assert call["url"] == f"{DEFAULT_GATEWAY_URL}/v1/chat/completions"
        assert call["body"]["model"] == "deepseek-v4-flash"
        assert call["body"]["messages"] == [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ]
        assert call["body"]["temperature"] == 0.2

    def test_temperature_omitted_when_unset(
        self, node: TextNode, transport: RecordingTransport
    ) -> None:
        transport.queue(200, OPENAI_OK)
        node.complete(TextSpec(model="m"), "hi")
        assert "temperature" not in transport.calls[0]["body"]

    def test_response_parsing(self, node: TextNode, transport: RecordingTransport) -> None:
        transport.queue(200, OPENAI_OK)
        result = node.complete(TextSpec(model="deepseek-v4-flash"), "hi")
        assert result.text == "Paris"
        assert result.model == "deepseek-v4-flash-ga-260731"
        assert result.usage["total_tokens"] == 108

    def test_empty_choices_raises(self, node: TextNode, transport: RecordingTransport) -> None:
        transport.queue(200, {"choices": []})
        with pytest.raises(GatewayError, match="no choices"):
            node.complete(TextSpec(model="m"), "hi")


class TestAnthropicFace:
    def test_request_shape(self, node: TextNode, transport: RecordingTransport) -> None:
        transport.queue(200, ANTHROPIC_OK)
        node.complete(
            TextSpec(model="claude-haiku-4-5-20251001", face="anthropic", system="s"), "hi"
        )
        call = transport.calls[0]
        assert call["url"] == f"{DEFAULT_GATEWAY_URL}/v1/messages"
        assert call["body"]["system"] == "s"
        assert call["body"]["messages"] == [{"role": "user", "content": "hi"}]
        assert call["headers"]["anthropic-version"] == "2023-06-01"
        assert call["headers"]["x-api-key"] == "tok"

    def test_concatenates_text_blocks_and_ignores_others(
        self, node: TextNode, transport: RecordingTransport
    ) -> None:
        transport.queue(
            200,
            {
                "model": "m",
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "Pa"},
                    {"type": "text", "text": "ris"},
                ],
            },
        )
        assert node.complete(TextSpec(model="m", face="anthropic"), "hi").text == "Paris"


class TestErrors:
    def test_non_2xx_raises_with_status(
        self, node: TextNode, transport: RecordingTransport
    ) -> None:
        transport.queue(429, {"error": "rate limited"})
        with pytest.raises(GatewayError) as excinfo:
            node.complete(TextSpec(model="m"), "hi")
        assert excinfo.value.status == 429

    def test_non_object_body_raises(self, node: TextNode, transport: RecordingTransport) -> None:
        transport.queue(200, "<html>gateway down</html>")
        with pytest.raises(GatewayError, match="expected an object"):
            node.complete(TextSpec(model="m"), "hi")


class TestInvariantThree:
    """Model access is single-point, and code names only logical models."""

    def test_every_call_goes_to_the_loopback_gateway(
        self, node: TextNode, transport: RecordingTransport
    ) -> None:
        transport.queue(200, OPENAI_OK)
        transport.queue(200, ANTHROPIC_OK)
        node.complete(TextSpec(model="m"), "hi")
        node.complete(TextSpec(model="m", face="anthropic"), "hi")
        assert all(c["url"].startswith("http://127.0.0.1:15722") for c in transport.calls)

    def test_traffic_is_attributable(self, node: TextNode, transport: RecordingTransport) -> None:
        """DoD-6: gateway traffic must trace back to fleet-graph."""
        transport.queue(200, OPENAI_OK)
        node.complete(TextSpec(model="m"), "hi")
        assert transport.calls[0]["headers"]["X-Fleet-Graph-Component"] == "fleet-graph"

    def test_transport_does_not_trust_proxy_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fleet_graph.executors.text_node import HttpxTransport

        monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7891")
        assert HttpxTransport()._client.trust_env is False


class TestEmptyCompletionGuard:
    """A reasoning model can burn the whole budget and emit nothing.

    Observed live: glm-4.6 spent all 512 max_tokens on reasoning_tokens and
    returned an empty string, and hello-graph reported success with an empty
    critique. Propagating that is how a line stalls while looking healthy.
    """

    def test_empty_openai_completion_raises(
        self, node: TextNode, transport: RecordingTransport
    ) -> None:
        transport.queue(
            200,
            {
                "model": "glm-4.6",
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"completion_tokens": 512},
            },
        )
        with pytest.raises(EmptyCompletion) as excinfo:
            node.complete(TextSpec(model="glm-4.6"), "hi")
        assert excinfo.value.finish_reason == "length"
        assert excinfo.value.model == "glm-4.6"

    def test_whitespace_only_counts_as_empty(
        self, node: TextNode, transport: RecordingTransport
    ) -> None:
        transport.queue(200, {"model": "m", "choices": [{"message": {"content": "   \n"}}]})
        with pytest.raises(EmptyCompletion):
            node.complete(TextSpec(model="m"), "hi")

    def test_empty_anthropic_completion_raises(
        self, node: TextNode, transport: RecordingTransport
    ) -> None:
        transport.queue(
            200,
            {
                "model": "m",
                "content": [{"type": "thinking", "thinking": "..."}],
                "stop_reason": "max_tokens",
            },
        )
        with pytest.raises(EmptyCompletion) as excinfo:
            node.complete(TextSpec(model="m", face="anthropic"), "hi")
        assert excinfo.value.finish_reason == "max_tokens"

    def test_opt_out_returns_the_empty_result(
        self, node: TextNode, transport: RecordingTransport
    ) -> None:
        transport.queue(200, {"model": "m", "choices": [{"message": {"content": ""}}]})
        result = node.complete(TextSpec(model="m", require_text=False), "hi")
        assert result.text == ""

    def test_finish_reason_is_carried_on_success(
        self, node: TextNode, transport: RecordingTransport
    ) -> None:
        transport.queue(
            200,
            {"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )
        assert node.complete(TextSpec(model="m"), "hi").finish_reason == "stop"
