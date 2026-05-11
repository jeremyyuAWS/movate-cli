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


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    """LiteLLM-style model string, e.g. 'openai/gpt-4o-mini-2024-07-18'."""

    messages: list[Message]
    params: dict[str, Any] = Field(default_factory=dict)


class CompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    raw: dict[str, Any] = Field(default_factory=dict)


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
