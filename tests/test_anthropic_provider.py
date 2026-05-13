"""Tests for the native Anthropic adapter.

These tests use a mock client (``FakeAnthropicClient`` below) that
mimics the ``anthropic.AsyncAnthropic`` shape we depend on — that
way we don't burn real Anthropic credits and the suite stays hermetic.

Real-API smoke is gated by ``@pytest.mark.smoke`` (nightly only,
see test_smoke_litellm for the precedent)."""

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
from movate.providers.anthropic import AnthropicProvider
from movate.providers.base import CompletionRequest, Message

# ---------------------------------------------------------------------------
# Fakes that mimic the anthropic SDK surface we depend on
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeMessage:
    content: list[_FakeTextBlock]
    usage: _FakeUsage
    model: str = "claude-haiku-4-5-20251001"
    stop_reason: str = "end_turn"


@dataclass
class _FakeContentBlockDelta:
    text: str = ""
    type: str = "text_delta"


@dataclass
class _FakeStreamEvent:
    type: str
    delta: _FakeContentBlockDelta | None = None


class _FakeStreamContext:
    """Mimics the async context manager returned by ``messages.stream``."""

    def __init__(self, events: list[_FakeStreamEvent], final_message: _FakeMessage) -> None:
        self._events = events
        self._final_message = final_message

    async def __aenter__(self) -> _FakeStreamContext:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self) -> _FakeStreamContext:
        self._cursor = 0
        return self

    async def __anext__(self) -> _FakeStreamEvent:
        if self._cursor >= len(self._events):
            raise StopAsyncIteration
        ev = self._events[self._cursor]
        self._cursor += 1
        return ev

    async def get_final_message(self) -> _FakeMessage:
        return self._final_message


@dataclass
class _FakeMessages:
    create_response: _FakeMessage | None = None
    create_exc: Exception | None = None
    stream_events: list[_FakeStreamEvent] = field(default_factory=list)
    stream_final: _FakeMessage | None = None
    stream_exc: Exception | None = None

    last_create_call: dict[str, Any] = field(default_factory=dict)
    last_stream_call: dict[str, Any] = field(default_factory=dict)

    async def create(self, **kwargs: Any) -> _FakeMessage:
        self.last_create_call = kwargs
        if self.create_exc is not None:
            raise self.create_exc
        assert self.create_response is not None
        return self.create_response

    def stream(self, **kwargs: Any) -> _FakeStreamContext:
        self.last_stream_call = kwargs
        if self.stream_exc is not None:
            raise self.stream_exc
        assert self.stream_final is not None
        return _FakeStreamContext(self.stream_events, self.stream_final)


@dataclass
class _FakeClient:
    messages: _FakeMessages = field(default_factory=_FakeMessages)


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pricing_key_prepends_anthropic_prefix() -> None:
    """Native-Anthropic agents declare bare model ids in agent.yaml
    (``claude-sonnet-4-6``), but pricing.yaml uses LiteLLM-style
    keys (``anthropic/claude-sonnet-4-6``). The adapter bridges."""
    provider = AnthropicProvider(client=_FakeClient())  # type: ignore[arg-type]
    assert provider.pricing_key("claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"
    # Already prefixed — idempotent (operators who pass the LiteLLM
    # form still get a working lookup).
    assert provider.pricing_key("anthropic/claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"


@pytest.mark.unit
async def test_complete_happy_path_extracts_text_and_tokens() -> None:
    """Text from the first text content block + tokens from usage."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="hello world")],
        usage=_FakeUsage(input_tokens=42, output_tokens=7),
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    resp = await provider.complete(
        CompletionRequest(
            provider="claude-haiku-4-5",
            messages=[Message(role="user", content="hi")],
        )
    )
    assert resp.text == "hello world"
    assert resp.tokens.input == 42
    assert resp.tokens.output == 7


@pytest.mark.unit
async def test_complete_splits_system_message_to_kwarg() -> None:
    """Anthropic separates system from messages; the adapter should
    extract role=system entries and pass them via the ``system=`` kwarg."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="ok")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    await provider.complete(
        CompletionRequest(
            provider="claude-haiku-4-5",
            messages=[
                Message(role="system", content="you are concise"),
                Message(role="user", content="explain quicksort"),
            ],
        )
    )
    # System extracted; messages array contains only the user turn.
    assert fake.messages.last_create_call["system"] == "you are concise"
    assert fake.messages.last_create_call["messages"] == [
        {"role": "user", "content": "explain quicksort"}
    ]


@pytest.mark.unit
async def test_complete_defaults_max_tokens() -> None:
    """Anthropic requires ``max_tokens``. If the user didn't set it,
    the adapter picks a sane default — without this the SDK errors
    immediately."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="ok")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    await provider.complete(
        CompletionRequest(
            provider="claude-haiku-4-5",
            messages=[Message(role="user", content="hi")],
        )
    )
    assert fake.messages.last_create_call["max_tokens"] == 4096


@pytest.mark.unit
async def test_complete_passes_through_user_params() -> None:
    """User-set params (temperature, top_p, etc.) reach the SDK call."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="ok")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    await provider.complete(
        CompletionRequest(
            provider="claude-haiku-4-5",
            messages=[Message(role="user", content="hi")],
            params={"temperature": 0.7, "max_tokens": 100},
        )
    )
    assert fake.messages.last_create_call["temperature"] == 0.7
    # User-set max_tokens wins over the default.
    assert fake.messages.last_create_call["max_tokens"] == 100


# ---------------------------------------------------------------------------
# Exception translation
# ---------------------------------------------------------------------------


class _StubError(Exception):
    """Base class for our exception fakes — only the class NAME matters
    because the adapter does string-based dispatch (so it doesn't have
    to import anthropic at module scope)."""


class AuthenticationError(_StubError):
    pass


class RateLimitError(_StubError):
    def __init__(self, msg: str, retry_after: float | None = None) -> None:
        super().__init__(msg)
        if retry_after is not None:
            # Build a one-off response object that mirrors the
            # ``response.headers`` shape the anthropic SDK exposes —
            # instance-attr, not class-attr, to keep ruff happy.
            from types import SimpleNamespace  # noqa: PLC0415

            self.response = SimpleNamespace(headers={"retry-after": str(retry_after)})


class APITimeoutError(_StubError):
    pass


class BadRequestError(_StubError):
    pass


class APIConnectionError(_StubError):
    pass


@pytest.mark.unit
async def test_exception_translation_auth() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = AuthenticationError("bad key")
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(AuthError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_translation_rate_limit_extracts_retry_after() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = RateLimitError("slow down", retry_after=12.5)
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    # The adapter raises movate's RateLimitError (different class from
    # the SDK's), carrying the retry_after extracted from response headers.
    with pytest.raises(MovateRateLimitError) as exc_info:
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )
    assert exc_info.value.retry_after == 12.5


@pytest.mark.unit
async def test_exception_translation_timeout() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = APITimeoutError("timed out")
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(MovateTimeoutError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_translation_context_length() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = BadRequestError(
        "input is too long: exceeds context window of 200000 tokens"
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ContextLengthError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_translation_content_filter() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = BadRequestError("content policy violation: harmful content detected")
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ContentFilterError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_translation_generic_bad_request_is_schema_error() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = BadRequestError("invalid request body")
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_translation_connection_is_model_unavailable() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = APIConnectionError("can't reach api.anthropic.com")
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ModelUnavailableError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_stream_yields_text_chunks_and_final_usage() -> None:
    """Stream events project to chunks; final chunk carries usage."""
    fake = _FakeClient()
    fake.messages.stream_events = [
        _FakeStreamEvent(
            type="content_block_delta",
            delta=_FakeContentBlockDelta(text="hello "),
        ),
        _FakeStreamEvent(
            type="content_block_delta",
            delta=_FakeContentBlockDelta(text="world"),
        ),
        # Non-text event — should be filtered out.
        _FakeStreamEvent(type="message_stop"),
    ]
    fake.messages.stream_final = _FakeMessage(
        content=[_FakeTextBlock(text="hello world")],
        usage=_FakeUsage(input_tokens=5, output_tokens=2),
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    chunks = []
    async for chunk in provider.stream(
        CompletionRequest(
            provider="claude-haiku-4-5",
            messages=[Message(role="user", content="hi")],
        )
    ):
        chunks.append(chunk)

    # Two text deltas + one usage-only final chunk.
    assert len(chunks) == 3
    assert chunks[0].text == "hello "
    assert chunks[1].text == "world"
    assert chunks[2].text == ""
    assert chunks[2].tokens is not None
    assert chunks[2].tokens.input == 5
    assert chunks[2].tokens.output == 2


# ---------------------------------------------------------------------------
# Tool-use (PR 6): to_tool_spec + tool_use response parsing + message translation
# ---------------------------------------------------------------------------


@dataclass
class _FakeToolUseBlock:
    """Mimics the SDK's ``ToolUseBlock`` shape — type, id, name, input."""

    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _FakeMessageMixed:
    """Response that mixes a text block + a tool_use block, like the SDK
    surfaces when the model emits reasoning text before calling a tool."""

    content: list[Any]
    usage: _FakeUsage
    model: str = "claude-sonnet-4-6"
    stop_reason: str = "tool_use"


def _fake_skill_bundle(
    *,
    name: str = "calc",
    description: str = "Adds numbers",
    schema: dict[str, Any] | None = None,
) -> Any:
    """Build a minimal SkillBundle stand-in for to_tool_spec tests.

    The provider only reads ``spec.name`` / ``spec.description`` /
    ``input_schema`` — we don't need a real SkillBundle for this
    surface, just an object with those attributes.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    return SimpleNamespace(
        spec=SimpleNamespace(name=name, description=description),
        input_schema=schema
        or {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    )


@pytest.mark.unit
def test_to_tool_spec_emits_anthropic_flat_shape() -> None:
    """Anthropic wants ``{name, description, input_schema}`` directly —
    NOT OpenAI's nested ``{type: "function", function: {...}}``."""
    provider = AnthropicProvider(client=_FakeClient())  # type: ignore[arg-type]
    spec = provider.to_tool_spec(_fake_skill_bundle())
    assert set(spec.keys()) == {"name", "description", "input_schema"}
    assert spec["name"] == "calc"
    assert spec["description"] == "Adds numbers"
    # The skill's JSON schema becomes input_schema verbatim.
    assert spec["input_schema"]["properties"] == {
        "a": {"type": "number"},
        "b": {"type": "number"},
    }
    # No OpenAI-style wrapper keys should leak through.
    assert "type" not in spec
    assert "function" not in spec


@pytest.mark.unit
def test_to_tool_spec_falls_back_to_name_when_description_missing() -> None:
    """Skills without a description shouldn't emit an empty string —
    the model needs *something* to disambiguate the tool."""
    provider = AnthropicProvider(client=_FakeClient())  # type: ignore[arg-type]
    spec = provider.to_tool_spec(_fake_skill_bundle(description=""))
    assert spec["description"] == "calc"


@pytest.mark.unit
async def test_complete_passes_tools_through_to_sdk() -> None:
    """When ``request.tools`` is set, it's forwarded to ``messages.create``
    as the ``tools`` kwarg — unchanged (to_tool_spec already shaped them)."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="ok")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    tool_specs = [{"name": "calc", "description": "Adds", "input_schema": {"type": "object"}}]
    await provider.complete(
        CompletionRequest(
            provider="claude-sonnet-4-6",
            messages=[Message(role="user", content="2+2?")],
            tools=tool_specs,
        )
    )
    assert fake.messages.last_create_call.get("tools") == tool_specs


@pytest.mark.unit
async def test_complete_omits_tools_kwarg_when_none() -> None:
    """Single-shot agents (no skills) don't get a tools= kwarg.

    Anthropic accepts ``tools=None`` cleanly, but being explicit keeps
    the wire payload minimal and avoids any upstream quirks with empty
    tool arrays.
    """
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="ok")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    await provider.complete(
        CompletionRequest(
            provider="claude-sonnet-4-6",
            messages=[Message(role="user", content="hi")],
        )
    )
    assert "tools" not in fake.messages.last_create_call


@pytest.mark.unit
async def test_complete_surfaces_tool_use_response() -> None:
    """A tool_use content block → ``CompletionResponse(kind="tool_use", ...)``."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessageMixed(
        content=[
            _FakeToolUseBlock(id="toolu_01abc", name="calc", input={"a": 2, "b": 3}),
        ],
        usage=_FakeUsage(input_tokens=10, output_tokens=4),
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    resp = await provider.complete(
        CompletionRequest(
            provider="claude-sonnet-4-6",
            messages=[Message(role="user", content="2+3?")],
        )
    )
    assert resp.kind == "tool_use"
    assert resp.tool_name == "calc"
    assert resp.tool_id == "toolu_01abc"
    assert resp.tool_input == {"a": 2, "b": 3}
    assert resp.tokens.input == 10
    assert resp.tokens.output == 4


@pytest.mark.unit
async def test_complete_preserves_reasoning_text_before_tool_use() -> None:
    """When a model emits text + tool_use in the same response, the text
    is preserved in ``.text`` so the executor can log the reasoning."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessageMixed(
        content=[
            _FakeTextBlock(text="Let me calculate that."),
            _FakeToolUseBlock(id="toolu_x", name="calc", input={"a": 1, "b": 1}),
        ],
        usage=_FakeUsage(),
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    resp = await provider.complete(
        CompletionRequest(
            provider="claude-sonnet-4-6",
            messages=[Message(role="user", content="1+1?")],
        )
    )
    assert resp.kind == "tool_use"
    assert resp.text == "Let me calculate that."


@pytest.mark.unit
async def test_complete_takes_first_tool_use_when_multiple_emitted() -> None:
    """Parallel tool calls aren't supported in this cut — first wins.

    Matches the LiteLLM provider's PR 1 decision; a follow-up can wire
    multi-dispatch in the executor + a list-shaped tool_input here.
    """
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessageMixed(
        content=[
            _FakeToolUseBlock(id="toolu_1", name="calc", input={"a": 1, "b": 2}),
            _FakeToolUseBlock(id="toolu_2", name="other", input={"x": 99}),
        ],
        usage=_FakeUsage(),
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    resp = await provider.complete(
        CompletionRequest(
            provider="claude-sonnet-4-6",
            messages=[Message(role="user", content="?")],
        )
    )
    assert resp.tool_id == "toolu_1"
    assert resp.tool_name == "calc"


@pytest.mark.unit
async def test_complete_translates_openai_style_tool_history_to_content_blocks() -> None:
    """A continuing tool-use loop sends back an OpenAI-style history
    (assistant turn with tool_calls + tool result). The provider must
    fold these into Anthropic's content-block format before calling
    the SDK.
    """
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="2+3 is 5")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    # The executor's mid-loop history shape.
    history = [
        Message(role="user", content="what's 2+3?"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "toolu_01abc",
                    "type": "function",
                    "function": {"name": "calc", "arguments": '{"a": 2, "b": 3}'},
                }
            ],
        ),
        Message(role="tool", content='{"sum": 5}', tool_call_id="toolu_01abc"),
    ]
    await provider.complete(CompletionRequest(provider="claude-sonnet-4-6", messages=history))
    sent_messages = fake.messages.last_create_call["messages"]
    # Three turns from the executor → three messages on the wire:
    # the user's original question, the assistant's tool_use, and the
    # user-coalesced tool_result.
    assert len(sent_messages) == 3
    assert sent_messages[0] == {"role": "user", "content": "what's 2+3?"}
    # Assistant turn: content is a content-block list with one tool_use.
    assert sent_messages[1]["role"] == "assistant"
    assert sent_messages[1]["content"] == [
        {
            "type": "tool_use",
            "id": "toolu_01abc",
            "name": "calc",
            "input": {"a": 2, "b": 3},
        }
    ]
    # Tool result: role flipped to "user" with a tool_result content block.
    assert sent_messages[2]["role"] == "user"
    assert sent_messages[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_01abc",
            "content": '{"sum": 5}',
        }
    ]


@pytest.mark.unit
async def test_complete_coalesces_consecutive_tool_results() -> None:
    """Two back-to-back tool results coalesce into one user message
    with two tool_result blocks — Anthropic rejects consecutive user
    messages, so this is required for parallel-tool-call follow-ups."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="ok")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    history = [
        Message(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "id1",
                    "type": "function",
                    "function": {"name": "a", "arguments": "{}"},
                },
                {
                    "id": "id2",
                    "type": "function",
                    "function": {"name": "b", "arguments": "{}"},
                },
            ],
        ),
        Message(role="tool", content="result-a", tool_call_id="id1"),
        Message(role="tool", content="result-b", tool_call_id="id2"),
    ]
    await provider.complete(CompletionRequest(provider="claude-sonnet-4-6", messages=history))
    sent_messages = fake.messages.last_create_call["messages"]
    # Two messages: the assistant (with two tool_use blocks) and one
    # coalesced user message carrying both tool_result blocks.
    assert len(sent_messages) == 2
    assert sent_messages[1]["role"] == "user"
    assert len(sent_messages[1]["content"]) == 2
    assert sent_messages[1]["content"][0]["tool_use_id"] == "id1"
    assert sent_messages[1]["content"][1]["tool_use_id"] == "id2"


@pytest.mark.unit
async def test_complete_preserves_assistant_text_alongside_tool_call() -> None:
    """Some models emit a text prelude ("Let me check...") before a
    tool call. The provider must preserve the prelude as a text block
    so subsequent turns see the full conversation."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="done")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    history = [
        Message(
            role="assistant",
            content="Let me check the records.",
            tool_calls=[
                {
                    "id": "id1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        ),
        Message(role="tool", content="found", tool_call_id="id1"),
    ]
    await provider.complete(CompletionRequest(provider="claude-sonnet-4-6", messages=history))
    sent_messages = fake.messages.last_create_call["messages"]
    # Assistant message has text block + tool_use block, in order.
    assert sent_messages[0]["content"] == [
        {"type": "text", "text": "Let me check the records."},
        {
            "type": "tool_use",
            "id": "id1",
            "name": "lookup",
            "input": {},
        },
    ]


@pytest.mark.unit
async def test_complete_handles_malformed_tool_call_arguments() -> None:
    """OpenAI-style ``arguments`` is JSON-encoded; if it's malformed
    (upstream bug), the provider falls through with an empty input
    dict rather than crashing — the model can recover on the next turn."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="ok")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    history = [
        Message(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "id1",
                    "type": "function",
                    "function": {"name": "calc", "arguments": "{not valid json"},
                }
            ],
        ),
    ]
    await provider.complete(CompletionRequest(provider="claude-sonnet-4-6", messages=history))
    sent_messages = fake.messages.last_create_call["messages"]
    assert sent_messages[0]["content"][0]["input"] == {}


# ---------------------------------------------------------------------------
# Optional-dep gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_without_client_raises_clear_error_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the anthropic package isn't installed AND no client is
    injected, construction must raise ImportError with the
    ``movate-cli[anthropic]`` install hint."""
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "anthropic":
            raise ImportError("no module named anthropic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match=r"movate-cli\[anthropic\]"):
        AnthropicProvider()
