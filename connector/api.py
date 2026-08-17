"""HTTP surface the other end calls to reach Teams.

WHY THIS MODULE EXISTS

Agent platforms generally cannot start a Teams conversation on their
own. Where a native Teams app exists it is usually an *entry point* — a
human @-mentions it and a new session begins — with no way for an
already-running session to post somewhere. (Devin, for example, ships
bidirectional session/thread sync for Slack; Teams has no equivalent.)
This router is the outbound half that is missing.

The caller reaches it one of two ways, and the choice is a network
question, not a code one:

  * as a **custom MCP server** — ergonomic and typed, but some vendors
    now run MCP on their own infrastructure rather than the session
    VM, which requires this endpoint to be publicly reachable; or
  * as **plain HTTP from the session**, with the token supplied as a
    session secret — works inside a VPC.

Both speak this same API, so nothing here depends on which is used.

Seven endpoints: health, where can I speak (conversations/channels),
who is there (members), start a thread, speak there, forget a place. There is no ``/bind`` and no
session id anywhere in this file — the caller passes the
``conversation_id`` it received when it started a thread, or the one
that arrived on the inbound side, and keeps its own record of which of
its sessions that concerns. See ``store/schema.sql`` for why that is
the right side of the line.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from connector import inbound, store
from connector.settings import settings
from connector.teams_members import list_channel_members
from connector.teams_send import send_to_reference
from connector.teams_threads import create_channel_thread

log = logging.getLogger("connector.api")

router = APIRouter(prefix="/api/connector", tags=["connector"])


async def require_token(authorization: str = Header(default="")) -> None:
    """Bearer-token gate for every endpoint.

    The roster reveals which teams the bot belongs to, and ``say``
    posts to real people, so nothing here is anonymous. Compared with
    :func:`hmac.compare_digest` to keep the check constant-time; the
    token is a shared secret, rotated by editing the environment.
    """
    if not settings.api_token:
        # Failing closed matters more than convenience: an unset token
        # would otherwise mean an open relay into the tenant's Teams.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="connector disabled — CONNECTOR_API_TOKEN is not set",
        )

    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        presented, settings.api_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
        )


class MentionSpec(BaseModel):
    """One person to @-mention, already resolved by the caller.

    The connector holds no directory permission and does not look
    people up: only the caller knows whose answer it wants, and only it
    can turn that into an AAD object id. Bounded lengths because this
    crosses a trust boundary — these values are interpolated into
    outbound activity markup.
    """

    aad_object_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)


class SayRequest(BaseModel):
    """Post ``text`` into a conversation the bot has already seen."""

    conversation_id: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=8000)
    mentions: list[MentionSpec] | None = Field(default=None, max_length=20)


class CreateThreadRequest(BaseModel):
    """Start a new thread in a channel the bot has been added to."""

    channel_conversation_id: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=8000)
    mentions: list[MentionSpec] | None = Field(default=None, max_length=20)


class ForgetRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=500)


@router.get("/health", dependencies=[Depends(require_token)])
async def health() -> dict:
    """Readiness for the dependencies the connector cannot fake."""
    return {
        "storage": store.is_enabled(),
        "forwarding_configured": inbound.is_configured(),
        "teams_configured": bool(settings.teams_app_id),
    }


@router.get("/conversations", dependencies=[Depends(require_token)])
async def list_conversations() -> dict:
    """Conversations the bot can post into, newest activity first.

    This is the only discovery surface in the connector. It lists
    places the bot was actually added — never tenant directory
    contents — so it cannot be used to enumerate the organization.
    """
    convs = await store.list_conversations()
    return {
        "conversations": [
            {
                "conversation_id": c.conversation_id,
                "type": c.conversation_type,
                "name": c.display_name,
            }
            for c in convs
        ]
    }


@router.get("/channels", dependencies=[Depends(require_token)])
async def list_channels() -> dict:
    """Channels the bot can start new threads in.

    A filtered view of ``/conversations``: only channels can host a
    thread, so offering the full list here would invite 404s from
    ``/threads``. Same containment property — these are places the bot
    was added, never a directory listing.

    Excludes thread ids. Every message the bot sees in a channel is
    recorded, and a reply inside a thread carries a ``;messageid=``
    suffix while still typed ``channel`` — but you start a *new* thread
    in the channel root, not inside an existing thread. Listing the
    threads here would offer the agent dozens of near-identical ids that
    all resolve to the same channel and invite it to reply where it
    meant to start fresh. So only channel roots (no suffix) are offered.
    """
    convs = await store.list_conversations()
    return {
        "channels": [
            {"conversation_id": c.conversation_id, "name": c.display_name}
            for c in convs
            if (c.conversation_type or "").lower() == "channel"
            and ";messageid=" not in c.conversation_id
        ]
    }


@router.get("/members", dependencies=[Depends(require_token)])
async def list_members(conversation_id: str) -> dict:
    """People in a channel the bot belongs to.

    The other end calls this to turn a name into the Entra object id an
    @-mention needs, without holding any directory permission of its own.
    Returns only members of a conversation the bot was added to — the
    same containment boundary as ``/channels``, never a tenant listing.

    A 404 means the bot has never seen that conversation; a 502 means
    Teams refused to return the roster.
    """
    if not conversation_id.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="conversation_id is required",
        )

    members = await list_channel_members(conversation_id)
    if members is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "could not read the roster — check that the bot has been "
                "added to that channel and that the id names a channel"
            ),
        )
    return {
        "members": [
            {"name": m.name, "email": m.email, "aad_object_id": m.aad_object_id}
            for m in members
        ]
    }


@router.post("/threads", dependencies=[Depends(require_token)])
async def create_thread(req: CreateThreadRequest) -> dict:
    """Start a new thread in a channel and return its address.

    The returned ``conversation_id`` is the correlation key: every reply
    a human posts inside this thread arrives on the inbound side
    carrying it. Store it against whatever unit of work asked the
    question, and route answers back by looking it up.

    Use one thread per piece of work, not per message — follow-ups go
    through ``/say`` with the id returned here, so the exchange stays a
    single readable conversation instead of scattering across the
    channel.
    """
    mentions = [m.model_dump() for m in (req.mentions or [])]
    result = await create_channel_thread(
        req.channel_conversation_id, req.text, mentions
    )
    if result is None:
        # Deliberately not distinguishing "unknown channel" from "Teams
        # refused" in the status code: both mean the caller cannot
        # currently post there, and the difference is in the logs. The
        # detail text names the likely fix.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "could not create the thread — check that the bot has been "
                "added to that channel and that the id names a channel"
            ),
        )

    conversation_id, _reference = result
    return {"conversation_id": conversation_id}


@router.post("/say", dependencies=[Depends(require_token)])
async def say(req: SayRequest) -> dict:
    """Post text into a Teams conversation.

    Returns as soon as Teams accepts the message. Replies arrive
    asynchronously on the inbound side, tagged with this same
    ``conversation_id`` — there is no waiting here, by design, because
    humans answer in hours or days.
    """
    ref = await store.reference_for_conversation(req.conversation_id)
    if ref is None:
        # Not an error to route around: the bot has never seen an
        # activity there, so it genuinely cannot post. Adding the bot
        # to the conversation is the fix.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown conversation — the bot has not been added to it",
        )

    mentions = [m.model_dump() for m in (req.mentions or [])]
    if not await send_to_reference(ref, req.text, mentions):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Teams rejected the message",
        )

    return {"delivered": True, "conversation_id": req.conversation_id}


@router.post("/forget", dependencies=[Depends(require_token)])
async def forget(req: ForgetRequest) -> dict:
    """Erase everything the connector holds about one conversation.

    Exposed because "can you delete it?" is a question every review
    asks, and no answer to it should require raw SQL. Afterwards the
    bot cannot address that conversation until a new activity arrives
    from it.
    """
    return {"forgotten": await store.forget_conversation(req.conversation_id)}
