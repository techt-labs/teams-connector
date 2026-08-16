"""The connector's own inbound Teams endpoint.

WHY THIS EXISTS SEPARATELY FROM ``api.py``

``api.py`` is the *outbound* surface — the door the other end knocks on
to start a thread or post a reply. This module is the *inbound* one: the
door Azure Bot Service knocks on when a human says something in Teams.

A standalone connector (no host application around it) has to receive
those activities itself. So this is the bot's messaging endpoint: it
validates the Bot Framework JWT, records the conversation so the bot can
reply into it later, and hands the message to :func:`handle_inbound`,
which forwards it to whatever is attached.

WHY IT IS NOT ALWAYS MOUNTED

When the connector runs *inside* a host that already owns a Bot
Framework webhook (as it does in this repository's assessment app), the
host validates and feeds :func:`handle_inbound` directly, and this
module is simply not included — mounting it would mean two endpoints
racing to process the same activity. ``app.py`` mounts it for the
standalone deployment; the host does not. That is the whole distinction.
"""
from __future__ import annotations

import logging

from botbuilder.schema import Activity
from fastapi import APIRouter, Request, Response

from connector import InboundMessage, handle_inbound, store
from connector.teams_send import get_adapter

log = logging.getLogger("connector.webhook")

router = APIRouter(tags=["connector-teams"])


@router.post("/api/messages")
async def teams_messages(request: Request) -> Response:
    """Azure Bot Service -> connector. This is the bot's messaging endpoint.

    Point the Azure Bot registration's messaging endpoint at this path.
    Every request is signed by Bot Framework with a JWT; the adapter
    validates it against our app id *before* the turn handler runs, so an
    unsigned or wrongly-signed request never reaches any logic here.

    Returns 200 on success and 502 on failure. The 502 is deliberate:
    Bot Framework retries a failed delivery, and the webhook is
    idempotent (recording is an upsert, forwarding carries the same
    conversation id), so a retry is safer than dropping a human's answer.
    """
    body = await request.json()
    auth_header = request.headers.get("Authorization", "")
    activity = Activity().deserialize(body)

    async def _on_turn(turn_context) -> None:
        act = turn_context.activity

        # Record first, before any early return. Channel installs arrive
        # as a text-less ``conversationUpdate``; capturing the reference
        # here is exactly what lets the bot post into that channel later
        # and what makes it show up in ``GET /channels``.
        try:
            await store.record_conversation(act)
        except Exception as exc:
            # A missed record costs a *future* send, never the message in
            # hand — must not break the inbound path.
            log.warning(f"connector.webhook: record failed (non-fatal): {exc}")

        text = (act.text or "").strip()
        if not text:
            return  # install / typing / reaction — nothing to forward

        conv = getattr(act, "conversation", None)
        if conv is None or not conv.id:
            return

        # Attribution only, never identity internals. The connector holds
        # no Graph permission, so it forwards the display name Teams
        # supplies and leaves email resolution to whoever owns the
        # directory — the receiving system, not this pass-through.
        sender = getattr(act, "from_property", None)
        speaker = getattr(sender, "name", None) or "unknown participant"

        await handle_inbound(
            InboundMessage(
                conversation_id=conv.id,
                text=text,
                speaker=speaker,
                speaker_email=None,
                # Recorded above already, so the activity is not passed
                # again — handle_inbound would otherwise repeat the write.
                activity_id=getattr(act, "id", None),
            )
        )

    try:
        await get_adapter().process_activity(activity, auth_header, _on_turn)
        return Response(status_code=200)
    except Exception as exc:
        # The exception class only — never the message, which can carry a
        # service URL, and a URL can carry a token in its query string.
        log.error(
            f"connector.webhook processing failed: {type(exc).__name__}"
        )
        return Response(status_code=502)
