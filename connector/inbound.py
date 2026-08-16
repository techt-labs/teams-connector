"""Forwarding inbound Teams messages to whatever you attached.

WHY THIS IS ONE MODULE AND NOT A SET OF VENDOR ADAPTERS

An earlier version shipped per-vendor backends that delivered each
message to a specific *session* on the other end. That design required
the connector to know which session a Teams reply belonged to, and
that knowledge does not exist here: the systems on the other end run
many concurrent sessions against the same Teams channel, and only they
know which ones are still listening.

So the contract shrank to its honest size. One POST, one URL:

    {"conversation_id": "...",      # where it came from — reply here
     "text":            "...",      # what the human typed
     "speaker":         "...",      # display name, when Teams gives one
     "speaker_email":   "...",
     "source":          "microsoft-teams"}

Any 2xx means accepted; the body is ignored. What happens next — which
sessions get woken, which are stale — is the receiving system's
business. To say something back, it calls ``POST /api/connector/say``
with the same ``conversation_id``.

SECURITY POSTURE

The optional bearer token is the only credential ever sent, and it is
never logged. Message text is capped before it leaves the process, so
a pathological paste cannot become an unbounded request body.
"""
from __future__ import annotations

import logging

import httpx

from connector.settings import settings

log = logging.getLogger("connector.inbound")

# Generous for a chat turn, small enough that no single message can be
# used to push an unbounded body at the receiving system.
MAX_MESSAGE_CHARS = 16_000

_TRUNCATION_SUFFIX = "\n\n[truncated by connector]"

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class ForwardError(RuntimeError):
    """Forwarding failed. Carries no credential material."""


def is_configured() -> bool:
    """True when a forwarding destination is set."""
    return settings.forwarding_enabled


async def forward(
    conversation_id: str,
    text: str,
    speaker: str | None = None,
    speaker_email: str | None = None,
) -> None:
    """POST one Teams message to the configured endpoint.

    Raises :class:`ForwardError` on any failure so the caller can
    decide what to do — the connector's policy is to fall through
    rather than swallow, because losing a stakeholder's answer is far
    worse than handling it twice.
    """
    if not is_configured():
        raise ForwardError(
            "no forwarding destination — set CONNECTOR_INBOUND_URL to an "
            "http(s) URL"
        )

    if not text.strip():
        raise ForwardError("refusing to forward an empty message")

    body = text[:MAX_MESSAGE_CHARS]
    if len(text) > MAX_MESSAGE_CHARS:
        body += _TRUNCATION_SUFFIX

    headers = {"Content-Type": "application/json"}
    if settings.inbound_token:
        headers["Authorization"] = f"Bearer {settings.inbound_token}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                settings.inbound_url,
                headers=headers,
                json={
                    "conversation_id": conversation_id,
                    "text": body,
                    "speaker": speaker,
                    "speaker_email": speaker_email,
                    "source": "microsoft-teams",
                },
            )
    except httpx.HTTPError as exc:
        # The exception type alone; the string can contain the URL,
        # and the URL can carry a token in a query string.
        raise ForwardError(
            f"transport failure forwarding inbound message: {type(exc).__name__}"
        ) from exc

    if resp.status_code >= 400:
        raise ForwardError(f"destination returned {resp.status_code}")

    # Sizes and status, never content: message text is stakeholder
    # speech and does not belong in logs.
    log.info(
        f"connector.forward conversation={conversation_id} "
        f"chars={len(body)} status={resp.status_code}"
    )
