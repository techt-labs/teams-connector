"""Reading the member roster of a channel the bot belongs to.

WHY THIS IS HERE, AND WHY IT IS GRAPH-FREE

To @-mention someone, Teams needs their Entra object id — and the caller
usually knows a person only by name or email. This module bridges that
gap without Microsoft Graph: the resource-specific ``TeamMember.Read.Group``
consent, granted when a team owner installs the app, lets the *bot
connector* return the roster of a conversation the bot is in. That is a
different, far smaller grant than a tenant-wide Graph directory read.

WHY ``TeamsInfo.get_paged_members`` WORKS HERE WHEN ``send_message`` DID NOT

The channel-post helper needed a ``CloudAdapterBase``, which our
``BotFrameworkAdapter`` is not. The roster methods do not: they read the
Bot Connector client that the adapter already puts in the turn state, so
borrowing a turn on the channel reference is enough. Verified against the
installed SDK.

CONTAINMENT

Only members of a conversation the bot was added to are ever returned —
never a tenant directory. Same boundary as the rest of the connector:
the bot can see exactly where it was invited, and no call widens that.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from botbuilder.core import TurnContext
from botbuilder.core.teams import TeamsInfo

from connector import store
from connector.settings import settings
from connector.teams_send import get_adapter

log = logging.getLogger("connector.teams_members")


@dataclass(frozen=True)
class Member:
    """One person in a channel, reduced to what a mention needs."""

    name: Optional[str]
    email: Optional[str]
    aad_object_id: Optional[str]


async def list_channel_members(
    channel_conversation_id: str,
) -> Optional[list[Member]]:
    """Roster of a channel the bot is in, or None if it cannot be read.

    ``channel_conversation_id`` must name a conversation the bot has
    already seen (captured at install). Returns None — never raises — on
    an unknown conversation or a Teams-side refusal, matching the rest of
    the connector's "operational event, not exception" convention.
    """
    root_ref = await store.reference_for_conversation(channel_conversation_id)
    if root_ref is None:
        log.error(
            "list_channel_members: no stored reference for the requested "
            "conversation — the bot has not been added to it"
        )
        return None

    collected: dict[str, Any] = {"members": [], "error": None}

    async def _logic(turn_context: TurnContext) -> None:
        # A proactively-continued turn carries no channel_data (the SDK
        # drops it from a ConversationReference), and TeamsInfo's team-id
        # lookup deserializes that field unconditionally — it raises on
        # None. An empty dict makes the team-id lookup return None, which
        # routes get_paged_members down the conversation-members path,
        # i.e. the channel's own roster, which is exactly what we want.
        turn_context.activity.channel_data = {}

        # Page through the roster: a large channel returns members in
        # chunks, and dropping the continuation token would silently
        # truncate the list — which would make a real stakeholder look
        # absent and their question go out with no mention.
        result = await TeamsInfo.get_paged_members(turn_context)
        collected["members"].extend(result.members or [])
        token = result.continuation_token
        while token:
            result = await TeamsInfo.get_paged_members(
                turn_context, continuation_token=token
            )
            collected["members"].extend(result.members or [])
            token = result.continuation_token

    try:
        await get_adapter().continue_conversation(
            root_ref, _logic, bot_id=settings.teams_app_id
        )
    except Exception as exc:
        log.error(
            f"list_channel_members failed: {type(exc).__name__}: {exc}"
        )
        return None

    members: list[Member] = []
    for m in collected["members"]:
        members.append(
            Member(
                name=getattr(m, "name", None),
                # Teams exposes the address on either field depending on
                # account type; prefer the explicit email, fall back to
                # the UPN, which is an email for normal members.
                email=getattr(m, "email", None)
                or getattr(m, "user_principal_name", None),
                aad_object_id=getattr(m, "aad_object_id", None),
            )
        )
    return members
