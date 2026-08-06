"""RFC 0088 T4 / RFC 0093 P5 tests for the BinKeeper vision worker (mocked client)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.request import Request

import pytest

from binkeeper.bin_vision import (
    BinVisionError,
    GeminiVisionClient,
    OllamaVisionClient,
    default_vision_client,
    propose_bin_label,
)


class _FakeVisionClient:
    """Returns canned raw model text per call, in order."""

    model = "qwen3-vl:test"

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls = 0

    def analyze(self, prompt: str, image: bytes) -> str:
        raw = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return raw


_ONE_PHOTO = (
    '{"items": [{"label": "hex keys", "traits": ["metal"], "confidence": 0.9}, '
    '{"label": "blurry thing", "confidence": 0.1}], '
    '"theme": "hand tools", "summary": "small hand tools"}'
)


def test_proposes_label_and_applies_confidence_floor() -> None:
    client = _FakeVisionClient([_ONE_PHOTO])

    proposal = propose_bin_label(client, [b"photo"], notes="keep near the workbench")

    assert client.calls == 1
    assert proposal.theme == "hand tools"
    # The low-confidence item is dropped by the floor; the strong one is kept.
    assert proposal.accepts == ("hex keys",)
    assert proposal.owner_phrase == "keep near the workbench"
    assert proposal.model_version == "qwen3-vl:test"
    assert proposal.photo_count == 1


def test_merges_items_across_photos_keeping_the_higher_confidence() -> None:
    photo_a = '{"items": [{"label": "calipers", "confidence": 0.5}], "theme": "measuring"}'
    photo_b = (
        '{"items": [{"label": "calipers", "confidence": 0.95}, '
        '{"label": "micrometer", "confidence": 0.8}], "theme": "measuring"}'
    )
    client = _FakeVisionClient([photo_a, photo_b])

    proposal = propose_bin_label(client, [b"a", b"b"])

    assert client.calls == 2
    labels = {item.label: item.confidence for item in proposal.items}
    assert labels["calipers"] == pytest.approx(0.95)  # higher wins across photos
    assert "micrometer" in labels
    assert proposal.theme == "measuring"
    assert proposal.photo_count == 2


def test_parses_json_out_of_reasoning_prefixed_output() -> None:
    reasoning = "<think>Let me look... I see cables.</think>\n" + _ONE_PHOTO
    client = _FakeVisionClient([reasoning])

    proposal = propose_bin_label(client, [b"photo"])

    assert "hex keys" in proposal.accepts


def test_tolerates_one_unparseable_photo_among_several() -> None:
    # A single bad photo must not sink the batch: the good one still proposes.
    good = '{"items": [{"label": "drill bits", "confidence": 0.9}], "theme": "power tools"}'
    client = _FakeVisionClient(["sorry, no idea", good])

    proposal = propose_bin_label(client, [b"bad", b"good"])

    assert proposal.theme == "power tools"
    assert "drill bits" in proposal.accepts
    assert proposal.photo_count == 2


def test_all_unparseable_output_raises() -> None:
    client = _FakeVisionClient(["I could not read the image, sorry."])
    with pytest.raises(BinVisionError):
        propose_bin_label(client, [b"photo"])


def test_no_photos_raises() -> None:
    client = _FakeVisionClient([_ONE_PHOTO])
    with pytest.raises(BinVisionError, match="at least one photo"):
        propose_bin_label(client, [])


class _FakeHttpResponse:
    def __init__(self, message: Mapping[str, object] | None = None) -> None:
        self._message = (
            dict(message)
            if message is not None
            else {"content": '{"items": [], "theme": "empty", "summary": ""}'}
        )

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps({"choices": [{"message": self._message}]}).encode("utf-8")


def _request_payload(request: Request) -> dict[str, object]:
    encoded = request.data
    assert isinstance(encoded, bytes)
    payload = json.loads(encoded.decode("utf-8"))
    assert isinstance(payload, dict)
    return {str(key): value for key, value in payload.items()}


# Regression 2026-07-13: the 32B default spent over a minute generating hidden
# reasoning. Ask Ollama for bounded JSON and its no-thinking mode on every request.
def test_ollama_request_asks_for_no_thinking_and_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: Request, *, timeout: float) -> _FakeHttpResponse:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = _request_payload(request)
        return _FakeHttpResponse()

    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", fake_urlopen)
    client = OllamaVisionClient(
        endpoint="http://peecee:11434/v1",
        model="qwen3-vl:8b",
        max_tokens=768,
        timeout_s=45,
    )

    client.analyze("Describe this bin", b"not-a-real-image")

    assert captured["url"] == "http://peecee:11434/v1/chat/completions"
    assert captured["timeout"] == 45
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["think"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 768


def test_ollama_uses_reasoning_json_when_content_is_non_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: Request, *, timeout: float) -> _FakeHttpResponse:
        return _FakeHttpResponse(
            {
                "content": "I inspected the photo.",
                "reasoning": _ONE_PHOTO,
            }
        )

    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", fake_urlopen)
    client = OllamaVisionClient(endpoint="http://peecee:11434/v1")

    proposal = propose_bin_label(client, [b"not-a-real-image"])

    assert proposal.theme == "hand tools"


# Regression 2026-07-13: a stored phone photo returned reasoning without JSON.
def test_non_json_model_output_recovers_with_larger_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: Request, *, timeout: float) -> _FakeHttpResponse:
        payload = _request_payload(request)
        max_tokens = payload["max_tokens"]
        assert isinstance(max_tokens, int)
        message = (
            {"content": _ONE_PHOTO, "reasoning": ""}
            if max_tokens >= 4096
            else {"content": "", "reasoning": "still inspecting the photo"}
        )
        return _FakeHttpResponse(message)

    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", fake_urlopen)
    client = OllamaVisionClient(
        endpoint="http://peecee:11434/v1",
        max_tokens=3072,
    )

    proposal = propose_bin_label(client, [b"not-a-real-image"])

    assert proposal.theme == "hand tools"


def test_non_json_retry_uses_remaining_request_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((100.0, 112.0))
    timeouts: list[float] = []

    def fake_urlopen(request: Request, *, timeout: float) -> _FakeHttpResponse:
        timeouts.append(timeout)
        message = (
            {"content": "", "reasoning": "still inspecting the photo"}
            if timeout == 60
            else {"content": _ONE_PHOTO, "reasoning": ""}
        )
        return _FakeHttpResponse(message)

    monkeypatch.setattr("binkeeper.bin_vision.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", fake_urlopen)
    client = OllamaVisionClient(endpoint="http://peecee:11434/v1", timeout_s=60)

    proposal = propose_bin_label(client, [b"not-a-real-image"])

    assert proposal.theme == "hand tools"
    assert timeouts == [60, 48]


# Regression 2026-07-29: a socket read timeout raises the builtin TimeoutError,
# which is an OSError but NOT a urllib URLError, so it escaped _request's
# handlers and surfaced as an unhandled 500 on the photo-drop page -- losing the
# form even though the photos had already been stored. Vision is advisory; a
# slow model must degrade to an owner-readable message like every other failure.
def test_read_timeout_becomes_a_vision_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Request, *, timeout: float) -> _FakeHttpResponse:
        raise TimeoutError("timed out")

    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", fake_urlopen)
    client = OllamaVisionClient(endpoint="http://peecee:11434/v1", timeout_s=60)

    with pytest.raises(BinVisionError, match="did not respond within 60"):
        propose_bin_label(client, [b"not-a-real-image"])


class _FakeGeminiResponse:
    def __init__(self, text: str | None = '{"items": [], "theme": "empty", "summary": ""}') -> None:
        self._text = text

    def __enter__(self) -> _FakeGeminiResponse:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        return None

    def read(self) -> bytes:
        parts = [] if self._text is None else [{"text": self._text}]
        return json.dumps({"candidates": [{"content": {"parts": parts}}]}).encode("utf-8")


# ADR 0004: the cloud backend sends only the inference image and prompt text,
# authenticates via header, and asks Gemini for bounded JSON output.
def test_gemini_request_shape_and_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: Request, *, timeout: float) -> _FakeGeminiResponse:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["key"] = request.get_header("X-goog-api-key")
        captured["payload"] = _request_payload(request)
        return _FakeGeminiResponse(_ONE_PHOTO)

    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", fake_urlopen)
    client = GeminiVisionClient(model="gemini-test", api_key="synthetic-key", timeout_s=45)

    proposal = propose_bin_label(client, [b"not-a-real-image"])

    assert proposal.theme == "hand tools"
    assert captured["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
    )
    assert captured["timeout"] == 45
    assert captured["key"] == "synthetic-key"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    config = payload["generationConfig"]
    assert isinstance(config, dict)
    assert config["responseMimeType"] == "application/json"
    contents = payload["contents"]
    assert isinstance(contents, list)
    part_keys = {key for part in contents[0]["parts"] for key in part}
    assert part_keys == {"inline_data", "text"}


def test_gemini_missing_key_is_a_vision_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BINKEEPER_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client = GeminiVisionClient()

    with pytest.raises(BinVisionError, match="no Gemini API key"):
        client.analyze("Describe this bin", b"not-a-real-image")


def test_gemini_http_error_is_a_vision_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error

    def fake_urlopen(request: Request, *, timeout: float) -> _FakeGeminiResponse:
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"error": {"message": "model retired"}}'),
        )

    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", fake_urlopen)
    client = GeminiVisionClient(api_key="synthetic-key")

    with pytest.raises(BinVisionError, match=r"HTTP 404.*model retired"):
        client.analyze("Describe this bin", b"not-a-real-image")


def test_gemini_read_timeout_is_a_vision_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Request, *, timeout: float) -> _FakeGeminiResponse:
        raise TimeoutError("timed out")

    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", fake_urlopen)
    client = GeminiVisionClient(api_key="synthetic-key", timeout_s=60)

    with pytest.raises(BinVisionError, match="did not respond within 60"):
        client.analyze("Describe this bin", b"not-a-real-image")


def test_gemini_empty_candidates_is_a_vision_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Request, *, timeout: float) -> _FakeGeminiResponse:
        return _FakeGeminiResponse(None)

    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", fake_urlopen)
    client = GeminiVisionClient(api_key="synthetic-key")

    with pytest.raises(BinVisionError, match="missing message content"):
        client.analyze("Describe this bin", b"not-a-real-image")


def test_default_vision_client_selects_the_configured_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BINKEEPER_OPENROUTER_API_KEY", "synthetic-key")
    openrouter = default_vision_client("openrouter")
    assert isinstance(openrouter, OllamaVisionClient)
    assert openrouter.endpoint == "https://openrouter.ai/api/v1"
    assert openrouter.api_key == "synthetic-key"
    assert isinstance(default_vision_client("gemini"), GeminiVisionClient)
    local = default_vision_client("local")
    assert isinstance(local, OllamaVisionClient)
    assert local.api_key is None
    with pytest.raises(BinVisionError, match="unsupported vision provider"):
        default_vision_client("mystery")


def test_openrouter_provider_without_key_is_a_vision_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BINKEEPER_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(BinVisionError, match="no OpenRouter API key"):
        default_vision_client("openrouter")


def test_openai_compatible_client_sends_bearer_token_when_keyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: Request, *, timeout: float) -> _FakeHttpResponse:
        captured["auth"] = request.get_header("Authorization")
        return _FakeHttpResponse()

    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", fake_urlopen)
    client = OllamaVisionClient(
        endpoint="https://openrouter.ai/api/v1",
        model="some/vision-model",
        api_key="synthetic-key",
    )

    client.analyze("Describe this bin", b"not-a-real-image")

    assert captured["auth"] == "Bearer synthetic-key"
