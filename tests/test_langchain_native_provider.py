"""Tests for the LangChain adapter.

The adapter resolves a user-provided entry-point (``module:function``)
and calls the returned Runnable's ``.ainvoke()`` / ``.astream()``.
We don't import LangChain here — instead we register fake Runnables
in a test module and point ``CompletionRequest.provider`` at them.
That way the tests work whether or not the ``langchain`` extra is
installed.
"""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest

from movate.core.failures import ModelUnavailableError, SchemaError
from movate.providers.base import CompletionRequest, Message
from movate.providers.langchain_native import LangChainProvider

# ---------------------------------------------------------------------------
# Fake Runnables registered as a synthetic module
# ---------------------------------------------------------------------------


class _StringRunnable:
    """Runnable whose ainvoke returns a bare string."""

    def __init__(self, output: str = "hello from langchain") -> None:
        self._output = output

    async def ainvoke(self, _: Any) -> str:
        return self._output


class _MessageLike:
    """Mimics a LangChain BaseMessage — has a ``.content`` attr."""

    def __init__(self, content: str) -> None:
        self.content = content


class _MessageRunnable:
    async def ainvoke(self, _: Any) -> _MessageLike:
        return _MessageLike("hello from a message-shaped runnable")


class _DictRunnable:
    async def ainvoke(self, _: Any) -> dict[str, Any]:
        return {"answer": "yes", "confidence": 0.9}


class _StreamingRunnable:
    async def ainvoke(self, _: Any) -> str:
        return "concat"

    async def astream(self, _: Any) -> AsyncIterator[str]:
        for piece in ["hel", "lo ", "world"]:
            yield piece


class _RaisingRunnable:
    async def ainvoke(self, _: Any) -> str:
        raise RuntimeError("boom")


class _NotARunnable:
    """No .ainvoke. Should be rejected by the entry-point loader."""


def _string_factory() -> _StringRunnable:
    return _StringRunnable()


def _message_factory() -> _MessageRunnable:
    return _MessageRunnable()


def _dict_factory() -> _DictRunnable:
    return _DictRunnable()


def _streaming_factory() -> _StreamingRunnable:
    return _StreamingRunnable()


def _raising_factory() -> _RaisingRunnable:
    return _RaisingRunnable()


def _not_a_runnable_factory() -> _NotARunnable:
    return _NotARunnable()


def _factory_that_raises() -> _StringRunnable:
    raise ValueError("config invalid")


_NOT_A_CALLABLE = 42


@pytest.fixture(scope="module", autouse=True)
def _register_test_module() -> None:
    """Make the fake factories importable as ``movate_test_runnables:*``.

    Registering a synthetic module in ``sys.modules`` is the simplest
    way to give the LangChain adapter's importlib-based loader something
    real to find without polluting the package's tests/ directory with
    importable scratch modules."""
    mod = types.ModuleType("movate_test_runnables")
    mod.string_factory = _string_factory  # type: ignore[attr-defined]
    mod.message_factory = _message_factory  # type: ignore[attr-defined]
    mod.dict_factory = _dict_factory  # type: ignore[attr-defined]
    mod.streaming_factory = _streaming_factory  # type: ignore[attr-defined]
    mod.raising_factory = _raising_factory  # type: ignore[attr-defined]
    mod.not_a_runnable_factory = _not_a_runnable_factory  # type: ignore[attr-defined]
    mod.factory_that_raises = _factory_that_raises  # type: ignore[attr-defined]
    mod.not_a_callable = _NOT_A_CALLABLE  # type: ignore[attr-defined]
    sys.modules["movate_test_runnables"] = mod


# ---------------------------------------------------------------------------
# complete() — output coercion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pricing_key_returns_none_because_model_is_opaque() -> None:
    """LangChain Runnables wrap an arbitrary chain — movate can't see
    which model the Runnable will invoke, so pricing isn't applicable.
    The adapter returns None and the executor records cost=0."""
    provider = LangChainProvider()
    assert provider.pricing_key("myapp.chains:build_chain") is None
    assert provider.pricing_key("anything_else") is None


@pytest.mark.unit
async def test_complete_string_runnable() -> None:
    provider = LangChainProvider()
    resp = await provider.complete(
        CompletionRequest(
            provider="movate_test_runnables:string_factory",
            messages=[Message(role="user", content="hi")],
        )
    )
    assert resp.text == "hello from langchain"


@pytest.mark.unit
async def test_complete_message_runnable_extracts_content() -> None:
    """If the Runnable returns a BaseMessage-like object, the adapter
    pulls ``.content`` rather than ``str()``-ing the whole object."""
    provider = LangChainProvider()
    resp = await provider.complete(
        CompletionRequest(
            provider="movate_test_runnables:message_factory",
            messages=[Message(role="user", content="hi")],
        )
    )
    assert resp.text == "hello from a message-shaped runnable"


@pytest.mark.unit
async def test_complete_dict_runnable_json_serialises() -> None:
    """Dict outputs are JSON-serialised so they survive movate's
    output-schema validation downstream (which expects valid JSON)."""
    import json  # noqa: PLC0415

    provider = LangChainProvider()
    resp = await provider.complete(
        CompletionRequest(
            provider="movate_test_runnables:dict_factory",
            messages=[Message(role="user", content="hi")],
        )
    )
    payload = json.loads(resp.text)
    assert payload == {"answer": "yes", "confidence": 0.9}


# ---------------------------------------------------------------------------
# Entry-point loading — error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_entry_point_without_colon_rejected() -> None:
    """``package.module:function`` is the only accepted shape. Without
    the colon, ``module.attr`` is ambiguous — reject loudly."""
    provider = LangChainProvider()
    with pytest.raises(SchemaError, match=r"package\.module:function"):
        await provider.complete(
            CompletionRequest(
                provider="just_a_module_name",
                messages=[Message(role="user", content="hi")],
            )
        )


@pytest.mark.unit
async def test_entry_point_missing_module_rejected() -> None:
    provider = LangChainProvider()
    with pytest.raises(SchemaError, match="couldn't import"):
        await provider.complete(
            CompletionRequest(
                provider="does_not_exist:some_func",
                messages=[Message(role="user", content="hi")],
            )
        )


@pytest.mark.unit
async def test_entry_point_missing_function_rejected() -> None:
    provider = LangChainProvider()
    with pytest.raises(SchemaError, match="has no attribute"):
        await provider.complete(
            CompletionRequest(
                provider="movate_test_runnables:nope",
                messages=[Message(role="user", content="hi")],
            )
        )


@pytest.mark.unit
async def test_entry_point_non_callable_rejected() -> None:
    """Pointing at a module attribute that isn't a function (e.g. a
    constant) should fail with a clear message."""
    provider = LangChainProvider()
    with pytest.raises(SchemaError, match="non-callable"):
        await provider.complete(
            CompletionRequest(
                provider="movate_test_runnables:not_a_callable",
                messages=[Message(role="user", content="hi")],
            )
        )


@pytest.mark.unit
async def test_entry_point_returns_non_runnable_rejected() -> None:
    """If the factory returns something without ``.ainvoke``, reject
    with a hint about LangChain Runnables (and LangGraph chains)."""
    provider = LangChainProvider()
    with pytest.raises(SchemaError, match=r"\.ainvoke"):
        await provider.complete(
            CompletionRequest(
                provider="movate_test_runnables:not_a_runnable_factory",
                messages=[Message(role="user", content="hi")],
            )
        )


@pytest.mark.unit
async def test_factory_raises_surfaces_as_schema_error() -> None:
    """An exception inside the factory function is a config issue —
    schema-class, not retryable."""
    provider = LangChainProvider()
    with pytest.raises(SchemaError, match="config invalid"):
        await provider.complete(
            CompletionRequest(
                provider="movate_test_runnables:factory_that_raises",
                messages=[Message(role="user", content="hi")],
            )
        )


@pytest.mark.unit
async def test_runnable_ainvoke_raises_surfaces_as_model_unavailable() -> None:
    """The factory loaded fine, but the runnable failed at runtime.
    That's a transient failure — surface as ModelUnavailable so the
    executor's retry policy treats it as retryable."""
    provider = LangChainProvider()
    with pytest.raises(ModelUnavailableError, match="runnable invocation failed"):
        await provider.complete(
            CompletionRequest(
                provider="movate_test_runnables:raising_factory",
                messages=[Message(role="user", content="hi")],
            )
        )


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_stream_yields_chunks_then_final_empty() -> None:
    """``astream()`` chunks reach the caller; a final empty chunk
    closes the stream (mirrors the contract used by LiteLLM /
    Anthropic / OpenAI adapters)."""
    provider = LangChainProvider()
    chunks = []
    async for chunk in provider.stream(
        CompletionRequest(
            provider="movate_test_runnables:streaming_factory",
            messages=[Message(role="user", content="hi")],
        )
    ):
        chunks.append(chunk)
    # 3 text chunks ("hel", "lo ", "world") + 1 empty final.
    text_chunks = [c for c in chunks if c.text]
    assert [c.text for c in text_chunks] == ["hel", "lo ", "world"]
    # Final chunk has empty text + a zero-token TokenUsage.
    assert chunks[-1].text == ""
    assert chunks[-1].tokens is not None
