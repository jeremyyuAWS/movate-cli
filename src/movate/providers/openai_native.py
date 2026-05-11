"""Native OpenAI adapter — direct ``openai`` SDK calls.

The v0.6 alternative to :class:`LiteLLMProvider` for agents that
declare ``runtime: native_openai`` in ``agent.yaml``. The win:
features LiteLLM doesn't yet surface — strict-schema structured
outputs (``response_format``), Assistants API, parallel
function-calling, native vision-with-tools.

Module name is ``openai_native`` (not ``openai``) so we don't shadow
the SDK package — ``from movate.providers.openai_native import …``
stays unambiguous.

Scope of this first cut (mirrors the Anthropic adapter)
-------------------------------------------------------

* Text-in, text-out :meth:`complete` against
  ``chat.completions.create``.
* Token-aware :meth:`stream` (``stream=True`` + ``stream_options``).
* Exception translation matching :class:`LiteLLMProvider`'s taxonomy.

Tool-use, structured outputs (``response_format``), Assistants API,
and vision are intentionally deferred — they need schema /
Message-type changes that compound the diff. The architecture is
ready; the features land as follow-ups.

Optional install:

    uv add 'movate-cli[openai]'
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from movate.core.failures import (
    AuthError,
    ContentFilterError,
    ContextLengthError,
    ModelUnavailableError,
    MovateTimeoutError,
    RateLimitError,
    SchemaError,
)
from movate.core.models import TokenUsage
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)

if TYPE_CHECKING:
    import openai


class OpenAIProvider(BaseLLMProvider):
    """``BaseLLMProvider`` implementation that calls the official
    ``openai`` Python SDK directly.

    Provider strings on this runtime are bare OpenAI model ids —
    ``gpt-4o-mini-2024-07-18``, ``gpt-4.1``, ``o1-preview`` — NOT
    the ``openai/...`` LiteLLM-style prefix. Set the
    ``OPENAI_API_KEY`` env var the way the official SDK expects."""

    name = "native_openai"
    version = "0.0.1"

    def __init__(self, *, client: openai.AsyncOpenAI | None = None) -> None:
        """``client`` exists for tests — pass a mock that exposes the
        ``chat.completions.create`` shape. Production leaves it
        ``None`` and we construct from env vars."""
        if client is not None:
            self._client = client
        else:
            try:
                import openai as _openai  # noqa: PLC0415
            except ImportError as exc:
                raise ImportError(
                    "the 'openai' package is required for runtime: native_openai. "
                    "Install with: uv add 'movate-cli[openai]'"
                ) from exc
            self._client = _openai.AsyncOpenAI()

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        try:
            resp = await self._client.chat.completions.create(
                model=request.provider,
                # mypy: the SDK takes ``Iterable[ChatCompletionMessageParam]``
                # (a TypedDict); our runtime dicts satisfy the same keys.
                messages=[m.model_dump() for m in request.messages],  # type: ignore[misc]
                **request.params,
            )
        except Exception as exc:
            _translate_exception(exc)
            raise  # _translate_exception always raises

        return _to_completion_response(resp)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Stream via ``stream=True`` + ``stream_options={"include_usage": True}``.

        Without ``include_usage`` the SDK ends the stream without
        usage stats — cost accounting downstream would read zero.
        Force it on like LiteLLMProvider does."""
        params = dict(request.params)
        existing_opts = params.pop("stream_options", None) or {}
        params["stream_options"] = {**existing_opts, "include_usage": True}

        try:
            # The SDK's ``create`` is overload-typed: ``stream=False`` →
            # ``ChatCompletion``, ``stream=True`` → ``AsyncStream[...]``.
            # Passing ``stream=True`` via **kwargs defeats the overload
            # so mypy sees the union; cast to the async-iterable variant
            # since that's what runtime gives us.
            stream: Any = await self._client.chat.completions.create(
                model=request.provider,
                messages=[m.model_dump() for m in request.messages],  # type: ignore[misc]
                stream=True,
                **params,
            )
        except Exception as exc:
            _translate_exception(exc)
            raise

        async for chunk in stream:
            sc = _stream_chunk_from_openai(chunk)
            if sc is not None:
                yield sc

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError(
            "embedding support lands separately — for now use openai's SDK "
            "directly in your agent code if you need embeddings."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_completion_response(resp: Any) -> CompletionResponse:
    """Convert ``chat.completions.create`` response → :class:`CompletionResponse`.

    Text comes from ``choices[0].message.content``. Tool-call shapes
    are intentionally not extracted in this first cut."""
    text = ""
    choices = getattr(resp, "choices", None) or []
    if choices:
        msg = getattr(choices[0], "message", None)
        if msg is not None:
            text = getattr(msg, "content", "") or ""

    return CompletionResponse(
        text=text,
        tokens=_tokens_from_usage(getattr(resp, "usage", None)),
        raw={
            "openai_model": getattr(resp, "model", ""),
            "finish_reason": getattr(choices[0], "finish_reason", "") if choices else "",
        },
    )


def _stream_chunk_from_openai(chunk: Any) -> StreamChunk | None:
    """Project one SDK chunk to a :class:`StreamChunk`.

    Two shapes:

    * Mid-stream: ``choices[0].delta.content`` carries new text; no usage.
    * Final: ``choices`` may be empty and ``usage`` is populated
      (because we forced ``include_usage``).

    Returns ``None`` for empty chunks (no text + no usage) so the
    iterator doesn't yield noise."""
    text = ""
    choices = getattr(chunk, "choices", None) or []
    if choices:
        delta = getattr(choices[0], "delta", None)
        if delta is not None:
            text = getattr(delta, "content", "") or ""

    tokens: TokenUsage | None = None
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        tokens = _tokens_from_usage(usage)

    if not text and tokens is None:
        return None
    return StreamChunk(text=text, tokens=tokens)


def _tokens_from_usage(usage: Any) -> TokenUsage:
    """Convert SDK ``usage`` → :class:`TokenUsage`.

    OpenAI returns ``prompt_tokens`` + ``completion_tokens`` (+
    ``prompt_tokens_details.cached_tokens`` for prompt-caching-eligible
    models). Same shape we use for LiteLLMProvider."""
    if usage is None:
        return TokenUsage()
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    return TokenUsage(
        input=int(getattr(usage, "prompt_tokens", 0) or 0),
        output=int(getattr(usage, "completion_tokens", 0) or 0),
        cached_input=cached,
    )


def _translate_exception(exc: Exception) -> None:
    """Map ``openai.*`` exceptions to ``MovateError`` subclasses.

    Same approach as the Anthropic adapter: dispatch on class NAME
    so we don't have to import ``openai`` at module scope (the
    extra might not be installed). Always raises."""
    cls = type(exc).__name__
    msg = str(exc)
    if cls in {"AuthenticationError", "PermissionDeniedError"}:
        raise AuthError(msg) from exc
    if cls == "RateLimitError":
        retry_after = _extract_retry_after(exc)
        raise RateLimitError(msg, retry_after=retry_after) from exc
    if cls == "APITimeoutError":
        raise MovateTimeoutError(msg) from exc
    if cls == "BadRequestError":
        low = msg.lower()
        if "context" in low and ("length" in low or "window" in low):
            raise ContextLengthError(msg) from exc
        if "content" in low and ("policy" in low or "filter" in low):
            raise ContentFilterError(msg) from exc
        raise SchemaError(msg) from exc
    if cls in {"APIConnectionError", "InternalServerError", "NotFoundError"}:
        raise ModelUnavailableError(msg) from exc
    # Unknown openai error — surface as ModelUnavailable so the retry
    # policy treats it as retryable; log the class name for diagnosis.
    raise ModelUnavailableError(f"unmapped openai.{cls}: {msg}") from exc


def _extract_retry_after(exc: Exception) -> float | None:
    """OpenAI surfaces retry-after in the ``response.headers``."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
