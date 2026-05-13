"""Adaptive Card for a failed run (or any user-facing error).

Two error sources funnel into this card:

* **Runtime failure** — agent ran but produced an error
  (schema mismatch, model outage, policy violation). The
  :class:`RunView.error` field carries the category + message.
* **Client failure** — the runtime never accepted the job
  (auth failure, agent not found, malformed input). Surface as a
  plain :class:`MovateClientError` from the wrapper.

Both render identically here: a red "❌" header, the failure
category, the operator-friendly message, and an optional hint
("did you mean: ...", "configure MOVATE_API_KEY", etc.).

**No stack traces.** Stack traces live in the runtime logs / Langfuse.
Teams gets just enough to know what went wrong; the trace link covers
the deep-dive case.
"""

from __future__ import annotations

from typing import Any

from movate.teams_bot.cards._common import (
    container,
    empty_card,
    text_block,
)


def build_error_card(
    *,
    title: str,
    message: str,
    hint: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """Render a failure as an Adaptive Card spec.

    Args:
        title: One-line summary. Renders large + red. Example:
            ``"Couldn't run faq-agent"``.
        message: The actual error. Operator-friendly prose, NOT a
            stack trace. Example: ``"agent output did not match schema:
            missing required field 'message'"``.
        hint: Optional one-line suggestion for how to fix. Example:
            ``"Check the agent's schema/output.json"`` or
            ``"Try: @movate run faq-agent {\\"question\\": \\"...\\"}"``.
        category: Optional short code rendered as a subtle prefix
            (``schema_error``, ``rate_limit``, ``auth_error``). Maps
            to the runtime's :class:`ErrorInfo.code` field when known.

    Returns the card JSON ready to drop into an ``Attachment.content``.
    """
    card = empty_card()

    # Header — large bold red.
    card["body"].append(
        text_block(
            f"❌ {title}",
            weight="Bolder",
            size="Large",
            color="Attention",
        )
    )

    # Optional category prefix in subtle text. Operators recognize
    # categories at a glance (e.g. ``rate_limit`` → "wait + retry").
    if category:
        card["body"].append(
            text_block(
                f"category: {category}",
                is_subtle=True,
            )
        )

    # Main message. Wrap in a Container with attention style so the
    # red theme threads through the body, not just the header.
    card["body"].append(
        container(
            [text_block(message)],
            style="attention",
        )
    )

    # Optional hint, dim. Always last so the eye lands on it after
    # reading the failure.
    if hint:
        card["body"].append(
            text_block(
                f"💡 {hint}",
                is_subtle=True,
            )
        )

    # Drop the empty actions array — same reason as run_result.
    if not card["actions"]:
        del card["actions"]

    return card
