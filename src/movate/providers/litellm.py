"""LiteLLM-backed implementation of :class:`BaseLLMProvider`.

This is the only place in movate that imports LiteLLM. Two important choices:

1. ``num_retries=0`` — movate's :func:`movate.core.retry.run_with_retries`
   owns the retry policy. Letting LiteLLM also retry would compound delays
   and obscure the typed failure taxonomy.

2. Exceptions are translated to :class:`movate.core.failures.MovateError`
   subclasses so the executor can act on a single taxonomy. LiteLLM's
   ``OPENAI_PROXY_*`` style errors are translated by string-sniffing where
   the structured exception class doesn't disambiguate.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, cast

import litellm
from litellm import exceptions as lle

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
    ToolCall,
)

log = logging.getLogger(__name__)

# LiteLLM emits a noisy startup log line by default; quiet it.
litellm.suppress_debug_info = True


class LiteLLMProvider(BaseLLMProvider):
    name = "litellm"
    version = "0.0.1"

    async def complete(  # noqa: PLR0912 — exception translation table
        self, request: CompletionRequest
    ) -> CompletionResponse:
        # Build kwargs incrementally so tools / tool_choice are passed only
        # when set. LiteLLM emits warnings on `tools=None` for some providers.
        kwargs: dict[str, Any] = {
            "model": request.provider,
            "messages": [_serialize_message(m) for m in request.messages],
            "num_retries": 0,  # movate owns retries
            **request.params,
        }
        if request.tools is not None:
            kwargs["tools"] = request.tools
        if request.tool_choice is not None:
            kwargs["tool_choice"] = request.tool_choice
        try:
            resp = await litellm.acompletion(**kwargs)
        except lle.AuthenticationError as exc:
            raise AuthError(str(exc)) from exc
        except lle.RateLimitError as exc:
            retry_after = _extract_retry_after(exc)
            raise RateLimitError(str(exc), retry_after=retry_after) from exc
        except lle.Timeout as exc:
            raise MovateTimeoutError(str(exc)) from exc
        except lle.ContextWindowExceededError as exc:
            raise ContextLengthError(str(exc)) from exc
        except lle.ContentPolicyViolationError as exc:
            raise ContentFilterError(str(exc)) from exc
        except lle.BadRequestError as exc:
            msg = str(exc).lower()
            if "context" in msg and "length" in msg:
                raise ContextLengthError(str(exc)) from exc
            if "content" in msg and ("policy" in msg or "filter" in msg):
                raise ContentFilterError(str(exc)) from exc
            raise SchemaError(str(exc)) from exc
        except lle.APIConnectionError as exc:
            raise ModelUnavailableError(str(exc)) from exc
        except lle.ServiceUnavailableError as exc:
            raise ModelUnavailableError(str(exc)) from exc
        except lle.InternalServerError as exc:
            raise ModelUnavailableError(str(exc)) from exc

        return _to_completion_response(resp)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Stream a completion via LiteLLM's ``stream=True``.

        We force ``stream_options={"include_usage": True}`` so the
        final chunk carries token totals — without it, cost accounting
        downstream would have to guess. Exception translation matches
        :meth:`complete`: the executor's retry / fallback layer can
        treat one-shot and streaming failures interchangeably."""
        # Merge user params with the streaming-specific options. User
        # params win on conflict, but ``stream`` and ``stream_options``
        # are forced so cost accounting always works.
        params = dict(request.params)
        existing_opts = params.pop("stream_options", None) or {}
        params["stream_options"] = {**existing_opts, "include_usage": True}

        try:
            resp = await litellm.acompletion(
                model=request.provider,
                messages=[m.model_dump() for m in request.messages],
                stream=True,
                num_retries=0,  # movate owns retries
                **params,
            )
        except lle.AuthenticationError as exc:
            raise AuthError(str(exc)) from exc
        except lle.RateLimitError as exc:
            retry_after = _extract_retry_after(exc)
            raise RateLimitError(str(exc), retry_after=retry_after) from exc
        except lle.Timeout as exc:
            raise MovateTimeoutError(str(exc)) from exc
        except lle.ContextWindowExceededError as exc:
            raise ContextLengthError(str(exc)) from exc
        except lle.ContentPolicyViolationError as exc:
            raise ContentFilterError(str(exc)) from exc
        except lle.BadRequestError as exc:
            msg = str(exc).lower()
            if "context" in msg and "length" in msg:
                raise ContextLengthError(str(exc)) from exc
            if "content" in msg and ("policy" in msg or "filter" in msg):
                raise ContentFilterError(str(exc)) from exc
            raise SchemaError(str(exc)) from exc
        except lle.APIConnectionError as exc:
            raise ModelUnavailableError(str(exc)) from exc
        except lle.ServiceUnavailableError as exc:
            raise ModelUnavailableError(str(exc)) from exc
        except lle.InternalServerError as exc:
            raise ModelUnavailableError(str(exc)) from exc

        async for chunk in resp:
            yield _stream_chunk_from_litellm(chunk)

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover - v0.5
        raise NotImplementedError("embed lands in v0.5 with retrieval")


def _extract_retry_after(exc: Exception) -> float | None:
    """LiteLLM stores retry-after on different attrs across versions."""
    for attr in ("retry_after", "_retry_after"):
        v = getattr(exc, attr, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _serialize_message(m: Any) -> dict[str, Any]:
    """Convert our :class:`Message` to LiteLLM's expected dict shape.

    Drops keys that LiteLLM doesn't recognise (most providers reject
    unknown fields). The OpenAI shape uses ``role`` / ``content`` /
    ``tool_calls`` / ``tool_call_id`` / ``name`` — we mirror that
    subset and omit any null fields so the dict matches what LiteLLM
    expects byte-for-byte.
    """
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        # Assistant message that requested tools — re-serialize each
        # ToolCall back to the OpenAI shape (the model returned this
        # to us in the same shape; we just round-trip it).
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": tc.arguments_json or json.dumps(tc.arguments),
                },
            }
            for tc in m.tool_calls
        ]
        # Assistant tool-call messages typically have empty content;
        # some providers reject non-empty + tool_calls on the same
        # message. Empty string is fine.
    if m.tool_call_id is not None:
        out["tool_call_id"] = m.tool_call_id
    if m.name is not None:
        out["name"] = m.name
    return out


def _parse_tool_calls(msg: Any) -> list[ToolCall] | None:
    """Pull ``tool_calls`` off LiteLLM's message object and convert to
    our :class:`ToolCall` shape. Returns None for messages without
    tool calls (the common case)."""
    raw_calls = getattr(msg, "tool_calls", None) or []
    if not raw_calls:
        return None
    out: list[ToolCall] = []
    for call in raw_calls:
        fn = getattr(call, "function", None)
        name = getattr(fn, "name", "") if fn is not None else ""
        arg_json = getattr(fn, "arguments", "") if fn is not None else ""
        try:
            args = json.loads(arg_json) if arg_json else {}
        except json.JSONDecodeError:
            # Model emitted invalid JSON in arguments. Pass through
            # the raw string and an empty dict; the executor will
            # surface a schema error when it tries to invoke.
            args = {}
        out.append(
            ToolCall(
                id=getattr(call, "id", "") or "",
                name=name,
                arguments=args,
                arguments_json=arg_json or "",
            )
        )
    return out


def _to_completion_response(resp: Any) -> CompletionResponse:
    """Convert a LiteLLM ModelResponse to our CompletionResponse.

    Token usage is pulled from ``resp.usage``; LiteLLM's reported cost is
    placed in ``raw['litellm_cost_usd']`` for drift checks against the
    canonical pricing table — never used by the executor for billing.
    """
    choices = getattr(resp, "choices", None) or []
    text = ""
    tool_calls: list[ToolCall] | None = None
    if choices:
        msg = getattr(choices[0], "message", None)
        if msg is not None:
            text = getattr(msg, "content", "") or ""
            tool_calls = _parse_tool_calls(msg)

    usage = getattr(resp, "usage", None)
    tokens = TokenUsage(
        input=int(getattr(usage, "prompt_tokens", 0) or 0),
        output=int(getattr(usage, "completion_tokens", 0) or 0),
        cached_input=int(_cached_input_tokens(usage)),
    )

    raw: dict[str, Any] = {
        "litellm_model": getattr(resp, "model", ""),
    }
    hidden = cast(dict[str, Any] | None, getattr(resp, "_hidden_params", None))
    if hidden:
        cost = hidden.get("response_cost")
        if cost is not None:
            raw["litellm_cost_usd"] = float(cost)

    return CompletionResponse(text=text, tokens=tokens, raw=raw, tool_calls=tool_calls)


def _stream_chunk_from_litellm(chunk: Any) -> StreamChunk:
    """Convert one LiteLLM stream slice to our :class:`StreamChunk`.

    Two shapes to handle:

    * Mid-stream content delta: ``chunk.choices[0].delta.content`` has
      the new token(s); ``chunk.usage`` is ``None``.
    * Final chunk with usage stats (because we passed
      ``stream_options={"include_usage": True}``): the ``choices``
      may be empty and ``chunk.usage`` carries totals.

    LiteLLM normalises across providers, so we don't peek at the raw
    provider format here."""
    text = ""
    choices = getattr(chunk, "choices", None) or []
    if choices:
        delta = getattr(choices[0], "delta", None)
        if delta is not None:
            text = getattr(delta, "content", "") or ""

    tokens: TokenUsage | None = None
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        tokens = TokenUsage(
            input=int(getattr(usage, "prompt_tokens", 0) or 0),
            output=int(getattr(usage, "completion_tokens", 0) or 0),
            cached_input=int(_cached_input_tokens(usage)),
        )

    return StreamChunk(text=text, tokens=tokens)


def _cached_input_tokens(usage: Any) -> int:
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    return int(getattr(details, "cached_tokens", 0) or 0)
