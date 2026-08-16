"""Posting into Teams as the bot, from a stored reference.

WHY THE CONNECTOR OWNS ITS OWN ADAPTER

The host application has an equivalent helper, but importing it would
tie the connector to a codebase it is meant to outlive. Bot Framework
adapters are cheap — one object holding an app id and password — so
owning one costs almost nothing and buys complete independence.

WHY NO GRAPH PERMISSION APPEARS HERE

Sending a message is not permission-gated in the way reading is.
Possession of a valid ConversationReference plus the bot's own
credentials *is* the authorization. This is why the connector can only
ever speak into conversations the bot was actually added to.

Posting into a *channel* additionally relies on the resource-specific
``ChannelMessage.Send.Group`` consent declared in the app manifest,
which a team owner grants when installing the app — not a tenant-wide
admin grant.
"""
from __future__ import annotations

import logging

from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.schema import Activity, ActivityTypes, ConversationReference

from connector.mentions import build_mentions
from connector.settings import settings

log = logging.getLogger("connector.teams_send")

_adapter: BotFrameworkAdapter | None = None


def get_adapter() -> BotFrameworkAdapter:
    global _adapter
    if _adapter is None:
        _adapter = BotFrameworkAdapter(
            BotFrameworkAdapterSettings(
                app_id=settings.teams_app_id,
                app_password=settings.teams_app_password,
                # Single-tenant bots must authenticate against their
                # specific tenant rather than the generic Bot Framework
                # directory. Without this we hit AADSTS700016.
                channel_auth_tenant=settings.teams_tenant_id or None,
            )
        )
    return _adapter


async def send_to_reference(
    ref: ConversationReference,
    text: str,
    mentions: list[dict] | None = None,
) -> bool:
    """Post as the bot into a conversation using a *stored* reference.

    Replays what Teams actually gave us, so it works for channels,
    group chats and 1:1 threads alike, and honours the regional Bot
    Framework endpoint recorded at capture time rather than assuming
    the global one.

    ``mentions`` are already-resolved ``{aad_object_id, name, email}``
    dicts. Supplying them is what makes Teams actually notify the person
    rather than rendering their name as grey text; see ``mentions.py``.
    Omitting them preserves the original plain-text behaviour exactly.

    Returns False rather than raising: a failed send is an operational
    event the caller reports, not an exception that should unwind an
    inbound webhook.
    """
    if ref is None:
        log.error("send_to_reference: no reference supplied")
        return False

    final_text, entities = build_mentions(text, mentions or [])

    delivered = {"ok": False}

    async def _logic(turn_context):
        activity = Activity(type=ActivityTypes.message, text=final_text)
        if entities:
            activity.entities = entities
        await turn_context.send_activity(activity)
        delivered["ok"] = True

    try:
        await get_adapter().continue_conversation(
            ref, _logic, bot_id=settings.teams_app_id
        )
    except Exception as exc:
        # Bot Framework raises a wide variety of transport and auth
        # errors here; none of them should carry credentials, but log
        # the class only to be certain.
        log.error(f"send_to_reference failed: {type(exc).__name__}: {exc}")
        return False

    return delivered["ok"]
