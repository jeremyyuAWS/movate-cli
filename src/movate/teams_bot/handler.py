"""Bot Framework Activity → reply Activity dispatcher.

The handler is an async function: it takes an inbound :class:`Activity`
plus a :class:`HandlerContext` (carries the runtime client, langfuse
host, etc.) and returns a :class:`ReplyActivity` (or ``None`` for
activities we deliberately ignore). It does NOT do HTTP — the FastAPI
app calls it and serialises the result.

What changed in slice 3.1.b
---------------------------

* The ``run`` command now actually **executes** the agent via
  :func:`execute_run` against the bot's runtime client. The reply is
  an Adaptive Card built by :mod:`movate.teams_bot.cards` instead of
  plain text. Four outcome variants render as cards: success,
  terminal-failure, timeout, client-failure.
* New :class:`HandlerContext` dataclass carries the per-request
  collaborators (runtime client + langfuse host). Built once at app
  startup and passed in on every call so the handler stays
  trivially testable with a fake context.

What's still deferred (3.1.c+)
------------------------------

* Per-user identity binding (the fleet API key is used for every
  request — issue #67).
* File-attachment ingestion for BYO agent.yaml / dataset (issue #68).
* Confirmation-card "are you sure? this will cost ~$X" gate before
  expensive runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from movate.core.client import MovateClient
from movate.teams_bot.activity import Activity, Attachment, ReplyActivity
from movate.teams_bot.cards import build_error_card, build_run_result_card
from movate.teams_bot.cards._common import ADAPTIVE_CARD_CONTENT_TYPE
from movate.teams_bot.client import RunOutcome, execute_run
from movate.teams_bot.parser import ParsedCommand, parse_command


@dataclass
class HandlerContext:
    """Per-app collaborators handed to the handler on every request.

    Held on ``FastAPI.app.state.handler_ctx`` and passed through the
    endpoint. The underlying ``MovateClient`` is long-lived (one
    instance per bot process — see :func:`build_app`).

    Slice 3.1.b uses ``runtime_client`` and ``langfuse_public_host``.
    Future slices add ``identity_resolver`` for per-user keys (3.1.c)
    and an ``attachment_handler`` for file uploads (3.1.d). Keeping it
    a dataclass means each addition is one new field with a default.
    """

    runtime_client: MovateClient | None = None
    """HTTP client bound to the deployed Movate runtime. ``None`` when
    the operator started the bot without a runtime configured — in
    which case the ``run`` command returns an error card explaining
    how to wire one."""

    langfuse_public_host: str | None = None
    """When set (e.g. ``https://langfuse.movate.com``), successful run
    cards get a "View trace" button deep-linking to the run's trace.
    Off by default; the link only surfaces when we know the host is
    routable for the audience (don't show prospects an internal URL)."""


# Help text shown for `@movate help`. Now mentions `run` is live.
_HELP_TEXT = (
    "👋 movate bot — commands available:\n"
    "\n"
    "• `@movate ping` — liveness check\n"
    "• `@movate run <agent-name> <json-input>` — run an agent and "
    "render the result as a card\n"
    "• `@movate help` — this message\n"
    "\n"
    "More commands coming: `eval` (3.2), `connect` (3.1.c), "
    "`rotate-key` (3.1.c). Track progress in ADR 003."
)


def _text_reply(activity: Activity, text: str) -> ReplyActivity:
    """Build a text-only reply (no card).

    Used for trivial commands like ``ping`` / ``help`` and for cases
    where the parse failed so completely that there's nothing to
    render in card form.
    """
    return ReplyActivity(
        type="message",
        text=text,
        replyToId=activity.id,
        conversation=activity.conversation,
    )


def _card_reply(
    activity: Activity,
    *,
    card: dict[str, Any],
    fallback_text: str = "",
) -> ReplyActivity:
    """Build a reply carrying an Adaptive Card attachment.

    ``fallback_text`` shows on channels that don't render cards
    (none today for Teams, but Bot Framework lets us deploy to other
    channels later). It's also what screen readers fall back to.
    """
    return ReplyActivity(
        type="message",
        text=fallback_text,
        replyToId=activity.id,
        conversation=activity.conversation,
        attachments=[
            Attachment(contentType=ADAPTIVE_CARD_CONTENT_TYPE, content=card),
        ],
    )


async def handle_activity(
    activity: Activity,
    ctx: HandlerContext | None = None,
) -> ReplyActivity | None:
    """Dispatch an inbound Activity to the matching command handler.

    ``ctx`` is optional for back-compat with the 3.1.a test suite that
    didn't pass one — when ``None``, we use an empty default context
    (no runtime client, no langfuse host). The ``run`` path checks for
    a configured client and returns an error card when missing.

    Returns ``None`` for activities we deliberately don't respond to
    (conversationUpdate, empty messages, etc.) — the FastAPI app
    surfaces this as ``HTTP 200`` with an empty body, which Teams
    treats as "no reply, OK".
    """
    if ctx is None:
        ctx = HandlerContext()

    cmd = parse_command(activity)

    if cmd.action == "empty":
        # Bot was added to a channel, or user sent a message that's
        # just an @mention with no command. Either way: don't spam.
        return None

    if cmd.action == "ping":
        return _text_reply(activity, "pong")

    if cmd.action == "help":
        return _text_reply(activity, _HELP_TEXT)

    if cmd.action == "run":
        return await _handle_run(activity, cmd, ctx)

    # Unknown command — render the static help as a friendly fallback.
    first_word = cmd.raw_args.split(maxsplit=1)[0] if cmd.raw_args else ""
    return _text_reply(
        activity,
        f"❓ I don't recognize `{first_word}` as a command. Try `@movate help`.",
    )


async def _handle_run(
    activity: Activity,
    cmd: ParsedCommand,
    ctx: HandlerContext,
) -> ReplyActivity:
    """Execute a ``run`` command and render the result as a card.

    Five paths:

    1. **Parse error** — bad JSON or missing arg. Render an error card
       with the parse-error message + a usage hint.
    2. **No runtime configured** — the bot was started without a
       runtime URL. Render an error card pointing at the env var.
    3. **Successful execution** — render the run-result card with the
       agent's response body, cost, latency, trace link.
    4. **Terminal failure** — agent ran but failed. Render an error
       card with the structured error category + message.
    5. **Timeout** — job still progressing beyond budget. Render an
       error card with the job id + ``mdk jobs show`` hint.
    """
    # Path 1: parse error.
    if cmd.parse_error:
        return _card_reply(
            activity,
            card=build_error_card(
                title="Couldn't parse `run`",
                message=cmd.parse_error,
                hint=(
                    'Usage: `@movate run <agent-name> {"...": "..."}`. '
                    "JSON must be a single object."
                ),
                category="parse_error",
            ),
            fallback_text=f"Couldn't parse run: {cmd.parse_error}",
        )

    # Path 2: no runtime client.
    if ctx.runtime_client is None:
        return _card_reply(
            activity,
            card=build_error_card(
                title="No runtime configured",
                message=(
                    "This bot wasn't started with a runtime URL. "
                    "The `run` command needs a deployed Movate runtime to call."
                ),
                hint=(
                    "Restart with `mdk teams-bot serve --runtime-url "
                    "http://...` or set MOVATE_RUNTIME_URL in the env."
                ),
                category="config_error",
            ),
            fallback_text="No runtime configured for this bot.",
        )

    # Paths 3-5: actually execute.
    outcome = await execute_run(
        client=ctx.runtime_client,
        agent=cmd.agent,
        input_payload=cmd.input,
    )
    return _render_outcome(activity, outcome, ctx)


def _render_outcome(
    activity: Activity,
    outcome: RunOutcome,
    ctx: HandlerContext,
) -> ReplyActivity:
    """Pick the right card template for a :class:`RunOutcome` variant."""
    if outcome.kind == "success" and outcome.run is not None:
        return _card_reply(
            activity,
            card=build_run_result_card(
                outcome.run,
                langfuse_public_host=ctx.langfuse_public_host,
            ),
            fallback_text=_success_fallback_text(outcome),
        )

    if outcome.kind == "timeout":
        return _card_reply(
            activity,
            card=build_error_card(
                title="Job still running",
                message=outcome.message,
                hint=outcome.hint,
                category="timeout",
            ),
            fallback_text=f"Job still running: {outcome.job_id}",
        )

    # terminal_failure OR client_failure both render via the error card.
    title = "Run failed" if outcome.kind == "terminal_failure" else "Couldn't submit run"
    return _card_reply(
        activity,
        card=build_error_card(
            title=title,
            message=outcome.message,
            hint=outcome.hint or None,
            category=outcome.category or None,
        ),
        fallback_text=f"{title}: {outcome.message}",
    )


def _success_fallback_text(outcome: RunOutcome) -> str:
    """Plain-text fallback for the success path.

    Renders on channels that don't support Adaptive Cards (none today,
    but Bot Framework can deploy to e.g. Slack via the same activities
    — fallback text matters there). One-line summary of the response.
    """
    if outcome.run is None or outcome.run.output is None:
        return "✅ run succeeded"
    # Use a compact dump so the fallback fits in a single channel line.
    return f"✅ {json.dumps(outcome.run.output, ensure_ascii=False)}"
