"""Tests for the native OpenAI adapter.

Mirrors test_anthropic_provider.py: a mock client that mimics the
``openai.AsyncOpenAI`` shape we depend on, so tests stay hermetic
and don't burn API credits. Real-API smoke is gated by
``@pytest.mark.smoke`` (nightly only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from movate.core.failures import (
    AuthError,
    ContentFilterError,
    ContextLengthError,
    ModelUnavailableError,
    MovateTimeoutError,
    SchemaError,
)
from movate.core.failures import RateLimitError as MovateRateLimitError
from movate.providers.base import CompletionRequest, Message
from movate.providers.openai_native import OpenAIProvider

# ---------------------------------------------------------------------------
# Fakes that mimic the openai SDK surface
# ---------------------------------------------------------------------------


@dataclass
class _FakePromptDetails:
    cached_tokens: int = 0


@dataclass
class _FakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: _FakePromptDetails = field(default_factory=_FakePromptDetails)


@dataclass
class _FakeMessage:
    content: str = ""


@dataclass
class _FakeChoice:
    message: _FakeMessage = field(default_factory=_FakeMessage)
    finish_reason: str = "stop"


@dataclass
class _FakeChatCompletion:
    choices: list[_FakeChoice]
    usage: _FakeUsage
    model: str = "gpt-4o-mini-2024-07-18"


@dataclass
class _FakeDelta:
    content: str = ""


@dataclass
class _FakeStreamChoice:
    delta: _FakeDelta | None = None


@dataclass
class _FakeChatChunk:
    choices: list[_FakeStreamChoice] = field(default_factory=list)
    usage: _FakeUsage | None = None


class _FakeAsyncIter:
    def __init__(self, chunks: list[_FakeChatChunk]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> _FakeAsyncIter:
        self._cursor = 0
        return self

    async def __anext__(self) -> _FakeChatChunk:
        if self._cursor >= len(self._chunks):
            raise StopAsyncIteration
        c = self._chunks[self._cursor]
        self._cursor += 1
        return c


@dataclass
class _FakeChatCompletions:
    create_response: _FakeChatCompletion | None = None
    create_exc: Exception | None = None
    stream_chunks: list[_FakeChatChunk] = field(default_factory=list)
    stream_exc: Exception | None = None
    last_create_call: dict[str, Any] = field(default_factory=dict)

    async def create(self, **kwargs: Any) -> _FakeChatCompletion | _FakeAsyncIter:
        self.last_create_call = kwargs
        if kwargs.get("stream"):
            if self.stream_exc is not None:
                raise self.stream_exc
            return _FakeAsyncIter(self.stream_chunks)
        if self.create_exc is not None:
            raise self.create_exc
        assert self.create_response is not None
        return self.create_response


@dataclass
class _FakeChat:
    completions: _FakeChatCompletions = field(default_factory=_FakeChatCompletions)


@dataclass
class _FakeClient:
    chat: _FakeChat = field(default_factory=_FakeChat)


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pricing_key_prepends_openai_prefix() -> None:
    """Native-OpenAI agents declare bare model ids in agent.yaml
    (``gpt-4o-mini-2024-07-18``), but pricing.yaml uses LiteLLM-style
    keys (``openai/gpt-4o-mini-2024-07-18``). The adapter bridges.

    ``azure/...`` prefixes pass through unchanged because Azure-OpenAI
    deployments use the same pricing table entries with the azure prefix."""
    provider = OpenAIProvider(client=_FakeClient())  # type: ignore[arg-type]
    assert provider.pricing_key("gpt-4o-mini-2024-07-18") == "openai/gpt-4o-mini-2024-07-18"
    assert provider.pricing_key("openai/gpt-4o") == "openai/gpt-4o"
    # Azure deployments use the azure/ prefix in pricing.yaml.
    assert provider.pricing_key("azure/gpt-4.1") == "azure/gpt-4.1"


@pytest.mark.unit
async def test_complete_happy_path() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice(message=_FakeMessage(content="hi there"))],
        usage=_FakeUsage(prompt_tokens=11, completion_tokens=3),
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    resp = await provider.complete(
        CompletionRequest(provider="gpt-4o-mini", messages=[Message(role="user", content="hi")])
    )
    assert resp.text == "hi there"
    assert resp.tokens.input == 11
    assert resp.tokens.output == 3


@pytest.mark.unit
async def test_complete_passes_messages_and_params() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice()], usage=_FakeUsage()
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    await provider.complete(
        CompletionRequest(
            provider="gpt-4o-mini",
            messages=[
                Message(role="system", content="be concise"),
                Message(role="user", content="hi"),
            ],
            params={"temperature": 0.3, "max_tokens": 256},
        )
    )
    # OpenAI takes system as a message (unlike Anthropic) — both go in.
    assert fake.chat.completions.last_create_call["messages"] == [
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": "hi"},
    ]
    assert fake.chat.completions.last_create_call["temperature"] == 0.3


@pytest.mark.unit
async def test_complete_extracts_cached_tokens() -> None:
    """``usage.prompt_tokens_details.cached_tokens`` maps to
    ``TokenUsage.cached_input`` — used for prompt-caching cost math."""
    fake = _FakeClient()
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice(message=_FakeMessage(content="ok"))],
        usage=_FakeUsage(
            prompt_tokens=200,
            completion_tokens=10,
            prompt_tokens_details=_FakePromptDetails(cached_tokens=150),
        ),
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    resp = await provider.complete(
        CompletionRequest(provider="gpt-4o-mini", messages=[Message(role="user", content="hi")])
    )
    assert resp.tokens.cached_input == 150


# ---------------------------------------------------------------------------
# Exception translation
# ---------------------------------------------------------------------------


class _StubError(Exception):
    pass


class AuthenticationError(_StubError):
    pass


class RateLimitError(_StubError):
    def __init__(self, msg: str, retry_after: float | None = None) -> None:
        super().__init__(msg)
        if retry_after is not None:
            from types import SimpleNamespace  # noqa: PLC0415

            self.response = SimpleNamespace(headers={"retry-after": str(retry_after)})


class APITimeoutError(_StubError):
    pass


class BadRequestError(_StubError):
    pass


class APIConnectionError(_StubError):
    pass


@pytest.mark.unit
async def test_exception_auth() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = AuthenticationError("bad key")
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(AuthError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_rate_limit_carries_retry_after() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = RateLimitError("slow", retry_after=7.0)
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(MovateRateLimitError) as exc_info:
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )
    assert exc_info.value.retry_after == 7.0


@pytest.mark.unit
async def test_exception_timeout() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = APITimeoutError("timed out")
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(MovateTimeoutError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_context_length() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = BadRequestError(
        "message is too long for the model's context window"
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ContextLengthError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_content_filter() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = BadRequestError("blocked by content policy")
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ContentFilterError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_bad_request_falls_through_to_schema_error() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = BadRequestError("invalid params")
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_connection_is_model_unavailable() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = APIConnectionError("network error")
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ModelUnavailableError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_stream_yields_text_chunks_and_final_usage() -> None:
    fake = _FakeClient()
    fake.chat.completions.stream_chunks = [
        _FakeChatChunk(choices=[_FakeStreamChoice(delta=_FakeDelta(content="hello "))]),
        _FakeChatChunk(choices=[_FakeStreamChoice(delta=_FakeDelta(content="world"))]),
        # Final chunk has no text but populated usage.
        _FakeChatChunk(usage=_FakeUsage(prompt_tokens=5, completion_tokens=2)),
    ]
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    chunks = []
    async for chunk in provider.stream(
        CompletionRequest(provider="gpt-4o-mini", messages=[Message(role="user", content="hi")])
    ):
        chunks.append(chunk)
    # Two text chunks + one usage-only final.
    assert len(chunks) == 3
    assert chunks[0].text == "hello "
    assert chunks[1].text == "world"
    assert chunks[2].text == ""
    assert chunks[2].tokens is not None
    assert chunks[2].tokens.input == 5


@pytest.mark.unit
async def test_stream_forces_include_usage_option() -> None:
    """The adapter MUST set ``stream_options={'include_usage': True}``
    even if the user didn't — otherwise cost accounting downstream
    reads zero."""
    fake = _FakeClient()
    fake.chat.completions.stream_chunks = []
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    async for _ in provider.stream(
        CompletionRequest(provider="gpt-4o-mini", messages=[Message(role="user", content="hi")])
    ):
        pass

    call = fake.chat.completions.last_create_call
    assert call["stream"] is True
    assert call["stream_options"]["include_usage"] is True


# ---------------------------------------------------------------------------
# Optional-dep gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_without_client_raises_import_error_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the openai package isn't installed AND no client is injected,
    construction raises ImportError with the install hint."""
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "openai":
            raise ImportError("no module named openai")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match=r"movate-cli\[openai\]"):
        OpenAIProvider()
