"""End-to-end: Executor drives the tool-use loop against OpenAIProvider.

Unit-level coverage for the provider's response parsing lives in
``test_openai_native_provider.py``. This file proves the *contract*
between the executor and the native_openai adapter holds across a
multi-turn loop with a real skill dispatch.

Unlike the native_anthropic counterpart (which needs message
translation), the OpenAI SDK accepts the same flat-message format
the executor builds in. So this integration test really only verifies:

* ``tools=`` gets passed on turn 1
* The tool_calls response is parsed correctly
* Turn 2's payload carries the OpenAI-style assistant turn + tool
  result through unchanged (the default ``model_dump`` path works
  for the tool fields)
* The skill is dispatched, the final JSON answer validates, and the
  cost includes the skill's per_call_usd
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
from movate.providers.openai_native import OpenAIProvider
from movate.providers.pricing import load_pricing
from movate.providers.registry import ProviderRegistry
from movate.testing import InMemoryStorage, NullTracer


def _add_one(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Skill used by the integration test — adds 1 to ``x``."""
    return {"y": int(input["x"]) + 1}


# ---------------------------------------------------------------------------
# Scriptable FakeClient — mirrors the openai SDK shape the adapter touches.
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
class _FakeFunctionCall:
    name: str
    arguments: str  # JSON-encoded


@dataclass
class _FakeToolCall:
    id: str
    function: _FakeFunctionCall
    type: str = "function"


@dataclass
class _FakeMessage:
    content: str | None = None
    tool_calls: list[_FakeToolCall] = field(default_factory=list)


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
class _ScriptedCompletions:
    """Scripted ``chat.completions.create`` — emits responses in order
    and captures every call so the test can inspect wire payloads."""

    responses: list[_FakeChatCompletion] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> _FakeChatCompletion:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("scripted client ran out of responses")
        return self.responses.pop(0)


@dataclass
class _FakeChat:
    completions: _ScriptedCompletions = field(default_factory=_ScriptedCompletions)


@dataclass
class _FakeClient:
    chat: _FakeChat = field(default_factory=_FakeChat)


# ---------------------------------------------------------------------------
# Project layout helper (mirrors test_anthropic_executor_tool_use)
# ---------------------------------------------------------------------------


def _write_native_openai_agent(project_root: Path) -> Path:
    """Build a minimal project: one Python skill + one agent with
    ``runtime: native_openai`` that references the skill."""
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
        "  entry: tests.test_openai_native_executor_tool_use:_add_one\n"
        "cost:\n"
        "  per_call_usd: 0.0005\n"
    )
    agent_dir = project_root / "calc-agent"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: calc-agent\n"
        "version: 0.1.0\n"
        "runtime: native_openai\n"
        "model:\n"
        "  provider: gpt-4o-mini-2024-07-18\n"
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
async def test_executor_drives_openai_native_tool_use_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two-turn tool-use loop against the native_openai adapter.

    Turn 1: model emits a ``tool_calls`` entry calling ``add-one(x=41)``.
    Executor dispatches the skill, gets ``{"y": 42}``, appends the
    OpenAI-style assistant turn + tool result to history.

    Turn 2: provider forwards history unchanged (no Anthropic-style
    translation needed). Model returns the final JSON answer. We assert:

    * The skill ran (executor dispatched it).
    * The final response is schema-validated and successful.
    * Turn 1's wire payload carried the OpenAI-shaped tool spec.
    * Turn 2's wire payload preserved the assistant ``tool_calls`` +
      tool ``tool_call_id`` correlation.
    * Cost includes the skill's per_call_usd.
    """
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_native_openai_agent(tmp_path)

    bundle = load_agent(agent_dir)
    assert len(bundle.skills) == 1
    assert bundle.skills[0].spec.name == "add-one"

    fake = _FakeClient()
    fake.chat.completions.responses = [
        # Turn 1: model wants to call add-one(x=41).
        _FakeChatCompletion(
            choices=[
                _FakeChoice(
                    message=_FakeMessage(
                        content=None,
                        tool_calls=[
                            _FakeToolCall(
                                id="call_42",
                                function=_FakeFunctionCall(
                                    name="add-one",
                                    arguments='{"x": 41}',
                                ),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=_FakeUsage(prompt_tokens=10, completion_tokens=4),
        ),
        # Turn 2: final answer after seeing the tool result.
        _FakeChatCompletion(
            choices=[
                _FakeChoice(
                    message=_FakeMessage(content='{"answer": "42"}'),
                    finish_reason="stop",
                )
            ],
            usage=_FakeUsage(prompt_tokens=20, completion_tokens=8),
        ),
    ]
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    from movate.core.models import AgentRuntime  # noqa: PLC0415

    storage = InMemoryStorage()
    await storage.init()
    tracer = NullTracer()
    registry = ProviderRegistry(default_litellm=provider)
    registry.register(AgentRuntime.NATIVE_OPENAI, provider)
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
    # Skill cost (0.0005) was added to the run total.
    assert response.metrics.cost_usd >= 0.0005

    # Two SDK calls — one per turn.
    assert len(fake.chat.completions.calls) == 2

    # Turn 1 carried the tool spec via the default (OpenAI-style) to_tool_spec.
    turn1_tools = fake.chat.completions.calls[0].get("tools") or []
    assert turn1_tools, "expected tool specs on the first turn"
    assert turn1_tools[0]["type"] == "function"
    assert turn1_tools[0]["function"]["name"] == "add-one"
    # The schema gets forwarded as ``parameters``.
    assert "parameters" in turn1_tools[0]["function"]

    # Turn 2 carried the full history with tool_calls + tool_call_id
    # correlation preserved.
    turn2_messages = fake.chat.completions.calls[1]["messages"]
    assistant_with_tool_calls = next(
        m for m in turn2_messages if m["role"] == "assistant" and m.get("tool_calls")
    )
    assert assistant_with_tool_calls["tool_calls"][0]["id"] == "call_42"
    assert assistant_with_tool_calls["tool_calls"][0]["function"]["name"] == "add-one"

    tool_result_msg = next(m for m in turn2_messages if m["role"] == "tool")
    assert tool_result_msg["tool_call_id"] == "call_42"
    # Skill output JSON-serialised into the content.
    assert '"y": 42' in tool_result_msg["content"]
