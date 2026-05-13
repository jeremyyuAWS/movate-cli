"""Adaptive Card for a successful agent run.

Card layout (top to bottom):

1. Header — "✅ {agent} v{agent_version}" in large weight
2. Subtle "you asked:" line + the user's input (one-liner)
3. Container (style=emphasis) — the agent's actual response body,
   pretty-printed JSON or a single TextBlock if it's just a string
4. FactSet — cost, latency, run id, provider, prompt hash (last 8)
5. Actions — "Open trace" button when a Langfuse public host is
   configured (else omitted)

The card is a **pure function** of a :class:`RunView` plus the
optional Langfuse host URL. Easy to test, easy to swap, no SDK
dependency.
"""

from __future__ import annotations

from typing import Any

from movate.runtime.schemas import RunView
from movate.teams_bot.cards._common import (
    action_open_url,
    container,
    empty_card,
    fact,
    fact_set,
    format_cost,
    format_latency_ms,
    pretty_json,
    text_block,
    truncate_input,
)


def build_run_result_card(
    run: RunView,
    *,
    langfuse_public_host: str | None = None,
) -> dict[str, Any]:
    """Render a successful :class:`RunView` as an Adaptive Card spec.

    ``langfuse_public_host`` (e.g. ``https://langfuse.movate.com``) is
    optional — when provided, the card gets a "View trace" button that
    deep-links to the run's Langfuse trace. Omitting it keeps the card
    self-contained (useful for demos where Langfuse isn't routable).

    Returns the card JSON ready to drop into an
    :class:`Attachment.content`. The caller is responsible for wrapping
    it as ``{"contentType": ADAPTIVE_CARD_CONTENT_TYPE, "content": <this>}``.
    """
    card = empty_card()

    # Header — large + bold, with the green check signalling success.
    card["body"].append(
        text_block(
            f"✅ {run.agent} v{run.agent_version}",
            weight="Bolder",
            size="Large",
            color="Good",
        )
    )

    # Input echo so the viewer sees what was asked. ``isSubtle=True``
    # de-emphasises this compared to the actual response.
    card["body"].append(
        text_block(
            f"you asked: {truncate_input(run.input)}",
            is_subtle=True,
        )
    )

    # Response body. Wrap in a Container with emphasis-style so it
    # visually separates from the metadata. Output may be None on
    # the wire (e.g. if a run terminated without producing one — rare,
    # but defensive).
    response_text = pretty_json(run.output) if run.output is not None else "(no output)"
    card["body"].append(
        container(
            [
                text_block("Response", weight="Bolder", is_subtle=True),
                # ``fontType=Monospace`` would be nicer here for JSON but
                # has spotty Teams support across surfaces — TextBlock
                # default keeps wrap behaviour consistent.
                text_block(response_text),
            ],
            style="emphasis",
        )
    )

    # Metadata FactSet — operators want cost + latency at a glance.
    facts = [
        fact("Cost", format_cost(run.metrics.cost_usd)),
        fact("Latency", format_latency_ms(run.metrics.latency_ms)),
        fact("Run id", run.run_id),
        fact("Provider", run.provider),
        # Last 8 chars of prompt hash is enough to spot a prompt edit
        # across runs without making the card noisy.
        fact("Prompt", run.prompt_hash[-8:]),
    ]
    card["body"].append(fact_set(facts))

    # Trace deep-link — only when we know where Langfuse lives. The
    # run id is the trace key in Langfuse (we set this in the tracer).
    if langfuse_public_host:
        # langfuse_public_host should not have a trailing slash; strip
        # one defensively so misconfig doesn't produce double-slashes.
        host = langfuse_public_host.rstrip("/")
        card["actions"].append(
            action_open_url(
                "View trace",
                f"{host}/trace/{run.run_id}",
            )
        )

    # Empty actions array confuses some Teams renderers — drop it
    # cleanly if nothing was added.
    if not card["actions"]:
        del card["actions"]

    return card
