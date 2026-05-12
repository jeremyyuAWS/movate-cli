"""``BaseLLMProvider`` Protocol — the only LLM seam in movate.

Adapters return raw text + token usage. They MUST NOT compute cost or call
external pricing APIs — pricing is derived in the executor from a versioned
local table (see :mod:`movate.providers.pricing`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from movate.core.models import TokenUsage


class ToolCall(BaseModel):
    """One tool invocation requested by the model.

    Fields mirror OpenAI's ``tool_calls[i]`` shape but with arguments
    pre-parsed from JSON into a dict so callers don't re-parse. The
    raw JSON string is preserved in :attr:`arguments_json` for
    debugging / replay.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    """Provider-issued call id. Echoed back in the ``tool`` message that
    carries the call's result so the model can match request → response
    across multiple parallel tool calls."""

    name: str
    """Tool name as registered via :func:`movate.tools.tool`."""

    arguments: dict[str, Any] = Field(default_factory=dict)
    """Parsed arguments. Empty dict for tools that take no args."""

    arguments_json: str = ""
    """Raw arguments JSON as the provider returned it. Kept for the
    Tier 2 #3 resume path — replay needs the byte-exact payload."""


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    # Tool-call message fields. Set only on assistant messages that
    # requested tool calls, and tool messages that carry results back.
    tool_calls: list[ToolCall] | None = None
    """Set on an assistant message that REQUESTED tool calls. The
    follow-up user-side messages (role=tool) carry the results."""

    tool_call_id: str | None = None
    """Set on a tool-role message — points at the ToolCall.id this
    message is the result of."""

    name: str | None = None
    """Set on tool-role messages — the tool name. Optional but lets
    older providers route correctly."""

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """Override default Pydantic dump to drop ``None`` fields.

        Tool-call fields (``tool_calls`` / ``tool_call_id`` / ``name``)
        are ``None`` on the vast majority of messages. Including them
        in the serialized form pollutes provider payloads and breaks
        adapters that strict-validate their request body — OpenAI's
        Python SDK rejects ``tool_call_id: None`` on a user message,
        and LiteLLM passes the dict through unchanged."""
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(**kwargs)


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    """LiteLLM-style model string, e.g. 'openai/gpt-4o-mini-2024-07-18'."""

    messages: list[Message]
    params: dict[str, Any] = Field(default_factory=dict)

    tools: list[dict[str, Any]] | None = None
    """OpenAI / LiteLLM tool-format list (``[{type: function, function:
    {name, description, parameters}}, ...]``). When set, the model can
    emit tool calls in its response. Built by
    :meth:`movate.tools.Tool.to_openai_tool` per registered tool.
    Default is None — non-tool-using agents pass through unchanged."""

    tool_choice: str | None = None
    """Controls tool selection. Standard values: 'auto' (default in
    LiteLLM when tools are present), 'required' (model MUST call a
    tool), or 'none' (disable tool calls for this request). Most
    callers leave this None and let the provider default apply."""


class CompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    """The model's text response. Empty string when the response is a
    pure tool-call request (the model is asking the executor to invoke
    tools first; text comes on a subsequent iteration after results
    are fed back)."""

    tokens: TokenUsage = Field(default_factory=TokenUsage)
    raw: dict[str, Any] = Field(default_factory=dict)

    tool_calls: list[ToolCall] | None = None
    """Tool calls the model wants the executor to invoke. None / empty
    means the response is a normal text completion. When present, the
    executor's tool-call loop invokes each, appends the results, and
    re-calls the provider until tool_calls is empty (or iteration cap)."""


class StreamChunk(BaseModel):
    """One slice of a streaming response.

    Most chunks carry a small ``text`` delta (a token or two) and an
    empty ``tokens``. The FINAL chunk in a stream carries the
    accumulated usage stats — token totals aren't knowable until the
    provider closes the stream.

    ``raw`` is the provider's native chunk payload for adapters that
    want to forward extra signal (e.g. Anthropic's content-block
    types). Generic streaming code ignores it; advanced adapters
    can peek."""

    model_config = ConfigDict(extra="forbid")

    text: str = ""
    tokens: TokenUsage | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class BaseLLMProvider(Protocol):
    """The only contract movate code uses to talk to a model.

    Implementations must map provider-specific exceptions to
    :class:`movate.core.failures.MovateError` subclasses so the retry
    layer can act on a single taxonomy.
    """

    name: str
    version: str

    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Stream a completion as :class:`StreamChunk` slices.

        Implementations MUST:

        * Yield at least one chunk (an empty-text chunk is fine if the
          provider returns nothing).
        * Yield the final usage stats on the LAST chunk's ``tokens``
          field. Callers rely on this for cost accounting.
        * Translate provider exceptions to ``MovateError`` subclasses
          the same way :meth:`complete` does — the executor's retry +
          fallback layer treats stream failures identically to one-shot
          failures."""
        ...

    async def embed(self, text: str, *, model: str) -> list[float]:
        """Embed is reserved for v0.5+ (retrieval); raise NotImplementedError until then."""
        ...

    def pricing_key(self, provider: str) -> str | None:
        """Map the agent's ``model.provider`` string to a key in the
        :mod:`movate.providers.pricing` table.

        Different runtimes use different naming for the same model:

        * LiteLLM agents use the prefixed form (``anthropic/claude-haiku-4-5``)
          which IS the pricing-table key — default impl returns it unchanged.
        * Native Anthropic / OpenAI agents use bare model ids
          (``claude-haiku-4-5``) — their adapters override this to prepend
          the family prefix so cost lookups succeed.
        * LangChain agents wrap an opaque Runnable — the underlying model
          isn't visible to movate, so the adapter returns ``None`` and the
          executor records ``cost_usd=0`` with a note.

        Default impl is the LiteLLM-style pass-through — adapters that need
        a translation override.
        """
        return provider
