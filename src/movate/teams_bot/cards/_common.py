"""Shared helpers for Adaptive Card builders.

Centralises constants (schema URL, card version) and formatting
helpers (cost, latency, JSON pretty-print) so every card renders
consistently. Card builders compose these helpers; tests assert
against the shapes here once instead of redundantly per-card.

Reference: https://adaptivecards.io/explorer/AdaptiveCard.html
Schema version: 1.5 — supported by Teams desktop, web, and mobile
as of 2025. Bumping requires retesting in Teams; 1.5 covers every
element we use (TextBlock, FactSet, Container, ActionSet,
Action.OpenUrl).
"""

from __future__ import annotations

import json
from typing import Any

ADAPTIVE_CARD_VERSION = "1.5"
ADAPTIVE_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"

# Bot Framework's mime-type for Adaptive Card attachments. Teams
# inspects this exact string to decide "render as a card" vs "show
# raw"; any other value renders as a fallback file attachment.
ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

# Max chars before we truncate the response JSON in the rendered card.
# Teams collapses long TextBlocks anyway, but a 50KB response would
# blow up the card rendering in mobile. 2000 chars is enough to show
# a meaningful preview; the full payload is in the runtime trace.
_MAX_RESPONSE_LEN = 2000

# Max chars for the user input echo (top of the result card). Same
# logic — most demo inputs are <200 chars; pathological ones get cut.
_MAX_INPUT_LEN = 500

# Cost-formatting threshold: below 1 cent we render 6 decimal places so
# sub-cent demos don't show as ``$0.00``; above it, 2dp is enough.
_SUB_CENT_THRESHOLD_USD = 0.01

# Latency-formatting threshold: below 1 second we show raw ms; above,
# we collapse to seconds with one decimal.
_MS_TO_S_THRESHOLD = 1000


def empty_card() -> dict[str, Any]:
    """Return a fresh Adaptive Card scaffold with no body / actions.

    Card builders append elements to ``card["body"]`` and actions to
    ``card["actions"]`` as needed. The schema/version/type plumbing
    is the same on every card so we keep it in one place.
    """
    return {
        "$schema": ADAPTIVE_CARD_SCHEMA,
        "type": "AdaptiveCard",
        "version": ADAPTIVE_CARD_VERSION,
        "body": [],
        "actions": [],
    }


def text_block(
    text: str,
    *,
    weight: str = "Default",
    size: str = "Default",
    color: str = "Default",
    wrap: bool = True,
    is_subtle: bool = False,
) -> dict[str, Any]:
    """Build a TextBlock element with the most common knobs.

    Defaults match the Adaptive Cards spec — bumping any knob produces
    bold / larger / coloured / dim text. ``wrap=True`` is the friendly
    default; Teams' default is False which truncates mid-sentence.
    """
    block: dict[str, Any] = {"type": "TextBlock", "text": text, "wrap": wrap}
    if weight != "Default":
        block["weight"] = weight
    if size != "Default":
        block["size"] = size
    if color != "Default":
        block["color"] = color
    if is_subtle:
        block["isSubtle"] = True
    return block


def fact(title: str, value: str) -> dict[str, str]:
    """One row of a FactSet — title : value. Used for cost / latency /
    agent metadata where the label-value pairs are uniform."""
    return {"title": title, "value": value}


def fact_set(facts: list[dict[str, str]]) -> dict[str, Any]:
    """Render a list of facts as a two-column FactSet."""
    return {"type": "FactSet", "facts": facts}


def container(items: list[dict[str, Any]], *, style: str | None = None) -> dict[str, Any]:
    """Group elements into a Container — useful for visually setting
    apart the response body from the metadata.

    ``style`` (None / ``emphasis`` / ``good`` / ``attention`` / ``warning``)
    adds a subtle background tint that matches Teams' theme."""
    c: dict[str, Any] = {"type": "Container", "items": items}
    if style is not None:
        c["style"] = style
    return c


def action_open_url(title: str, url: str) -> dict[str, Any]:
    """Build an Action.OpenUrl — renders as a button under the card."""
    return {"type": "Action.OpenUrl", "title": title, "url": url}


def format_cost(cost_usd: float) -> str:
    """Format a USD cost. Sub-cent costs get 6dp so demos at \\$0.0001
    don't render as ``$0.00``; one-cent-and-up gets 2dp."""
    if cost_usd < _SUB_CENT_THRESHOLD_USD:
        return f"${cost_usd:.6f}"
    return f"${cost_usd:.2f}"


def format_latency_ms(latency_ms: int) -> str:
    """Format latency in ms or seconds depending on magnitude. 750ms,
    1.2s, 23.5s — never raw ``750ms`` for a 30-second run."""
    if latency_ms < _MS_TO_S_THRESHOLD:
        return f"{latency_ms}ms"
    return f"{latency_ms / _MS_TO_S_THRESHOLD:.1f}s"


def pretty_json(data: Any, *, max_chars: int = _MAX_RESPONSE_LEN) -> str:
    """Pretty-print a value for display in a TextBlock.

    ``ensure_ascii=False`` so non-ASCII (accents, emoji, CJK) renders
    correctly in Teams. Truncates at ``max_chars`` with a clear marker
    so demo viewers know they're seeing a preview, not the full body.
    """
    rendered = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max_chars - 80] + (
        f"\n... [truncated; {len(rendered) - max_chars + 80} more chars in full trace]"
    )


def truncate_input(data: Any) -> str:
    """One-line preview of the user's input — drives the "you asked"
    header at the top of the card."""
    rendered = json.dumps(data, ensure_ascii=False)
    if len(rendered) <= _MAX_INPUT_LEN:
        return rendered
    return rendered[: _MAX_INPUT_LEN - 3] + "..."
