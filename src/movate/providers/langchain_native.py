"""LangChain adapter — invoke a user-provided ``Runnable``.

This is fundamentally different from :class:`AnthropicProvider` /
:class:`OpenAIProvider`: LangChain isn't a model SDK, it's a
composition framework. The user writes a Python function that returns
a ``Runnable`` (an LLM, a chain, a graph, anything that satisfies the
LangChain LCEL interface); movate loads that function via an
entry-point spec in ``agent.yaml`` and calls
``.ainvoke()`` / ``.astream()``.

Field convention for ``runtime: langchain``
-------------------------------------------

The ``model.provider`` field on the agent.yaml is reused but its
meaning changes: it's a Python entry-point spec
(``package.module:function`` — same format setuptools uses). The
function is invoked with no arguments and must return a Runnable.

Example agent.yaml::

    runtime: langchain
    model:
      provider: myapp.chains:build_faq_chain

And ``myapp/chains.py``::

    from langchain_anthropic import ChatAnthropic
    from langchain_core.prompts import ChatPromptTemplate

    def build_faq_chain() -> Runnable:
        return (
            ChatPromptTemplate.from_template("Answer: {question}")
            | ChatAnthropic(model="claude-haiku-4-5")
        )

Trade-offs vs. native_anthropic / native_openai
-----------------------------------------------

* **You give up token + cost accounting precision.** The Runnable
  internally calls some model, but movate can't see it — token
  totals come back as zero. (A follow-up will add LangChain
  callbacks that capture usage; for now, treat costs on this
  runtime as ground truth = your LangSmith dashboard, not movate.)

* **You give up retry / fallback inside movate.** LangChain has its
  own retry semantics inside the Runnable; movate's retry layer
  sits around the whole chain.

* **You get LCEL composition + the LangChain ecosystem.** Document
  loaders, retrievers, memory, agents-as-tools, LangSmith tracing —
  all unchanged inside a movate-managed shell that provides the
  YAML contract, eval framework, persistence, and deploy.

Optional install::

    uv add 'movate-cli[langchain]'

(Installs ``langchain-core``. Specific model integrations
``langchain-openai`` / ``langchain-anthropic`` etc. are the user's
responsibility — movate doesn't pin them.)
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from typing import Any

from movate.core.failures import ModelUnavailableError, SchemaError
from movate.core.models import TokenUsage
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)


class LangChainProvider(BaseLLMProvider):
    """``BaseLLMProvider`` that loads a user-provided Runnable
    via an entry-point spec on each call.

    The Runnable is resolved per-request rather than cached because
    a long-running worker would otherwise miss code-reload edits
    during dev. Resolution cost is ``importlib`` + one function
    call — usually < 10ms — so this isn't a hot-path concern.
    For production, the worker process is restarted on each deploy
    anyway, so caching wouldn't help much."""

    name = "langchain"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        runnable = _load_runnable(request.provider)
        prompt = _messages_to_input(request.messages)
        try:
            result = await runnable.ainvoke(prompt)
        except Exception as exc:
            # We can't classify the underlying failure (LangChain may
            # wrap exceptions). Surface as ModelUnavailable so the
            # executor's retry policy treats it as retryable — that's
            # the safest default. Operators who want finer-grained
            # handling should add it inside their Runnable.
            raise ModelUnavailableError(f"runnable invocation failed: {exc}") from exc

        return CompletionResponse(
            text=_coerce_to_text(result),
            tokens=TokenUsage(),  # see docstring; LangChain doesn't surface usage uniformly
            raw={"langchain_entry_point": request.provider},
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Stream via ``runnable.astream()`` if the Runnable supports it.

        LangChain Runnables expose ``.astream()`` that yields chunks
        whose shape depends on what the Runnable produces — strings,
        BaseMessage chunks, dicts. We coerce each to a string the
        same way :meth:`complete` does for its final result."""
        runnable = _load_runnable(request.provider)
        prompt = _messages_to_input(request.messages)
        try:
            async for chunk in runnable.astream(prompt):
                text = _coerce_to_text(chunk)
                if text:
                    yield StreamChunk(text=text)
        except Exception as exc:
            raise ModelUnavailableError(f"runnable stream failed: {exc}") from exc
        # No final usage chunk — see docstring. Emit an empty chunk
        # with default-zero TokenUsage to keep the contract
        # (accumulator downstream sees the "stream ended" signal).
        yield StreamChunk(text="", tokens=TokenUsage())

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError(
            "embedding on runtime: langchain isn't supported — use a LangChain "
            "Embeddings primitive directly inside your Runnable."
        )


# ---------------------------------------------------------------------------
# Entry-point loading
# ---------------------------------------------------------------------------


def _load_runnable(entry_point: str) -> Any:
    """Resolve ``package.module:function`` → call the function → return
    the Runnable.

    Raises :class:`SchemaError` (NOT ImportError / AttributeError /
    TypeError) on any resolution failure — schema-level errors are
    non-retryable, which matches reality here: the agent.yaml is
    misconfigured, retrying won't fix it.
    """
    if ":" not in entry_point:
        raise SchemaError(
            f"runtime: langchain expects model.provider as 'package.module:function', "
            f"got {entry_point!r}"
        )
    module_path, _, func_name = entry_point.partition(":")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise SchemaError(
            f"couldn't import {module_path!r} for LangChain entry-point {entry_point!r}: {exc}"
        ) from exc
    factory = getattr(module, func_name, None)
    if factory is None:
        raise SchemaError(
            f"module {module_path!r} has no attribute {func_name!r} "
            f"(LangChain entry-point {entry_point!r})"
        )
    if not callable(factory):
        raise SchemaError(
            f"{entry_point!r} resolved to a non-callable {type(factory).__name__}; "
            f"LangChain entry-points must be functions that return a Runnable"
        )
    try:
        runnable = factory()
    except Exception as exc:
        raise SchemaError(f"LangChain entry-point {entry_point!r} raised: {exc}") from exc
    if not hasattr(runnable, "ainvoke"):
        raise SchemaError(
            f"{entry_point!r} returned a {type(runnable).__name__} which has no "
            f".ainvoke() — LangChain Runnables and LCEL chains do; chains compiled "
            f"with LangGraph also do."
        )
    return runnable


def _messages_to_input(messages: list[Any]) -> Any:
    """Convert movate ``Message`` list to something a LangChain
    Runnable can consume.

    Most Runnables accept either:

    * A bare string (the prompt) — for simple prompt-template chains.
    * A list of ``BaseMessage`` — for chains with proper role handling.

    We optimistically pass the joined string content of the user
    messages, falling back to a dict ``{"messages": [...]}`` shape
    when the Runnable expects structured input. The simplest path
    for v0.6 is to just join all user content into one prompt; users
    with complex shapes can use a PromptTemplate at the top of their
    chain to re-parse. Iterate in a follow-up if this is too lossy."""
    user_parts = [m.content for m in messages if m.role in {"user", "system"}]
    return "\n\n".join(user_parts)


def _coerce_to_text(result: Any) -> str:
    """Best-effort string extraction from a Runnable's output.

    Order:

    1. Plain ``str`` → use as-is.
    2. ``.content`` attribute (``BaseMessage`` / ``AIMessage``) →
       use that.
    3. ``dict`` → JSON-serialize so structured outputs survive a
       schema validation downstream.
    4. Anything else → ``str(result)`` (defensive — better than
       silently dropping the value)."""
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(result, dict):
        import json  # noqa: PLC0415

        return json.dumps(result)
    return str(result)
