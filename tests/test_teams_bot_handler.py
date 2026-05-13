"""Handler + client-wrapper integration: scripts a fake MovateClient
and drives the real handler end-to-end.

The card builders are unit-tested separately (test_teams_bot_cards.py);
the parser likewise (test_teams_bot.py). This file proves the *wiring*:
handler picks the right card variant for each :class:`RunOutcome` and
forwards the right context (langfuse host, etc.) to the builders.

Hermetic. No HTTP, no Bot Framework SDK. Uses a hand-rolled fake
MovateClient that mimics the four methods the wrapper uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from movate.core.client import MovateClientError
from movate.core.models import ErrorInfo, JobStatus, Metrics, TokenUsage
from movate.runtime.schemas import JobView, RunAccepted, RunView
from movate.teams_bot.activity import Activity
from movate.teams_bot.handler import HandlerContext, handle_activity

# ---------------------------------------------------------------------------
# Activity builder (shared shape with test_teams_bot.py)
# ---------------------------------------------------------------------------


def _activity_payload(text: str) -> dict[str, Any]:
    """Wire-format Teams Activity dict with a mention markup."""
    return {
        "type": "message",
        "id": "act-1",
        "channelId": "msteams",
        "text": text,
        "from": {"id": "u1", "name": "tester"},
        "conversation": {"id": "c1", "conversationType": "channel"},
        "recipient": {"id": "b1", "name": "movate"},
        "entities": [
            {
                "type": "mention",
                "text": "<at>movate</at>",
                "mentioned": {"id": "b1", "name": "movate"},
            }
        ],
    }


def _build_run_view(
    *,
    output: dict[str, Any] | None = None,
    error: ErrorInfo | None = None,
    run_id: str = "run-xyz",
    job_id: str = "job-abc",
) -> RunView:
    return RunView(
        run_id=run_id,
        job_id=job_id,
        agent="faq-agent",
        agent_version="0.1.0",
        prompt_hash="sha256:test",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="1.0",
        pricing_version="2026.05.01",
        status=JobStatus.SUCCESS if error is None else JobStatus.ERROR,
        input={"question": "hi"},
        output=output,
        metrics=Metrics(tokens=TokenUsage(input=10, output=4), cost_usd=0.001, latency_ms=500),
        error=error,
        created_at=datetime(2026, 5, 13, 16, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# FakeMovateClient — scripted responses for execute_run's four stages
# ---------------------------------------------------------------------------


@dataclass
class _FakeMovateClient:
    """Mimics the four MovateClient methods the wrapper calls.

    Tests script ``submit_response`` / ``submit_exc``, ``job_response``
    / ``job_exc``, ``run_response`` / ``run_exc`` to drive each branch
    of :func:`execute_run`. Calls are recorded for assertions.
    """

    submit_response: RunAccepted | None = None
    submit_exc: Exception | None = None
    job_responses: list[JobView] = field(default_factory=list)
    job_exc: Exception | None = None
    run_response: RunView | None = None
    run_exc: Exception | None = None

    submit_calls: list[dict[str, Any]] = field(default_factory=list)
    job_calls: list[str] = field(default_factory=list)
    run_calls: list[str] = field(default_factory=list)

    async def submit_job(self, **kwargs: Any) -> RunAccepted:
        self.submit_calls.append(kwargs)
        if self.submit_exc is not None:
            raise self.submit_exc
        assert self.submit_response is not None
        return self.submit_response

    async def wait_for_terminal(
        self,
        job_id: str,
        *,
        poll_interval_seconds: float = 1.0,
        max_wait_seconds: float | None = None,
    ) -> JobView:
        """Stand-in for the real wait_for_terminal. Returns the last
        scripted job_response — tests script one terminal JobView per
        run since the wrapper only needs one terminal value."""
        self.job_calls.append(job_id)
        if self.job_exc is not None:
            raise self.job_exc
        assert self.job_responses, "scripted client has no job_responses"
        return self.job_responses[-1]

    async def get_run(self, run_id: str) -> RunView:
        self.run_calls.append(run_id)
        if self.run_exc is not None:
            raise self.run_exc
        assert self.run_response is not None
        return self.run_response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _card_text(card: dict[str, Any]) -> str:
    """Flatten Adaptive Card body to a single string for assertions."""
    out: list[str] = []

    def walk(items: list[Any]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("type", "")
            if t == "TextBlock":
                out.append(str(item.get("text", "")))
            elif t == "Container":
                walk(item.get("items", []) or [])
            elif t == "FactSet":
                for f in item.get("facts", []) or []:
                    out.append(f"{f.get('title', '')}: {f.get('value', '')}")

    walk(card.get("body", []) or [])
    for a in card.get("actions", []) or []:
        if isinstance(a, dict):
            out.append(f"action: {a.get('title', '')} → {a.get('url', '')}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_success_returns_run_result_card() -> None:
    """End-to-end happy path: bot calls runtime, gets RunView, renders
    a run_result card."""
    fake = _FakeMovateClient()
    fake.submit_response = RunAccepted(job_id="job-1", status=JobStatus.QUEUED)
    fake.job_responses = [
        JobView(
            job_id="job-1",
            kind="agent",  # type: ignore[arg-type]
            target="faq-agent",
            status=JobStatus.SUCCESS,
            input={"question": "hi"},
            result_run_id="run-1",
            created_at=datetime(2026, 5, 13, tzinfo=UTC),
        )
    ]
    fake.run_response = _build_run_view(output={"answer": "Hello!"}, run_id="run-1", job_id="job-1")

    ctx = HandlerContext(runtime_client=fake)  # type: ignore[arg-type]
    activity = Activity.model_validate(
        _activity_payload('<at>movate</at> run faq-agent {"question": "hi"}')
    )
    reply = await handle_activity(activity, ctx)

    assert reply is not None
    assert reply.attachments
    card = reply.attachments[0].content
    text = _card_text(card)
    assert "✅" in text
    assert "faq-agent" in text
    assert "Hello!" in text
    # Fallback text mentions the response for non-card channels.
    assert "Hello!" in reply.text


@pytest.mark.asyncio
async def test_handler_success_with_langfuse_host_adds_trace_button() -> None:
    """When the context carries a langfuse host, the success card gets
    an Action.OpenUrl pointing at the run's trace."""
    fake = _FakeMovateClient()
    fake.submit_response = RunAccepted(job_id="j1", status=JobStatus.QUEUED)
    fake.job_responses = [
        JobView(
            job_id="j1",
            kind="agent",  # type: ignore[arg-type]
            target="faq-agent",
            status=JobStatus.SUCCESS,
            input={},
            result_run_id="run-trace-x",
            created_at=datetime(2026, 5, 13, tzinfo=UTC),
        )
    ]
    fake.run_response = _build_run_view(output={"answer": "ok"}, run_id="run-trace-x", job_id="j1")
    ctx = HandlerContext(
        runtime_client=fake,  # type: ignore[arg-type]
        langfuse_public_host="https://langfuse.movate.com",
    )
    activity = Activity.model_validate(_activity_payload('<at>movate</at> run faq-agent {"q":"x"}'))
    reply = await handle_activity(activity, ctx)
    assert reply is not None
    card = reply.attachments[0].content
    actions = card.get("actions", [])
    assert any(a.get("url") == "https://langfuse.movate.com/trace/run-trace-x" for a in actions)


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_submission_auth_error_returns_client_failure_card() -> None:
    """Submission rejected (bad API key) → ``client_failure`` card with
    an auth_error hint pointing at the env var."""
    fake = _FakeMovateClient()
    fake.submit_exc = MovateClientError(status_code=401, code="auth_error", message="unauthorized")

    ctx = HandlerContext(runtime_client=fake)  # type: ignore[arg-type]
    activity = Activity.model_validate(_activity_payload('<at>movate</at> run faq-agent {"q":"x"}'))
    reply = await handle_activity(activity, ctx)
    assert reply is not None
    card = reply.attachments[0].content
    text = _card_text(card)
    assert "Couldn't submit run" in text
    assert "unauthorized" in text
    # Hint should reference the env var.
    assert "MOVATE_TEAMS_FLEET_API_KEY" in text


@pytest.mark.asyncio
async def test_handler_terminal_error_returns_terminal_failure_card() -> None:
    """Agent ran but failed (schema validation, content filter, etc.)
    → ``terminal_failure`` card with the agent's structured error."""
    fake = _FakeMovateClient()
    fake.submit_response = RunAccepted(job_id="job-2", status=JobStatus.QUEUED)
    fake.job_responses = [
        JobView(
            job_id="job-2",
            kind="agent",  # type: ignore[arg-type]
            target="faq-agent",
            status=JobStatus.ERROR,
            input={"q": "x"},
            result_run_id="run-failed",
            error=ErrorInfo(type="schema_error", message="missing field 'message'"),
            created_at=datetime(2026, 5, 13, tzinfo=UTC),
        )
    ]
    fake.run_response = _build_run_view(
        output=None,
        error=ErrorInfo(type="schema_error", message="missing field 'message'"),
    )
    ctx = HandlerContext(runtime_client=fake)  # type: ignore[arg-type]
    activity = Activity.model_validate(_activity_payload('<at>movate</at> run faq-agent {"q":"x"}'))
    reply = await handle_activity(activity, ctx)
    assert reply is not None
    card = reply.attachments[0].content
    text = _card_text(card)
    assert "Run failed" in text
    assert "missing field" in text
    # Category surfaces in subtle prefix.
    assert "category: schema_error" in text
    # Hint references the agent's input schema.
    assert "input schema" in text.lower() or "agent.yaml" in text.lower()


@pytest.mark.asyncio
async def test_handler_timeout_returns_job_id_in_card() -> None:
    """Job exceeds budget → ``timeout`` card surfaces the job id so the
    user can poll later with ``mdk jobs show``."""
    fake = _FakeMovateClient()
    fake.submit_response = RunAccepted(job_id="job-slow", status=JobStatus.QUEUED)
    fake.job_exc = TimeoutError("over budget")
    ctx = HandlerContext(runtime_client=fake)  # type: ignore[arg-type]
    activity = Activity.model_validate(_activity_payload('<at>movate</at> run faq-agent {"q":"x"}'))
    reply = await handle_activity(activity, ctx)
    assert reply is not None
    card = reply.attachments[0].content
    text = _card_text(card)
    assert "Job still running" in text
    assert "job-slow" in text
    # Hint suggests `mdk jobs show <id>`.
    assert "mdk jobs show" in text


@pytest.mark.asyncio
async def test_handler_skips_runtime_call_when_parse_error() -> None:
    """A parse error short-circuits before hitting the runtime — saves
    a roundtrip on obviously-malformed inputs."""
    fake = _FakeMovateClient()
    ctx = HandlerContext(runtime_client=fake)  # type: ignore[arg-type]
    activity = Activity.model_validate(_activity_payload("<at>movate</at> run faq-agent {bad"))
    reply = await handle_activity(activity, ctx)
    assert reply is not None
    # Runtime was never called.
    assert not fake.submit_calls
    assert not fake.job_calls
    assert not fake.run_calls
    # Card was rendered.
    card = reply.attachments[0].content
    text = _card_text(card)
    assert "Couldn't parse" in text


# ---------------------------------------------------------------------------
# Help / ping still work after the handler refactor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_ping_still_returns_plain_text_after_refactor() -> None:
    """Ping is a trivial liveness check — no card needed."""
    ctx = HandlerContext()
    activity = Activity.model_validate(_activity_payload("<at>movate</at> ping"))
    reply = await handle_activity(activity, ctx)
    assert reply is not None
    assert reply.text == "pong"
    # No attachment — text-only.
    assert not reply.attachments


@pytest.mark.asyncio
async def test_handler_help_mentions_run_as_live_now() -> None:
    """The help text should reflect that ``run`` is live in 3.1.b,
    not the 3.1.a "echo only" note."""
    ctx = HandlerContext()
    activity = Activity.model_validate(_activity_payload("<at>movate</at> help"))
    reply = await handle_activity(activity, ctx)
    assert reply is not None
    assert "run" in reply.text
    # 3.1.a's "echoed back for now" note should be gone.
    assert "echoed" not in reply.text.lower()
