"""End-to-end: Executor drives the tool-use loop against AnthropicProvider.

Unit-level coverage for the provider's translation lives in
``test_anthropic_provider.py``. This file proves the *contract* between
the executor and the native_anthropic adapter holds across a multi-turn
loop with a real skill dispatch.

The agent uses ``runtime: native_anthropic``; the provider's underlying
SDK call is mocked through a scripted ``_FakeClient`` that returns:

  1. A ``tool_use`` content block on the first call (model decides to
     invoke ``add-one``).
  2. A final text content block on the second call (model's answer
     after seeing the tool result).

If either turn's wire payload is wrong (missing tool_use_id correlation,
wrong content-block shape, lost system kwarg), the second call's
``last_create_call`` lets us assert against it directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import RunRequest
from movate.core.skill_backend import SkillExecutionContext
from movate.providers.anthropic import AnthropicProvider
from movate.providers.pricing import load_pricing
from movate.providers.registry import ProviderRegistry
from movate.testing import InMemoryStorage, NullTracer


def _add_one(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Skill used by the integration test — adds 1 to ``x``."""
    return {"y": int(input["x"]) + 1}


# ---------------------------------------------------------------------------
# Scriptable FakeClient — mirrors the anthropic SDK surface the adapter touches.
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
class _FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _FakeMessage:
    content: list[Any]
    usage: _FakeUsage
    model: str = "claude-sonnet-4-6"
    stop_reason: str = "end_turn"


@dataclass
class _ScriptedMessages:
    """Scripted ``messages.create`` — emits responses in order.

    Captures every call so the test can assert against the full
    request payload (e.g. that the second call's history carried the
    tool_result content block correctly).
    """

    responses: list[_FakeMessage] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("scripted client ran out of responses")
        return self.responses.pop(0)

    def stream(self, **kwargs: Any) -> Any:  # pragma: no cover — unused
        raise NotImplementedError


@dataclass
class _FakeClient:
    messages: _ScriptedMessages = field(default_factory=_ScriptedMessages)


# ---------------------------------------------------------------------------
# Project layout helpers (mirror test_skills.py)
# ---------------------------------------------------------------------------


def _write_native_anthropic_agent(project_root: Path) -> Path:
    """Build a minimal project: one skill + one agent with
    ``runtime: native_anthropic`` that references the skill."""
    skill_dir = project_root / "skills" / "add-one"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: add-one\n"
        "version: 0.1.0\n"
        "description: adds one to x\n"
        "schema:\n"
        "  input:\n"
        "    x: integer\n"
        "  output:\n"
        "    y: integer\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: tests.test_anthropic_executor_tool_use:_add_one\n"
        "cost:\n"
        "  per_call_usd: 0.0\n"
    )
    agent_dir = project_root / "calc-agent"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: calc-agent\n"
        "version: 0.1.0\n"
        "runtime: native_anthropic\n"
        "model:\n"
        "  provider: claude-sonnet-4-6\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input:\n"
        "    question: string\n"
        "  output:\n"
        "    answer: string\n"
        "skills:\n"
        "  - add-one\n"
    )
    (agent_dir / "prompt.md").write_text("{{ input.question }}")
    return agent_dir


@pytest.mark.asyncio
async def test_executor_drives_anthropic_native_tool_use_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two-turn tool-use loop against the native_anthropic adapter.

    Turn 1: model emits a tool_use block calling ``add-one(x=41)``.
    Executor dispatches the skill, gets ``{"y": 42}``, appends the
    OpenAI-style history.

    Turn 2: provider must translate the history into Anthropic
    content blocks before re-prompting. Model returns the final
    JSON answer. We assert:

    * The skill ran (executor dispatched it correctly).
    * The final response is schema-validated and successful.
    * Turn 2's wire payload contained the tool_use block on the
      assistant turn AND the tool_result block on the user turn,
      with matching ``tool_use_id`` correlation.
    """
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_native_anthropic_agent(tmp_path)

    bundle = load_agent(agent_dir)
    assert len(bundle.skills) == 1
    assert bundle.skills[0].spec.name == "add-one"

    # Script the SDK responses: turn 1 = tool_use, turn 2 = final answer.
    fake = _FakeClient()
    fake.messages.responses = [
        _FakeMessage(
            content=[_FakeToolUseBlock(id="toolu_42", name="add-one", input={"x": 41})],
            usage=_FakeUsage(input_tokens=10, output_tokens=5),
            stop_reason="tool_use",
        ),
        _FakeMessage(
            content=[_FakeTextBlock(text='{"answer": "42"}')],
            usage=_FakeUsage(input_tokens=20, output_tokens=8),
            stop_reason="end_turn",
        ),
    ]
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    # Build an Executor wired with the native_anthropic provider as
    # both the default LiteLLM slot (irrelevant here) and the
    # NATIVE_ANTHROPIC registry entry. The executor picks by the
    # agent's runtime field.
    from movate.core.models import AgentRuntime  # noqa: PLC0415

    storage = InMemoryStorage()
    await storage.init()
    tracer = NullTracer()
    registry = ProviderRegistry(default_litellm=provider)
    registry.register(AgentRuntime.NATIVE_ANTHROPIC, provider)
    executor = Executor(
        registry=registry,
        pricing=load_pricing(),
        storage=storage,
        tracer=tracer,
        tenant_id="test",
    )

    response = await executor.execute(
        bundle,
        RunRequest(agent="calc-agent", input={"question": "what is 41+1?"}),
    )
    assert response.status == "success", response.error
    assert response.data == {"answer": "42"}

    # Two SDK calls were made — one per turn.
    assert len(fake.messages.calls) == 2

    # Turn 1 carried the tool spec from to_tool_spec (flat Anthropic shape).
    turn1_tools = fake.messages.calls[0].get("tools") or []
    assert turn1_tools, "expected tool specs on the first turn"
    assert turn1_tools[0]["name"] == "add-one"
    assert "input_schema" in turn1_tools[0]
    # No nested OpenAI-style "function" wrapper.
    assert "function" not in turn1_tools[0]

    # Turn 2 carried the full history: original user msg, assistant
    # with tool_use block, user with tool_result block.
    turn2_messages = fake.messages.calls[1]["messages"]
    # Find the assistant and tool_result messages.
    assistant_with_tool_use = next(
        m
        for m in turn2_messages
        if m["role"] == "assistant"
        and isinstance(m["content"], list)
        and any(b.get("type") == "tool_use" for b in m["content"])
    )
    tool_use_block = next(
        b for b in assistant_with_tool_use["content"] if b.get("type") == "tool_use"
    )
    assert tool_use_block["id"] == "toolu_42"
    assert tool_use_block["name"] == "add-one"
    assert tool_use_block["input"] == {"x": 41}

    user_with_tool_result = next(
        m
        for m in turn2_messages
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    )
    tool_result_block = next(
        b for b in user_with_tool_result["content"] if b.get("type") == "tool_result"
    )
    # The id correlation is what makes Anthropic's loop work — the
    # provider must echo the tool_use's id into tool_use_id on the
    # result.
    assert tool_result_block["tool_use_id"] == "toolu_42"
    # The tool result is the JSON-serialised skill output the
    # executor produced.
    assert '"y": 42' in tool_result_block["content"]
