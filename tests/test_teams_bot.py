"""Teams bot slice 3.1.a — Activity parser + handler + app round-trip.

Hermetic: no network, no Bot Framework SDK. Every fixture is plain
JSON shaped like a real Teams Activity. The bot's HTTP surface is
exercised via FastAPI's TestClient — Bot Framework's wire format is
just JSON over HTTPS, so a plain POST round-trips correctly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from movate.teams_bot.activity import Activity, ReplyActivity
from movate.teams_bot.app import build_app
from movate.teams_bot.handler import handle_activity
from movate.teams_bot.parser import parse_command

# ---------------------------------------------------------------------------
# Test fixtures — Activity builders mirroring the real Teams JSON shape
# ---------------------------------------------------------------------------


def _activity_payload(
    text: str,
    *,
    activity_type: str = "message",
    activity_id: str = "act-1",
    mention_text: str = "<at>movate</at>",
    include_mention: bool = True,
    conversation_id: str = "conv-1",
    user_id: str = "user-1",
    user_aad: str = "aad-1",
) -> dict[str, Any]:
    """Build a wire-format Teams Activity dict.

    Matches the shape Microsoft Bot Framework actually posts: ``from``
    (not ``from_``), camelCase outer keys (``replyToId``,
    ``conversationType``, ``aadObjectId``). Pydantic aliases handle the
    translation when we load through :class:`Activity.model_validate`.
    """
    payload: dict[str, Any] = {
        "type": activity_type,
        "id": activity_id,
        "channelId": "msteams",
        "text": text,
        "from": {
            "id": user_id,
            "name": "Alpha Tester",
            "aadObjectId": user_aad,
        },
        "conversation": {
            "id": conversation_id,
            "conversationType": "channel",
            "tenantId": "tenant-movate",
        },
        "recipient": {"id": "bot-id", "name": "movate"},
    }
    if include_mention:
        payload["entities"] = [
            {
                "type": "mention",
                "text": mention_text,
                "mentioned": {"id": "bot-id", "name": "movate"},
            }
        ]
    return payload


# ---------------------------------------------------------------------------
# Activity model
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_activity_loads_wire_payload_with_camel_case_aliases() -> None:
    """Validates aliases: ``from`` → ``from_``, ``aadObjectId`` →
    ``aad_object_id``, ``conversationType`` → ``conversation_type``."""
    payload = _activity_payload("@movate ping")
    activity = Activity.model_validate(payload)
    assert activity.text == "@movate ping"
    assert activity.from_.aad_object_id == "aad-1"
    assert activity.conversation.conversation_type == "channel"
    assert activity.conversation.tenant_id == "tenant-movate"


@pytest.mark.unit
def test_activity_accepts_unknown_fields() -> None:
    """``extra='allow'`` on Activity — the wire format has dozens of
    optional fields we don't care about (serviceUrl, channelData,
    etc.). They must pass through without rejecting."""
    payload = _activity_payload("hi")
    payload["serviceUrl"] = "https://smba.trafficmanager.net/amer/"
    payload["channelData"] = {"team": {"id": "..."}, "tenant": {"id": "..."}}
    activity = Activity.model_validate(payload)
    # Unknown fields don't surface as attributes but don't reject either.
    assert activity.text == "hi"


@pytest.mark.unit
def test_reply_activity_serialises_reply_to_id_with_alias() -> None:
    """Outbound payloads must use ``replyToId`` (camelCase), not the
    Python ``reply_to_id``. ``to_wire()`` enforces this."""
    reply = ReplyActivity(type="message", text="pong", replyToId="act-1")
    wire = reply.to_wire()
    assert wire["replyToId"] == "act-1"
    assert "reply_to_id" not in wire


# ---------------------------------------------------------------------------
# parse_command
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_strips_mention_markup_from_text() -> None:
    """``<at>movate</at> ping`` → command ``ping``. The mention's
    literal text is in ``entities`` so we strip by substring match."""
    activity = Activity.model_validate(_activity_payload("<at>movate</at> ping"))
    cmd = parse_command(activity)
    assert cmd.action == "ping"


@pytest.mark.unit
def test_parse_help() -> None:
    activity = Activity.model_validate(_activity_payload("<at>movate</at> help"))
    cmd = parse_command(activity)
    assert cmd.action == "help"


@pytest.mark.unit
def test_parse_unknown_command_yields_unknown_action() -> None:
    """Unrecognized command → handler shows a friendly error rather
    than the bot crashing or 4xx-ing."""
    activity = Activity.model_validate(_activity_payload("<at>movate</at> fubar baz"))
    cmd = parse_command(activity)
    assert cmd.action == "unknown"
    assert "fubar" in cmd.raw_args


@pytest.mark.unit
def test_parse_non_message_activity_yields_empty() -> None:
    """conversationUpdate (bot added to channel) yields ``empty``, so
    the handler doesn't spam a help message at join time."""
    activity = Activity.model_validate(_activity_payload("", activity_type="conversationUpdate"))
    cmd = parse_command(activity)
    assert cmd.action == "empty"


@pytest.mark.unit
def test_parse_empty_text_after_mention_strip_yields_empty() -> None:
    """User @-mentions the bot with no command. Don't reply."""
    activity = Activity.model_validate(_activity_payload("<at>movate</at>"))
    cmd = parse_command(activity)
    assert cmd.action == "empty"


@pytest.mark.unit
def test_parse_run_happy_path() -> None:
    """``run faq-agent {"question":"hi"}`` parses both the agent name
    and the JSON input."""
    activity = Activity.model_validate(
        _activity_payload('<at>movate</at> run faq-agent {"question": "hi"}')
    )
    cmd = parse_command(activity)
    assert cmd.action == "run"
    assert cmd.agent == "faq-agent"
    assert cmd.input == {"question": "hi"}
    assert cmd.parse_error == ""


@pytest.mark.unit
def test_parse_run_preserves_whitespace_in_json() -> None:
    """JSON values can contain spaces; the split must keep them.
    Confirms ``split(maxsplit=...)`` rather than full tokenisation."""
    activity = Activity.model_validate(
        _activity_payload('<at>movate</at> run faq-agent {"question": "what is movate?"}')
    )
    cmd = parse_command(activity)
    assert cmd.input == {"question": "what is movate?"}


@pytest.mark.unit
def test_parse_run_missing_agent() -> None:
    activity = Activity.model_validate(_activity_payload("<at>movate</at> run"))
    cmd = parse_command(activity)
    assert cmd.action == "run"
    assert cmd.parse_error
    assert "missing agent name" in cmd.parse_error


@pytest.mark.unit
def test_parse_run_missing_json() -> None:
    activity = Activity.model_validate(_activity_payload("<at>movate</at> run faq-agent"))
    cmd = parse_command(activity)
    assert cmd.action == "run"
    assert cmd.agent == "faq-agent"
    assert "missing input JSON" in cmd.parse_error


@pytest.mark.unit
def test_parse_run_invalid_json() -> None:
    activity = Activity.model_validate(
        _activity_payload("<at>movate</at> run faq-agent {not valid")
    )
    cmd = parse_command(activity)
    assert "invalid JSON" in cmd.parse_error


@pytest.mark.unit
def test_parse_run_rejects_json_array() -> None:
    """Top-level input must be a JSON object. An array is a usage error
    — the agent's input schema is a dict."""
    activity = Activity.model_validate(_activity_payload("<at>movate</at> run faq-agent [1, 2, 3]"))
    cmd = parse_command(activity)
    assert "must be a JSON object" in cmd.parse_error


@pytest.mark.unit
def test_parse_command_word_is_case_insensitive() -> None:
    """``PING`` should work the same as ``ping`` — Teams users often
    auto-capitalise."""
    activity = Activity.model_validate(_activity_payload("<at>movate</at> PING"))
    cmd = parse_command(activity)
    assert cmd.action == "ping"


# ---------------------------------------------------------------------------
# handle_activity — slice 3.1.a always returns plain text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_ping_returns_pong() -> None:
    activity = Activity.model_validate(_activity_payload("<at>movate</at> ping"))
    reply = await handle_activity(activity)
    assert reply is not None
    assert reply.text == "pong"
    # Reply threads back to the original Activity via replyToId.
    assert reply.reply_to_id == "act-1"


@pytest.mark.asyncio
async def test_handle_help_returns_help_body() -> None:
    activity = Activity.model_validate(_activity_payload("<at>movate</at> help"))
    reply = await handle_activity(activity)
    assert reply is not None
    # Spot-check the help body — should mention the three available
    # commands for slice 3.1.a.
    assert "ping" in reply.text
    assert "run" in reply.text
    assert "help" in reply.text


@pytest.mark.asyncio
async def test_handle_run_echoes_parsed_args() -> None:
    """Slice 3.1.a echoes the parsed run command back rather than
    executing it. The echo must include the agent + the JSON body
    so an alpha tester can verify the wire."""
    activity = Activity.model_validate(
        _activity_payload('<at>movate</at> run faq-agent {"question": "what is movate?"}')
    )
    reply = await handle_activity(activity)
    assert reply is not None
    assert "faq-agent" in reply.text
    assert "what is movate?" in reply.text
    # The plain-text body explicitly tags itself as a skeleton so the
    # tester doesn't think it's a real execution.
    assert "skeleton" in reply.text.lower() or "no execution" in reply.text.lower()


@pytest.mark.asyncio
async def test_handle_run_renders_parse_error_friendly() -> None:
    """Bad JSON → friendly error reply, NOT an exception."""
    activity = Activity.model_validate(_activity_payload("<at>movate</at> run faq-agent {bad"))
    reply = await handle_activity(activity)
    assert reply is not None
    assert "invalid JSON" in reply.text or "couldn't parse" in reply.text


@pytest.mark.asyncio
async def test_handle_empty_activity_returns_none() -> None:
    """conversationUpdate / empty messages should produce no reply.
    Teams treats ``None`` as ``HTTP 200`` with empty body, which is
    "no reply, OK"."""
    activity = Activity.model_validate(_activity_payload("", activity_type="conversationUpdate"))
    reply = await handle_activity(activity)
    assert reply is None


@pytest.mark.asyncio
async def test_handle_unknown_command_suggests_help() -> None:
    """Unknown command → friendly reply pointing at ``@movate help``."""
    activity = Activity.model_validate(_activity_payload("<at>movate</at> fubar"))
    reply = await handle_activity(activity)
    assert reply is not None
    assert "help" in reply.text.lower()


# ---------------------------------------------------------------------------
# FastAPI app — end-to-end HTTP round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())


@pytest.mark.unit
def test_health_endpoint(client: TestClient) -> None:
    """``GET /health`` returns 200 with the service tag. ACA's liveness
    probe leans on this — must never touch storage or external state."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "movate-teams-bot"


@pytest.mark.unit
def test_post_messages_ping_returns_pong(client: TestClient) -> None:
    """End-to-end: POST a Bot Framework Activity, get a reply Activity
    in the response body. Confirms the wire works."""
    payload = _activity_payload("<at>movate</at> ping")
    resp = client.post("/api/messages", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["text"] == "pong"
    assert body["replyToId"] == "act-1"


@pytest.mark.unit
def test_post_messages_empty_activity_returns_empty_body(client: TestClient) -> None:
    """No reply for conversationUpdate — body is empty dict (Teams
    treats this as "no reply, OK")."""
    payload = _activity_payload("", activity_type="conversationUpdate")
    resp = client.post("/api/messages", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {}


@pytest.mark.unit
def test_post_messages_invalid_json_returns_422(client: TestClient) -> None:
    """Malformed body → 422. FastAPI's Pydantic body parsing rejects
    with the standard validation-error envelope; Teams + the Bot
    Framework Emulator handle this cleanly."""
    resp = client.post(
        "/api/messages",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


@pytest.mark.unit
def test_post_messages_invalid_activity_shape_returns_422(client: TestClient) -> None:
    """Valid JSON but the shape doesn't match Activity → 422 with
    Pydantic's structured error pointing at the bad field. Operators
    diagnosing a Bot Framework Emulator misconfig see exactly which
    key is wrong."""
    resp = client.post(
        "/api/messages",
        json={"type": "message", "id": "x", "conversation": "not-an-object"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # FastAPI's validation envelope is a list of {loc, msg, type, ...}.
    assert isinstance(detail, list) and detail
    assert any("conversation" in str(err.get("loc", "")) for err in detail)


@pytest.mark.unit
def test_post_messages_run_command_echoes(client: TestClient) -> None:
    """End-to-end echo of a run command — proves the full pipeline:
    parse → handle → reply → serialise → JSON."""
    payload = _activity_payload('<at>movate</at> run faq-agent {"question": "what?"}')
    resp = client.post("/api/messages", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"]
    text = body["text"]
    assert "faq-agent" in text
    assert "what?" in text


# ---------------------------------------------------------------------------
# JSON round-trip safety — pretty-print preserves the parsed input
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_echo_preserves_unicode(
    client: TestClient,
) -> None:
    """JSON inputs may contain non-ASCII — the echo path must round-trip
    them. Teams users will type emoji / non-English text in demos."""
    payload = _activity_payload('<at>movate</at> run greet-agent {"name": "naïve résumé 🎯"}')
    resp = client.post("/api/messages", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    # The echo body inlines a pretty-printed JSON dump — the unicode
    # values must survive.
    parsed_input_section = body["text"].split("input:", 1)[1]
    # The pretty-print happens inside the fenced code block.
    assert "naïve résumé 🎯" in parsed_input_section
    # And the structure is valid JSON when extracted (sanity check —
    # if it weren't, our echo would be lying to the user).
    fenced = parsed_input_section.split("```", 2)
    assert len(fenced) >= 2
    json.loads(fenced[1])  # raises if malformed
