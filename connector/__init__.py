"""A Microsoft Teams connector you can attach anything to.

WHAT THIS IS

Teams integration is the hard, unglamorous part: registering an Azure
Bot Service app, getting the manifest and resource-specific consent
right, capturing a conversation reference that still works days later,
and posting back into a channel long after the original request ended.
This package does that, and nothing else.

What sits on the other end is your choice:

    Microsoft Teams  <->  [ connector ]  <->  your system
     (Azure Bot Svc)       one table          - a homegrown assistant
                           pass-through       - a vendor agent platform
                                              - any HTTP endpoint

Attach it with configuration (``CONNECTOR_INBOUND_URL``) or, when your
code runs in this same process, with :func:`set_fallback_handler`.
This repository happens to ship a homegrown assistant that uses the
second path, but the connector holds no reference to that assistant
and runs without it.

WHAT IT DELIBERATELY DOES NOT DO

No language model runs here. The connector does not decide what to
say, does not parse message content, and — the part that is easy to
get wrong — does not correlate Teams conversations with sessions on
the other end. It knows how to reach a conversation and it moves text
in both directions. Everything else belongs to whoever created the
work. See ``store/schema.sql`` for why that line is drawn there.

That narrowness is the security argument: there is no prompt to inject
and no model output to trust.

INDEPENDENCE IS ENFORCED, NOT ASSUMED

Nothing under ``connector/`` may import from the host application. A
smoke test asserts this, because a boundary that is only a convention
stops being true within a month.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from botbuilder.schema import Activity

from connector import inbound, store
from connector.inbound import ForwardError

log = logging.getLogger("connector")

__all__ = [
    "InboundMessage",
    "handle_inbound",
    "has_handler",
    "set_fallback_handler",
    "inbound",
    "store",
]


@dataclass(frozen=True)
class InboundMessage:
    """One message that arrived from Teams."""

    conversation_id: str
    text: str
    speaker: Optional[str] = None
    speaker_email: Optional[str] = None
    activity: Optional[Activity] = None
    # Teams' own id for this message. Carried so a handler can
    # recognise an edited message it has already seen; passing the whole
    # activity for that would re-trigger the conversation write above.
    activity_id: Optional[str] = None


InboundHandler = Callable[["InboundMessage"], Awaitable[bool]]

_fallback: Optional[InboundHandler] = None


def set_fallback_handler(handler: Optional[InboundHandler]) -> None:
    """Handle inbound messages in-process instead of forwarding them.

    The override point for a host that embeds the connector — this
    repository registers its assessment assistant here. The handler
    returns True when it has taken ownership of the message.

    Only consulted when no ``CONNECTOR_INBOUND_URL`` is configured:
    explicit operator configuration outranks a compiled-in default.
    Pass ``None`` to restore the bare-pipe behaviour.
    """
    global _fallback
    _fallback = handler


def has_handler() -> bool:
    """True when an in-process fallback handler is registered.

    Exposed so a host can tell whether the connector will actually do
    anything with a message before offering it one. Without this the
    only available signal is "is a token set", which is true in
    installs that mount the API but route messages elsewhere.
    """
    return _fallback is not None


def _frame(text: str, speaker: Optional[str]) -> str:
    """Label relayed text with its human origin.

    The other end sees many conversations; without an attribution line
    it cannot tell a stakeholder's answer from an operator's aside.
    Plain prefix rather than structured metadata, because a receiving
    endpoint is not required to parse anything.
    """
    who = speaker or "someone"
    return f"[Teams] {who}:\n{text}"


async def handle_inbound(message: InboundMessage) -> bool:
    """Route one inbound Teams message. True if the connector owned it.

    The whole routing policy, in one readable place:

        record the conversation (so we can reply later)
          -> a forwarding URL configured?  -> POST it there
          -> else a fallback handler?      -> hand off
          -> else                          -> not ours

    Note what is absent: no lookup of which session this belongs to.
    Every message from a conversation is forwarded with its
    ``conversation_id``, and the receiving system decides who cares.

    A forwarding failure returns False rather than True, so the caller
    falls through to its own handling instead of the message being
    silently swallowed.
    """
    if message.activity is not None:
        try:
            await store.record_conversation(message.activity)
        except Exception as exc:
            # Failing to record costs a *future* send, not the message
            # in hand, so this must never break the inbound path.
            log.warning(f"connector: could not record conversation: {exc}")

    if not message.text:
        return False

    if inbound.is_configured():
        try:
            await inbound.forward(
                conversation_id=message.conversation_id,
                text=_frame(message.text, message.speaker),
                speaker=message.speaker,
                speaker_email=message.speaker_email,
            )
        except ForwardError as exc:
            log.error(f"connector: forwarding failed, falling through: {exc}")
            return False
        return True

    if _fallback is not None:
        return await _fallback(message)

    return False
