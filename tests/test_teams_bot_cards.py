"""Adaptive Card builders for the Teams bot — pure-function tests.

Cards are JSON dicts. Tests assert on schema correctness + key fields
without exercising the SDK or HTTP. Everything here is hermetic.

Reference: https://adaptivecards.io/explorer/AdaptiveCard.html
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from movate.core.models import ErrorInfo, JobStatus, Metrics, TokenUsage
from movate.runtime.schemas import RunView
from movate.teams_bot.cards import build_error_card, build_run_result_card
from movate.teams_bot.cards._common import (
    ADAPTIVE_CARD_CONTENT_TYPE,
    ADAPTIVE_CARD_VERSION,
    format_cost,
    format_latency_ms,
    pretty_json,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Sentinel so `output=None` actually means "pass None" (the agent
# produced no output), distinct from "caller didn't override the default".
_UNSET: Any = object()


def _run_view(
    *,
    output: Any = _UNSET,
    error: ErrorInfo | None = None,
    cost_usd: float = 0.0042,
    latency_ms: int = 1234,
    agent: str = "faq-agent",
    agent_version: str = "0.1.0",
    run_id: str = "run-abc123",
    prompt_hash: str = "sha256:abcdef0123456789",
) -> RunView:
    """Build a minimal RunView for card tests.

    ``output=None`` is distinct from ``output=_UNSET``: the former is
    "the agent ran but produced no output" (rare, defensive case),
    the latter is "use the default test output". The card builder
    handles both — these tests cover both paths.
    """
    resolved_output = {"answer": "An AI agent platform."} if output is _UNSET else output
    return RunView(
        run_id=run_id,
        job_id="job-xyz",
        agent=agent,
        agent_version=agent_version,
        prompt_hash=prompt_hash,
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="1.0",
        pricing_version="2026.05.01",
        status=JobStatus.SUCCESS if error is None else JobStatus.ERROR,
        input={"question": "what is movate?"},
        output=resolved_output,
        metrics=Metrics(
            tokens=TokenUsage(input=42, output=18),
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        ),
        error=error,
        created_at=datetime(2026, 5, 13, 16, 0, 0, tzinfo=UTC),
    )


def _card_text_flat(card: dict[str, Any]) -> str:
    """Flat-join every TextBlock and FactSet for substring assertions."""
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
    return "\n".join(out)


# ---------------------------------------------------------------------------
# _common helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_cost_sub_cent_uses_6dp() -> None:
    """Sub-cent demos must not render as `$0.00`."""
    assert format_cost(0.0001) == "$0.000100"


@pytest.mark.unit
def test_format_cost_cent_and_above_uses_2dp() -> None:
    assert format_cost(0.42) == "$0.42"
    assert format_cost(12.5) == "$12.50"


@pytest.mark.unit
def test_format_latency_ms_under_second() -> None:
    assert format_latency_ms(750) == "750ms"


@pytest.mark.unit
def test_format_latency_ms_over_second() -> None:
    assert format_latency_ms(1200) == "1.2s"
    assert format_latency_ms(23_500) == "23.5s"


@pytest.mark.unit
def test_pretty_json_preserves_unicode() -> None:
    """ensure_ascii=False so emoji/accents render as glyphs in Teams."""
    rendered = pretty_json({"name": "naïve résumé 🎯"})
    assert "naïve" in rendered
    assert "🎯" in rendered


@pytest.mark.unit
def test_pretty_json_truncates_long_output() -> None:
    """Pathological inputs (10KB response) get cut with a clear marker
    so the card stays renderable on Teams mobile."""
    huge = {"data": "x" * 10_000}
    rendered = pretty_json(huge)
    assert "truncated" in rendered
    # Renders to roughly the configured max, not the original 10KB.
    assert len(rendered) < 2500


# ---------------------------------------------------------------------------
# build_run_result_card
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_result_card_has_correct_schema_and_type() -> None:
    """Every Adaptive Card must declare the right $schema + type +
    version so Teams renders it. A missing version is a common bug."""
    card = build_run_result_card(_run_view())
    assert card["$schema"].startswith("http://adaptivecards.io")
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == ADAPTIVE_CARD_VERSION


@pytest.mark.unit
def test_run_result_card_renders_agent_and_version_in_header() -> None:
    card = build_run_result_card(_run_view(agent="faq-agent", agent_version="0.1.0"))
    text = _card_text_flat(card)
    assert "faq-agent" in text
    assert "v0.1.0" in text
    # Success uses the green check.
    assert "✅" in text


@pytest.mark.unit
def test_run_result_card_includes_response_body() -> None:
    """The agent's output JSON must be in the card body — that's the
    point of the demo."""
    card = build_run_result_card(_run_view(output={"answer": "42"}))
    text = _card_text_flat(card)
    assert "42" in text


@pytest.mark.unit
def test_run_result_card_includes_cost_and_latency_facts() -> None:
    card = build_run_result_card(_run_view(cost_usd=0.0042, latency_ms=750))
    text = _card_text_flat(card)
    # Cost appears in 6dp because it's sub-cent.
    assert "Cost: $0.004200" in text
    assert "Latency: 750ms" in text


@pytest.mark.unit
def test_run_result_card_omits_trace_action_when_no_langfuse_host() -> None:
    """Without a configured Langfuse host, the trace button shouldn't
    render — don't show prospects a 404 link."""
    card = build_run_result_card(_run_view())
    # actions key should be absent or empty.
    assert not card.get("actions")


@pytest.mark.unit
def test_run_result_card_includes_trace_action_when_host_set() -> None:
    card = build_run_result_card(
        _run_view(run_id="run-abc123"),
        langfuse_public_host="https://langfuse.movate.com",
    )
    actions = card.get("actions", [])
    assert actions, "expected a trace-link action"
    assert actions[0]["type"] == "Action.OpenUrl"
    assert actions[0]["url"] == "https://langfuse.movate.com/trace/run-abc123"


@pytest.mark.unit
def test_run_result_card_trace_action_strips_trailing_slash() -> None:
    """Defensive: a misconfigured host with a trailing slash shouldn't
    produce a double-slash URL."""
    card = build_run_result_card(
        _run_view(run_id="run-x"),
        langfuse_public_host="https://langfuse.movate.com/",
    )
    actions = card.get("actions", [])
    assert actions[0]["url"] == "https://langfuse.movate.com/trace/run-x"


@pytest.mark.unit
def test_run_result_card_handles_none_output() -> None:
    """Defensive: a run that terminated without producing an output
    (rare) shouldn't crash the card."""
    card = build_run_result_card(_run_view(output=None))
    text = _card_text_flat(card)
    assert "(no output)" in text


@pytest.mark.unit
def test_run_result_card_renders_input_echo() -> None:
    """The "you asked: ..." line shows the user what they sent — useful
    when scrolling channel history."""
    view = _run_view()
    card = build_run_result_card(view)
    text = _card_text_flat(card)
    assert "what is movate?" in text


@pytest.mark.unit
def test_run_result_card_preserves_unicode_in_response() -> None:
    """End-to-end unicode preservation through the card builder."""
    card = build_run_result_card(_run_view(output={"answer": "naïve résumé 🎯"}))
    rendered = json.dumps(card, ensure_ascii=False)
    assert "naïve résumé 🎯" in rendered


# ---------------------------------------------------------------------------
# build_error_card
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_error_card_has_correct_schema_and_type() -> None:
    card = build_error_card(title="Test", message="boom")
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == ADAPTIVE_CARD_VERSION


@pytest.mark.unit
def test_error_card_renders_title_and_message() -> None:
    card = build_error_card(
        title="Couldn't run faq-agent",
        message="missing required field 'message'",
    )
    text = _card_text_flat(card)
    assert "Couldn't run faq-agent" in text
    assert "missing required field" in text
    assert "❌" in text


@pytest.mark.unit
def test_error_card_renders_hint_when_provided() -> None:
    card = build_error_card(
        title="Run failed",
        message="rate limit",
        hint="Wait a few seconds and re-ask.",
    )
    text = _card_text_flat(card)
    assert "Wait a few seconds" in text
    # Hint gets the lightbulb prefix.
    assert "💡" in text


@pytest.mark.unit
def test_error_card_omits_hint_when_not_provided() -> None:
    card = build_error_card(title="boom", message="kaboom")
    text = _card_text_flat(card)
    assert "💡" not in text


@pytest.mark.unit
def test_error_card_renders_category_prefix() -> None:
    """The category code helps operators triage by class — surfaces as
    a subtle prefix line."""
    card = build_error_card(
        title="Run failed",
        message="...",
        category="rate_limit",
    )
    text = _card_text_flat(card)
    assert "category: rate_limit" in text


@pytest.mark.unit
def test_error_card_omits_category_when_unknown() -> None:
    card = build_error_card(title="boom", message="kaboom")
    text = _card_text_flat(card)
    assert "category:" not in text


# ---------------------------------------------------------------------------
# Content-type sanity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_content_type_is_adaptive_card_mime() -> None:
    """The mime type string is Teams' exact match for rendering as a
    card — drift here breaks rendering on every channel."""
    assert ADAPTIVE_CARD_CONTENT_TYPE == "application/vnd.microsoft.card.adaptive"
