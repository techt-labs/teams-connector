"""Starting a new thread in a Teams channel.

WHY THIS EXISTS

``teams_send.py`` can only speak into a conversation that already
exists. That is enough to answer someone, but not to *start* something:
an agent that decides mid-task it needs a human has nowhere to put the
question. This module is the missing half.

It creates a genuine channel thread — a new top-level post that humans
reply under — and hands back the thread's conversation id. That id is
the correlation key the caller keeps: Teams stamps it on every reply
inside the thread, so an answer arriving hours later routes back to the
work that asked for it without anyone having to guess. One thread per
piece of work also means two concurrent questions to the same person
cannot be confused, because they were never in the same place.

WHY NOT ``TeamsInfo.send_message_to_teams_channel``

That is the obvious call, and the next reader will reach for it. Both
of its branches are broken for this adapter, verified against the
installed SDK:

  * the modern branch requires a ``CloudAdapterBase`` — ours is a
    ``BotFrameworkAdapter``, which is not one — and passes ``bot_app_id``
    positionally into a signature whose first parameter is a
    ``ConversationReference``;
  * the legacy branch does ``result[0]`` on the return of
    ``run_pipeline``, which discards the callback's return value. It
    raises ``TypeError`` before it can return anything.

So we call ``adapter.create_conversation`` directly. Note there is no
``TurnContext`` anywhere below: ``create_conversation`` is a method on
the adapter, and ``TeamsInfo`` only wanted a turn context in order to
*reach* an adapter. We already hold ours.

WHAT PERMISSION THIS NEEDS

None beyond the bot's own credentials. Creating a channel thread is
authorized by possession of a reference to that channel, which only
exists because the bot was added there. This is why the connector stays
free of Microsoft Graph, and why no tenant-wide admin consent is
involved — a team owner installing the app is the entire approval.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from botbuilder.core import TurnContext
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    ConversationParameters,
    ConversationReference,
)

from connector import store
from connector.mentions import build_mentions
from connector.settings import settings
from connector.teams_send import get_adapter

log = logging.getLogger("connector.teams_threads")


def channel_id_from_reference(ref: ConversationReference) -> Optional[str]:
    """The bare ``19:...@thread.tacv2`` channel id from a reference.

    A ``ConversationReference`` carries no ``channel_data`` — the SDK
    drops it when deriving a reference from an activity — so the channel
    id has to come from ``conversation.id``.

    For a channel, that id *is* the channel id, except when the
    reference was captured from a message inside a thread, in which case
    Teams appends ``;messageid=<root>``. Everything up to the first
    semicolon is the channel; the suffix identifies one thread within
    it. Stripping it means a reference captured from a thread reply
    still resolves to the right channel to start a *new* thread in.
    """
    conv = getattr(ref, "conversation", None)
    if conv is None or not conv.id:
        return None
    return conv.id.split(";", 1)[0]


async def create_channel_thread(
    channel_conversation_id: str,
    text: str,
    mentions: Optional[list[dict]] = None,
) -> Optional[tuple[str, ConversationReference]]:
    """Post a new thread into a channel. Returns ``(conversation_id, reference)``.

    ``channel_conversation_id`` must name a conversation the bot has
    already seen — normally the channel itself, captured when the app
    was installed there. ``mentions`` are already-resolved
    ``{aad_object_id, name, email}`` dicts; see ``mentions.py`` for why
    resolution is the caller's job.

    The returned conversation id addresses the new thread and is what
    the caller stores against its own unit of work. The reference is
    returned alongside it for callers that want to hold it directly,
    though it is also recorded here so ``send_to_reference`` works
    against the thread immediately.

    Returns ``None`` rather than raising, matching
    :func:`connector.teams_send.send_to_reference`: a failed create is
    an operational event the caller reports, not an exception that
    should unwind an inbound webhook.
    """
    root_ref = await store.reference_for_conversation(channel_conversation_id)
    if root_ref is None:
        log.error(
            "create_channel_thread: no stored reference for the requested "
            "conversation — the bot has not been added to it"
        )
        return None

    teams_channel_id = channel_id_from_reference(root_ref)
    if not teams_channel_id:
        log.error("create_channel_thread: stored reference carries no conversation id")
        return None

    final_text, entities = build_mentions(text, mentions or [])
    seed = Activity(type=ActivityTypes.message, text=final_text)
    if entities:
        seed.entities = entities

    params = ConversationParameters(
        is_group=True,
        channel_data={"channel": {"id": teams_channel_id}},
        activity=seed,
        # Carried over from the channel reference, not left to default.
        # create_conversation copies this onto the new activity's
        # `recipient`, and a reference derives its `bot` from that — so
        # omitting it yields a reference that cannot be replied through
        # later, which would not surface until the first follow-up.
        bot=root_ref.bot,
    )

    captured: dict[str, Any] = {}

    async def _capture(turn_context: TurnContext) -> None:
        # Written through the closure rather than returned: the adapter
        # passes this callback to run_pipeline, which discards its
        # return value. That discard is precisely the bug in the SDK's
        # own helper (see module docstring).
        activity = turn_context.activity
        conversation = getattr(activity, "conversation", None)
        if conversation is not None and not conversation.conversation_type:
            # Teams does not populate this on the synthetic event
            # activity, and the store would otherwise default it to
            # "personal" — mislabelling the thread in /channels and in
            # any operator listing. We know it is a channel: we just
            # created one there.
            conversation.conversation_type = "channel"
        captured["reference"] = TurnContext.get_conversation_reference(activity)
        captured["activity"] = activity

    try:
        await get_adapter().create_conversation(root_ref, _capture, params)
    except Exception as exc:
        # Bot Framework surfaces transport, auth and Teams-side refusals
        # here. Log the class and message only: neither carries the app
        # password, but the type alone is rarely enough to diagnose.
        log.error(
            f"create_channel_thread failed: {type(exc).__name__}: {exc}"
        )
        return None

    reference: Optional[ConversationReference] = captured.get("reference")
    if reference is None or reference.conversation is None or not reference.conversation.id:
        log.error("create_channel_thread: Teams returned no conversation id")
        return None

    conversation_id = reference.conversation.id

    # Record it so the thread is addressable straight away. Best-effort:
    # the thread exists in Teams either way, and the next inbound reply
    # would record it regardless — losing this write costs a send before
    # the first reply, not the thread.
    activity = captured.get("activity")
    if activity is not None:
        try:
            await store.record_conversation(activity)
        except Exception as exc:
            log.warning(
                f"create_channel_thread: thread created but not recorded: {exc}"
            )

    log.info(
        f"created Teams channel thread (channel={teams_channel_id[:20]}…, "
        f"mentions={len(entities)})"
    )
    return conversation_id, reference


def is_configured() -> bool:
    """True when the bot has credentials to create a thread with."""
    return bool(settings.teams_app_id and settings.teams_app_password)
