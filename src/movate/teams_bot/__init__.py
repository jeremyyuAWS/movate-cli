"""Teams bot — Movate's self-serve front door for non-technical users (ADR 003).

This package is a thin HTTP client of the existing v0.5 runtime
(:mod:`movate.runtime`) wrapped in the Microsoft Bot Framework's
Activity protocol so it can be exposed as a Teams app. Operators run
``mdk teams-bot serve`` next to ``mdk serve``; the bot forwards each
``@movate <command>`` to the runtime and renders the reply as a
Teams message.

Scope of slice 3.1.a (this PR)
-------------------------------

Skeleton only — the wire works end-to-end with plain-text replies:

* :mod:`movate.teams_bot.activity` — Pydantic models for the Bot
  Framework Activity types we care about. Hand-rolled instead of
  pulling in ``botbuilder-core`` (~30MB of transitive deps) since
  the protocol is small JSON and we don't need JWT validation in
  the local-dev skeleton. The full SDK lands when we add
  production auth.
* :mod:`movate.teams_bot.parser` — extract slash command + args
  from an incoming Activity. Strips the ``@BotName`` mention so
  ``@movate run faq-agent {"q":"hi"}`` becomes
  ``("run", "faq-agent", {"q":"hi"})``.
* :mod:`movate.teams_bot.handler` — async function that dispatches
  on the parsed command and returns a reply Activity. Replies are
  plain text for now (``help``, ``ping``); Adaptive Cards land in
  slice 3.1.b.
* :mod:`movate.teams_bot.app` — FastAPI app exposing
  ``POST /api/messages`` (the Bot Framework webhook) and
  ``GET /health`` (liveness).
* ``mdk teams-bot serve`` (in :mod:`movate.cli.teams_bot`) — boots
  the FastAPI app via uvicorn.

What slice 3.1.a does NOT do (deferred to later sub-PRs)
--------------------------------------------------------

* **No Adaptive Cards** (3.1.b) — replies are plain text.
* **No actual agent execution** (3.1.b) — the ``run`` command
  reports back what it parsed; integration with
  :class:`MovateClient` lands once we have card rendering for the
  result.
* **No identity binding / user→tenant mapping** (3.1.c) — every
  invocation uses a hardcoded fleet API key from
  ``MOVATE_TEAMS_FLEET_API_KEY``.
* **No JWT validation** of incoming Bot Framework requests (later
  hardening PR) — anyone who knows the URL can post. Acceptable
  for local dev + alpha pilot; required before public exposure.
* **No file attachment handling** (3.1.b/c) — uploads ignored.
* **No Teams manifest / appPackage** (3.1.e) — `manifest.json`
  ships later when we register the bot in Azure Bot Service.

Optional install:

    uv add 'movate-cli[teams]'
"""

from movate.teams_bot.activity import Activity, ChannelAccount, ConversationAccount
from movate.teams_bot.handler import handle_activity
from movate.teams_bot.parser import ParsedCommand, parse_command

__all__ = [
    "Activity",
    "ChannelAccount",
    "ConversationAccount",
    "ParsedCommand",
    "handle_activity",
    "parse_command",
]
