"""Bot Framework Activity → reply Activity dispatcher.

The handler is a pure async function: it takes an inbound
:class:`Activity` and returns a :class:`ReplyActivity` (or ``None``
for activities we deliberately ignore). It does NOT do HTTP — the
FastAPI app calls it and serialises the result.

Slice 3.1.a replies in plain text. Slice 3.1.b will swap the text
bodies for Adaptive Card JSON without changing the handler's
signature — every path here returns a single Activity-shaped object.

Side effects in this slice: none. No MovateClient calls yet (3.1.b),
no DB writes (3.1.c). The ``run`` command echoes back the parsed
arguments so an alpha tester can verify the bot is wired without
needing a deployed runtime.
"""

from __future__ import annotations

import json

from movate.teams_bot.activity import Activity, ReplyActivity
from movate.teams_bot.parser import ParsedCommand, parse_command

# Help text shown for `@movate help`. Kept here (not in parser.py) so
# slice 3.1.b can swap it for an Adaptive Card without touching the
# parser.
_HELP_TEXT = (
    "👋 movate bot — commands available in this slice (3.1.a):\n"
    "\n"
    "• `@movate ping` — liveness check\n"
    "• `@movate run <agent-name> <json-input>` — submit an agent run "
    "(echoed back for now; live execution lands in slice 3.1.b)\n"
    "• `@movate help` — this message\n"
    "\n"
    "More commands coming in v0.8: `eval`, `connect`, `rotate-key`. "
    "Track progress in ADR 003."
)


def _reply(activity: Activity, text: str) -> ReplyActivity:
    """Build a reply that threads correctly off the inbound activity.

    ``replyToId`` correlates the response to the original message so
    Teams renders it as an in-thread reply rather than a new top-level
    post. ``conversation`` is echoed so the channel routing works.
    """
    return ReplyActivity(
        type="message",
        text=text,
        replyToId=activity.id,
        conversation=activity.conversation,
    )


async def handle_activity(activity: Activity) -> ReplyActivity | None:
    """Dispatch an inbound Activity to the matching command handler.

    Returns ``None`` for activities we deliberately don't respond to
    (conversationUpdate, empty messages, etc.) — the FastAPI app
    surfaces this as ``HTTP 200`` with an empty body, which Teams
    treats as "no reply, OK".
    """
    cmd = parse_command(activity)

    if cmd.action == "empty":
        # Bot was added to a channel, or user sent a message that's
        # just an @mention with no command. Either way: don't spam.
        return None

    if cmd.action == "ping":
        return _reply(activity, "pong")

    if cmd.action == "help":
        return _reply(activity, _HELP_TEXT)

    if cmd.action == "run":
        return _reply(activity, _format_run_echo(cmd))

    # Unknown command.
    return _reply(
        activity,
        (
            f"❓ I don't recognize `{cmd.raw_args.split(maxsplit=1)[0] if cmd.raw_args else ''}` "
            f"as a command. Try `@movate help`."
        ),
    )


def _format_run_echo(cmd: ParsedCommand) -> str:
    """Plain-text echo of a parsed ``run`` command.

    Two paths:

    * **Parse error** (missing agent, bad JSON, non-object input):
      render the error message + a hint about the correct usage.
    * **Successful parse**: echo what we'd submit. This is the
      "loop is wired" signal for alpha testers in 3.1.a; slice
      3.1.b replaces this branch with an actual
      ``MovateClient.submit_and_wait`` call + a result Adaptive
      Card.
    """
    if cmd.parse_error:
        return f"❌ couldn't parse `run`: {cmd.parse_error}"

    # ensure_ascii=False so non-ASCII characters (accents, CJK, emoji)
    # render as the actual glyphs in the Teams reply. The transport is
    # UTF-8 JSON via FastAPI, so this is safe.
    pretty_input = json.dumps(cmd.input, indent=2, ensure_ascii=False)
    return (
        f"✅ parsed `run` (skeleton — no execution yet in 3.1.a):\n"
        f"\n"
        f"agent: `{cmd.agent}`\n"
        f"input: ```{pretty_input}```\n"
        f"\n"
        f"Slice 3.1.b will wire this to the runtime via "
        f"`MovateClient.submit_and_wait` and render the result as an "
        f"Adaptive Card."
    )
