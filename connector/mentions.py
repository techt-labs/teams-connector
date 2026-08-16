"""Rendering Teams @-mentions on an outbound activity.

WHY MENTIONS ARE NOT OPTIONAL

A message that merely contains the characters ``@John`` renders in
Teams as grey text: no chip, no highlight, and — the part that matters
— **no notification**. Nobody learns they were asked anything. To make
Teams ping a person, the outbound activity must carry both halves of a
wire-format pair:

  1. ``<at>Name</at>`` markup inside ``Activity.text``; and
  2. a matching ``Mention`` entity in ``Activity.entities`` binding that
     ``<at>`` block to the person's AAD object id.

Teams renders nothing without the entity and pings nobody without the
AAD id, so a connector that cannot do this can deliver a question but
cannot get it noticed.

WHY THIS DUPLICATES ``teams/mentions.py``

The host application has an equivalent helper. Importing it would tie
the connector to a codebase it is meant to outlive, and a smoke test
enforces that it does not. What is duplicated here is ~60 lines of pure
string manipulation with no dependencies; what is *not* duplicated is
the half that matters — ``resolve_aad_object_id``, which reaches into
the host's database and Microsoft Graph.

That split is the real boundary, and it is not arbitrary: **the
connector renders mentions, the caller decides who to mention.** Only
the caller knows which human an answer is wanted from, and only it can
resolve that human to a directory id. By the time a request arrives
here the ids are already resolved, so this module needs no directory
permission at all.

``tests/test_phase18_channel_thread_smoke.py`` pins the two
implementations to byte-identical output on a shared fixture table, so
the copy cannot silently drift.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from botbuilder.schema import ChannelAccount, Mention

log = logging.getLogger("connector.mentions")


def _at_tag(name: str) -> str:
    """The Teams ``<at>...</at>`` markup for a single mention."""
    return f"<at>{name}</at>"


def _wrap_name_in_text(text: str, name: str) -> tuple[str, bool]:
    """Wrap the first whole-word occurrence of ``name`` in ``<at>``.

    Returns ``(new_text, did_replace)``, leaving the text untouched
    when the name does not appear.

    A leading ``@`` is consumed if present: Teams draws its own ``@`` on
    the chip, so letting a literal one through renders ``@<at>John</at>``
    — a double at-sign the user sees.

    Deliberately conservative — only the first match is wrapped. Tagging
    every occurrence would produce several chips for one person, and a
    message that names someone twice usually means it once.
    """
    if not name:
        return text, False
    pattern = re.compile(rf"@?\b{re.escape(name)}\b", re.IGNORECASE)
    new_text, n = pattern.subn(_at_tag(name), text, count=1)
    return new_text, n > 0


def _wrap_email_in_text(text: str, email: str, name: str) -> tuple[str, bool]:
    """Fallback for text that addresses someone by email, not by name.

    Callers compose questions from many sources, and some of them write
    ``@john.doe@example.com`` where a human would have written
    ``@John Doe``. Matching the email as well means the mention still
    lands instead of degrading to plain text.

    Tries the full address first (most specific), then the local part —
    the latter only when preceded by ``@``, so ordinary words are never
    tagged by accident. At most one occurrence is replaced.
    """
    if not email or not name:
        return text, False

    pattern = re.compile(rf"@?{re.escape(email)}", re.IGNORECASE)
    new_text, n = pattern.subn(_at_tag(name), text, count=1)
    if n > 0:
        return new_text, True

    local = email.split("@", 1)[0]
    if local:
        pattern = re.compile(rf"@{re.escape(local)}\b", re.IGNORECASE)
        new_text, n = pattern.subn(_at_tag(name), text, count=1)
        if n > 0:
            return new_text, True

    return text, False


def build_mentions(
    text: str,
    stakeholders: Iterable[dict],
) -> tuple[str, list[Mention]]:
    """Turn plain text plus a list of people into ``(text, entities)``.

    Each entry in ``stakeholders`` is a dict with:

      - ``aad_object_id`` — required; without it Teams cannot ping.
      - ``name`` — the display name expected to appear in ``text``.
      - ``email`` — optional fallback when the text addresses the
        person by email instead.

    Resolution per person: display name, then full email, then
    ``@local-part``.

    Someone who cannot be matched is **skipped rather than half-tagged**.
    Emitting an entity whose ``<at>`` block is absent from the body
    gives Teams a dangling reference, and a message that fails to render
    is worse than one that renders as plain text. Skips are logged at
    info so an operator can see why a ping did not happen.

    Returns the text unchanged and an empty entity list when nothing
    matched.
    """
    if not stakeholders:
        return text, []

    entities: list[Mention] = []
    out_text = text

    for s in stakeholders:
        aad = (s.get("aad_object_id") or "").strip()
        name = (s.get("name") or "").strip()
        email = (s.get("email") or "").strip()
        if not aad:
            if name:
                log.info(
                    f"build_mentions: skipping {name!r} (no AAD object id "
                    f"supplied). Message sends as plain text for this name."
                )
            continue
        if not name:
            continue

        wrapped, replaced = _wrap_name_in_text(out_text, name)
        if not replaced and email:
            wrapped, replaced = _wrap_email_in_text(out_text, email, name)

        if not replaced:
            log.info(
                f"build_mentions: {name!r} not found in message body "
                f"(neither name nor email matched); message sends as "
                f"plain text for this name."
            )
            continue

        out_text = wrapped
        entities.append(
            Mention(
                mentioned=ChannelAccount(id=aad, name=name),
                text=_at_tag(name),
                type="mention",
            )
        )

    return out_text, entities
