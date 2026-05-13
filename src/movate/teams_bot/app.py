"""FastAPI app exposing the Bot Framework webhook.

Two routes:

* ``POST /api/messages`` — Bot Framework webhook. Parses the inbound
  Activity, dispatches to :func:`handle_activity`, and returns the
  reply Activity as the response body (inline-reply mode).
* ``GET /health`` — liveness probe for ACA / `mdk doctor`. Returns
  200 with a fixed JSON body; never touches storage.

The app deliberately does NOT validate the Bot Framework JWT yet —
that's a hardening PR. For 3.1.a, anyone who knows the URL can post;
acceptable for local dev + alpha pilot, NOT for production exposure.
``MOVATE_TEAMS_FLEET_API_KEY`` (read by later slices) is the only
secret the bot needs.

The FastAPI app construction is gated behind a function rather than
a module-level ``app`` so importing the module under
``movate-cli[teams]`` doesn't blow up when ``fastapi`` isn't
installed. The CLI command imports + calls :func:`build_app` only
after the optional extras have been resolved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from movate.teams_bot.activity import Activity
from movate.teams_bot.handler import handle_activity

if TYPE_CHECKING:
    from fastapi import FastAPI


def build_app() -> FastAPI:
    """Construct the Teams-bot FastAPI app.

    Importing FastAPI inline means a dev install without the
    ``[teams]`` extra (which pulls in fastapi/uvicorn from the
    ``[runtime]`` extra) can still import the rest of the package
    — only this function fails.
    """
    try:
        from fastapi import FastAPI  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "the 'fastapi' package is required for the Teams bot. "
            "Install with: uv add 'movate-cli[teams]'"
        ) from exc

    app = FastAPI(
        title="movate teams-bot",
        description="Bot Framework webhook bridging Teams to the Movate runtime.",
        version="0.7.0a",
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe. Never touches storage or external services.

        Deliberately separate from a deeper ``/ready`` (TBD) because
        ACA's liveness probe fires every few seconds and shouldn't
        depend on anything that can be slow or flaky."""
        return {"status": "ok", "service": "movate-teams-bot"}

    # Declared with ``Activity`` directly as the body parameter so
    # FastAPI runs Pydantic validation for us — malformed JSON and
    # bad Activity shape both surface as HTTP 422 with the same
    # validation-error envelope. The two failure modes were distinct
    # in an earlier draft (400 vs 400 with different messages); the
    # combined-422 path is the FastAPI-idiomatic shape and is what
    # the Bot Framework Emulator + Teams already handle.
    #
    # Pydantic accepts both the field name and the alias by default,
    # so the wire's ``"from": {...}`` matches the ``from_`` field
    # without any extra config.

    @app.post("/api/messages")
    async def on_message(activity: Activity) -> dict[str, Any]:
        """Bot Framework webhook.

        Bot Framework posts an Activity JSON object; we dispatch on
        the parsed command and return the reply Activity inline.
        Teams (and the Bot Framework Emulator) accept inline replies
        — no callback to the Bot Framework connector needed.

        Errors:

        * Malformed JSON / bad Activity shape → 422 via FastAPI's
          Pydantic validation envelope.
        * Handler raised → 500. Returning a 5xx tells Teams to retry,
          which is the right behaviour for transient errors. For
          deterministic failures (bad command, missing agent), the
          handler returns a 200 with an error-text reply instead.
        """
        reply = await handle_activity(activity)
        if reply is None:
            # Teams accepts an empty 200 as "no reply, OK".
            return {}
        return reply.to_wire()

    return app
