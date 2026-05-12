"""Executor tool-call loop — provider returns tool_calls → executor
invokes registered tools → results fed back → loop until plain text.

Covers:

* Single-tool happy path (model requests one tool, gets one result,
  returns a final text answer).
* Multi-iteration loop (model chains 3+ tool calls).
* Async tool callables awaited correctly.
* Tool return values that aren't strings are JSON-encoded.
* Token usage accumulates across iterations (cost calc sees the sum).
* Max-iterations cap fires with an actionable error.
* Unknown tool name on the agent fails with a SchemaError pointing at
  the typo (run-time check; agent-yaml-side validation lands later).
* `tools=[]` (empty list, default) doesn't pass `tools=` to the
  provider — non-tool-using agents see the existing single-shot path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import RunRequest, TokenUsage
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    ToolCall,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent
from movate.tools import tool
from movate.tools.registry import _clear_registry_for_tests


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    """Each test starts with an empty registry so tools registered in
    one test don't leak into another."""
    _clear_registry_for_tests()
    yield
    _clear_registry_for_tests()


# ---------------------------------------------------------------------------
# Test provider — scripts the tool-call dance
# ---------------------------------------------------------------------------


class _ScriptedProvider(BaseLLMProvider):
    """Returns pre-canned responses keyed on the call index — useful for
    asserting exact tool-loop iteration behaviour without depending on
    a real LLM."""

    name = "scripted"
    version = "0.0.1"

    def __init__(self, responses: list[CompletionResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request)
        if not self._responses:
            raise RuntimeError("scripted provider exhausted — test ran more calls than expected")
        return self._responses.pop(0)

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold_agent_with_tools(
    tmp_path: Path,
    *,
    tools: list[str],
    output_required_field: str = "answer",
) -> Path:
    """Scaffold an agent.yaml with a `tools:` field. The scaffold's
    default output schema requires a single field; we adjust if the
    caller needs a different one."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    yaml = agent_dir / "agent.yaml"
    text = yaml.read_text()
    # Inject `tools:` right before `tags:`.
    tool_yaml = "tools:\n" + "\n".join(f"  - {t}" for t in tools) + "\n"
    text = text.replace("tags: []", tool_yaml + "tags: []")
    yaml.write_text(text)

    # Adjust output schema to a single string field the loop can satisfy.
    output_schema = agent_dir / "schema" / "output.json"
    output_schema.write_text(
        json.dumps(
            {
                "type": "object",
                "required": [output_required_field],
                "additionalProperties": False,
                "properties": {output_required_field: {"type": "string"}},
            }
        )
    )
    return agent_dir


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def tracer() -> NullTracer:
    return NullTracer()


def _build_executor(
    provider: BaseLLMProvider,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> Executor:
    return Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)


# ---------------------------------------------------------------------------
# Happy path — single tool, two provider calls
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_single_tool_call_loop_completes(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    """Model requests `lookup`; tool returns `42`; second model call
    returns the final JSON answer."""

    @tool
    def lookup(key: str) -> int:
        """Return a fixed answer for any key."""
        return 42

    agent_dir = _scaffold_agent_with_tools(tmp_path, tools=["lookup"])

    provider = _ScriptedProvider(
        responses=[
            # Iteration 1: model requests tool
            CompletionResponse(
                text="",
                tokens=TokenUsage(input=10, output=5),
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="lookup",
                        arguments={"key": "x"},
                        arguments_json='{"key": "x"}',
                    )
                ],
            ),
            # Iteration 2: model returns final answer
            CompletionResponse(
                text='{"answer": "the answer is 42"}',
                tokens=TokenUsage(input=20, output=15),
            ),
        ]
    )
    bundle = load_agent(agent_dir)
    executor = _build_executor(provider, pricing, storage, tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "go"}))

    assert response.status == "success"
    assert response.data == {"answer": "the answer is 42"}
    # Token usage accumulated: 10 + 5 + 20 + 15 = 50 across both calls
    assert response.metrics.tokens.input == 30
    assert response.metrics.tokens.output == 20
    # Provider was called twice (initial + post-tool-result)
    assert len(provider.calls) == 2
    # First call passed tools=; second call also did (model could chain)
    assert provider.calls[0].tools is not None
    assert provider.calls[1].tools is not None


@pytest.mark.unit
async def test_async_tool_callable_is_awaited(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    """Tools registered as `async def` should work — the loop awaits them
    rather than calling synchronously."""

    @tool
    async def fetch(url: str) -> str:
        """Async fetch."""
        return f"contents of {url}"

    agent_dir = _scaffold_agent_with_tools(tmp_path, tools=["fetch"])

    provider = _ScriptedProvider(
        responses=[
            CompletionResponse(
                text="",
                tokens=TokenUsage(input=5, output=3),
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="fetch",
                        arguments={"url": "http://example.com"},
                        arguments_json='{"url": "http://example.com"}',
                    )
                ],
            ),
            CompletionResponse(
                text='{"answer": "fetched it"}',
                tokens=TokenUsage(input=10, output=5),
            ),
        ]
    )
    bundle = load_agent(agent_dir)
    executor = _build_executor(provider, pricing, storage, tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "go"}))

    assert response.status == "success"
    # Tool was passed in the SECOND request as a `role=tool` message.
    tool_msgs = [m for m in provider.calls[1].messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "contents of http://example.com" in tool_msgs[0].content


@pytest.mark.unit
async def test_non_string_tool_result_is_json_encoded(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    """Tools returning dicts / lists / numbers get JSON-encoded for the
    model's `role=tool` message content."""

    @tool
    def aggregate(key: str) -> dict[str, Any]:
        """Return a structured result."""
        return {"count": 17, "items": ["a", "b", "c"]}

    agent_dir = _scaffold_agent_with_tools(tmp_path, tools=["aggregate"])
    provider = _ScriptedProvider(
        responses=[
            CompletionResponse(
                text="",
                tokens=TokenUsage(input=5, output=3),
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="aggregate",
                        arguments={"key": "x"},
                        arguments_json='{"key": "x"}',
                    )
                ],
            ),
            CompletionResponse(text='{"answer": "done"}', tokens=TokenUsage(input=10, output=5)),
        ]
    )
    bundle = load_agent(agent_dir)
    executor = _build_executor(provider, pricing, storage, tracer)
    await executor.execute(bundle, RunRequest(agent="demo", input={"text": "go"}))

    tool_msg = next(m for m in provider.calls[1].messages if m.role == "tool")
    parsed = json.loads(tool_msg.content)
    assert parsed == {"count": 17, "items": ["a", "b", "c"]}


# ---------------------------------------------------------------------------
# Multi-iteration loop
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_multi_iteration_loop_accumulates_tokens(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    """Three tool-call iterations + final text → token sums across all
    four provider calls (initial + 3 follow-ups)."""

    @tool
    def step(name: str) -> str:
        """One step in a multi-step plan."""
        return f"did {name}"

    agent_dir = _scaffold_agent_with_tools(tmp_path, tools=["step"])

    def _make_tc(idx: int) -> ToolCall:
        return ToolCall(
            id=f"c{idx}",
            name="step",
            arguments={"name": f"step-{idx}"},
            arguments_json=f'{{"name": "step-{idx}"}}',
        )

    provider = _ScriptedProvider(
        responses=[
            CompletionResponse(
                text="", tokens=TokenUsage(input=10, output=5), tool_calls=[_make_tc(1)]
            ),
            CompletionResponse(
                text="", tokens=TokenUsage(input=12, output=5), tool_calls=[_make_tc(2)]
            ),
            CompletionResponse(
                text="", tokens=TokenUsage(input=14, output=5), tool_calls=[_make_tc(3)]
            ),
            CompletionResponse(
                text='{"answer": "all done"}',
                tokens=TokenUsage(input=16, output=10),
            ),
        ]
    )
    bundle = load_agent(agent_dir)
    executor = _build_executor(provider, pricing, storage, tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "go"}))

    assert response.status == "success"
    assert len(provider.calls) == 4
    # input tokens summed: 10 + 12 + 14 + 16 = 52
    assert response.metrics.tokens.input == 52
    # output tokens summed: 5 + 5 + 5 + 10 = 25
    assert response.metrics.tokens.output == 25


# ---------------------------------------------------------------------------
# Max-iterations cap
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_max_iterations_cap_fires_with_actionable_error(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    """Model that keeps requesting tools forever hits the 10-iter cap."""

    @tool
    def loop_tool() -> str:
        """Loop forever."""
        return "again"

    agent_dir = _scaffold_agent_with_tools(tmp_path, tools=["loop_tool"])

    # 11 responses, all requesting more tool calls — should hit the cap.
    bad_responses = [
        CompletionResponse(
            text="",
            tokens=TokenUsage(input=5, output=2),
            tool_calls=[
                ToolCall(
                    id=f"c{i}",
                    name="loop_tool",
                    arguments={},
                    arguments_json="{}",
                )
            ],
        )
        for i in range(11)
    ]
    provider = _ScriptedProvider(responses=bad_responses)

    bundle = load_agent(agent_dir)
    executor = _build_executor(provider, pricing, storage, tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "go"}))

    # Run records an error rather than crashing — error contains an
    # actionable hint about iteration cap.
    assert response.status == "error"
    assert response.error is not None
    assert "iteration" in response.error.message.lower()


# ---------------------------------------------------------------------------
# Error paths — unknown tool name
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_agent_referencing_unknown_tool_errors(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    """agent.yaml says `tools: [does_not_exist]` — execute fails fast
    with a SchemaError pointing at the typo. Validation at agent-load
    time would be even earlier; this is the runtime fallback."""
    agent_dir = _scaffold_agent_with_tools(tmp_path, tools=["does_not_exist"])
    provider = _ScriptedProvider(
        responses=[CompletionResponse(text='{"answer": "x"}', tokens=TokenUsage())]
    )
    bundle = load_agent(agent_dir)
    executor = _build_executor(provider, pricing, storage, tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "go"}))

    assert response.status == "error"
    assert response.error is not None
    assert "does_not_exist" in response.error.message
    # Provider was never called — fail-fast before the first request.
    assert provider.calls == []


@pytest.mark.unit
async def test_model_requesting_unknown_tool_mid_loop_errors(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    """The model emits a tool_call for a tool that exists in the registry
    but ISN'T in the agent's allowed list. Currently we look it up
    globally (allowed list is just the prompt-time projection), so this
    works — but if the model HALLUCINATES a tool name, the registry
    lookup fails and SchemaError surfaces."""

    @tool
    def real_tool() -> str:
        """real."""
        return "ok"

    agent_dir = _scaffold_agent_with_tools(tmp_path, tools=["real_tool"])

    provider = _ScriptedProvider(
        responses=[
            CompletionResponse(
                text="",
                tokens=TokenUsage(input=5, output=2),
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="hallucinated_tool",
                        arguments={},
                        arguments_json="{}",
                    )
                ],
            ),
        ]
    )
    bundle = load_agent(agent_dir)
    executor = _build_executor(provider, pricing, storage, tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "go"}))

    assert response.status == "error"
    assert response.error is not None
    assert "hallucinated_tool" in response.error.message


# ---------------------------------------------------------------------------
# Back-compat — empty tools list = single-shot path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_agent_without_tools_doesnt_pass_tools_to_provider(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    """Default-tools-empty agents see the existing single-shot path.
    The provider's CompletionRequest.tools field is None."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")  # no tools
    provider = _ScriptedProvider(
        responses=[CompletionResponse(text='{"message": "ok"}', tokens=TokenUsage())]
    )
    bundle = load_agent(agent_dir)
    executor = _build_executor(provider, pricing, storage, tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "go"}))

    assert response.status == "success"
    assert len(provider.calls) == 1
    assert provider.calls[0].tools is None  # not passed
