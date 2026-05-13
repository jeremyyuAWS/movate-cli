"""Pydantic models for the Bot Framework Activity protocol.

We hand-roll the subset of the Activity schema we depend on instead of
pulling in ``botbuilder-core``. The full schema is sprawling (hundreds
of optional fields covering 6 channels, file transfers, end-of-conversation
codes, etc.) and most of it doesn't apply to Teams text chat. Keeping
the surface tight makes the bot easy to reason about and to test.

Reference: https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference#activity-object

Fields we omit
--------------

* ``serviceUrl`` / ``replyToId`` / ``channelData`` — needed for actual
  reply delivery (later PR). For 3.1.a the bot composes a reply Activity
  but the HTTP handler returns it inline as the response body rather
  than POSTing back to the Bot Framework connector. This works for
  Teams' inline-response mode and skips the auth handshake.
* ``attachments``, ``attachmentLayout`` — file upload + Adaptive Card
  shapes. Adaptive Cards arrive in 3.1.b; file uploads in 3.1.b/c.
* ``valueSchema``, ``code``, ``locale``, ``localTimestamp``, etc. — not
  used by our handlers.

If a downstream slice needs a field we haven't modelled, add it here
with a clear comment. Activity is a Pydantic model with
``extra="allow"`` so unknown fields don't reject — we just don't read
them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChannelAccount(BaseModel):
    """A participant in a conversation — bot or user.

    Teams gives us the user's AAD object id in ``aadObjectId`` (when
    available); we'll need that for the identity-binding slice (3.1.c).
    For now we use ``id`` as an opaque per-user handle.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    """The channel-specific id for this participant. For Teams users
    this is the per-tenant Teams user id; for the bot it's the bot's
    AAD app id."""

    name: str = ""
    """Display name. Used in log lines + Adaptive Cards."""

    aad_object_id: str | None = Field(default=None, alias="aadObjectId")
    """The user's Azure AD object id when known. Required for the
    identity-binding slice — that's the stable key we map a Movate
    API key against."""

    role: str = ""


class ConversationAccount(BaseModel):
    """Identifies the conversation the Activity belongs to.

    Conversations are channel-scoped: a 1:1 DM has a unique id; a
    channel post does too. We pass this id back unchanged on replies
    so Teams routes the response to the right place.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    is_group: bool = Field(default=False, alias="isGroup")
    conversation_type: str = Field(default="", alias="conversationType")
    """``personal`` (1:1 DM), ``groupChat``, or ``channel``. Lets the
    handler tailor responses — e.g. ``/movate connect`` only makes
    sense in a 1:1 DM."""

    tenant_id: str | None = Field(default=None, alias="tenantId")
    """The Microsoft tenant id of the conversation. Useful for
    enforcing "this bot only serves Movate-internal channels" rules
    once we ship multi-tenant. Optional for 3.1.a."""


class Mention(BaseModel):
    """One ``@mention`` entity inside an Activity.

    Teams attaches mentions to ``Activity.entities`` so the bot can
    distinguish between text where it was @-mentioned vs. plain text.
    The ``mentioned`` sub-object identifies WHO; the ``text`` field
    carries the exact substring to strip from ``Activity.text``
    before parsing the command.
    """

    model_config = ConfigDict(extra="allow")

    type: str = ""
    text: str = ""
    """The exact substring to strip — e.g. ``<at>movate</at>``."""

    mentioned: ChannelAccount = Field(default_factory=ChannelAccount)


class Activity(BaseModel):
    """The unit of communication on Bot Framework.

    Every inbound HTTP POST to ``/api/messages`` is one Activity; every
    reply is one Activity. The most useful field for our skeleton is
    ``text`` (post-mention-strip via :func:`parser.parse_command`).
    """

    model_config = ConfigDict(extra="allow")

    type: str = "message"
    """``message`` (a user said something), ``conversationUpdate``
    (bot was added/removed from a chat), ``invoke`` (Adaptive Card
    action), etc. For 3.1.a we only handle ``message``; other types
    get an empty reply."""

    id: str = ""
    """Unique id for this Activity within the conversation. Stamped
    by the channel."""

    timestamp: str = ""
    """ISO-8601 timestamp from the channel."""

    channel_id: str = Field(default="", alias="channelId")
    """``msteams`` for Teams. Other Bot Framework channels are out
    of scope for this PR."""

    from_: ChannelAccount = Field(default_factory=ChannelAccount, alias="from")
    """Sender. ``from`` is a Python keyword so we alias it. Use
    ``Activity.from_`` in code; the wire payload reads ``from``."""

    conversation: ConversationAccount = Field(default_factory=ConversationAccount)

    recipient: ChannelAccount = Field(default_factory=ChannelAccount)
    """Who was meant to receive this — the bot, when an inbound
    activity arrives at our webhook."""

    text: str = ""
    """User-facing text. May include the ``@BotName`` mention as a
    substring — the parser strips it. May be empty for non-text
    activities (file uploads, card action invokes, etc.)."""

    entities: list[Mention] = Field(default_factory=list)
    """Structured entities attached to the activity. Mentions live
    here. Other entity types (clientInfo, etc.) we ignore."""


class Attachment(BaseModel):
    """A single attachment on a Bot Framework Activity.

    For our purposes, this is exclusively an **Adaptive Card** —
    ``contentType="application/vnd.microsoft.card.adaptive"`` with the
    card's JSON spec under ``content``. Teams renders the card inline
    in the message; the optional ``text`` field on the parent activity
    shows as fallback text in channels that don't support cards.

    Other attachment types (file uploads, image cards, HeroCard, etc.)
    are out of scope — we use Adaptive Cards exclusively because they
    render correctly on Teams desktop, mobile, and the Bot Framework
    Emulator without per-surface tweaking.
    """

    model_config = ConfigDict(extra="allow")

    content_type: str = Field(alias="contentType")
    """For Adaptive Cards: ``application/vnd.microsoft.card.adaptive``."""

    content: dict[str, Any] = Field(default_factory=dict)
    """The card JSON spec. For Adaptive Cards, this is the object that
    follows the schema at https://adaptivecards.io/schemas/adaptive-card.json
    — ``{type:"AdaptiveCard", version:"1.5", body:[...], actions:[...]}``."""


class ReplyActivity(BaseModel):
    """Shape of the JSON we return as the inline reply.

    A subset of the full Activity — only the fields Teams reads from
    an inline-mode reply. ``replyToId`` correlates back to the
    incoming Activity so the response threads correctly.

    Replies can carry EITHER plain text (``text``) OR an Adaptive Card
    (``attachments``) — Teams renders both when both are set, with the
    text appearing above the card. Slice 3.1.a uses text only; 3.1.b+
    swaps to attachments for richer rendering.
    """

    model_config = ConfigDict(extra="allow")

    type: str = "message"
    text: str = ""
    reply_to_id: str = Field(default="", alias="replyToId")
    conversation: ConversationAccount = Field(default_factory=ConversationAccount)
    attachments: list[Attachment] = Field(default_factory=list)
    """Adaptive Card attachments — empty in the 3.1.a skeleton, populated
    by the card builders in 3.1.b. Each entry's ``contentType`` should be
    ``application/vnd.microsoft.card.adaptive`` and ``content`` carries
    the card JSON spec."""

    def to_wire(self) -> dict[str, Any]:
        """Pydantic dump using by_alias=True so the wire payload has
        ``replyToId`` not ``reply_to_id``. The FastAPI endpoint
        returns this dict directly."""
        return self.model_dump(by_alias=True, exclude_none=True)
