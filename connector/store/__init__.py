"""Connector state — an address book, and nothing more.

One fact has to survive across days: how to reach a Teams
conversation. See ``schema.sql`` for the full argument, including why
session correlation deliberately does not live here.

All SQL is written once, with ``?`` placeholders and
``CURRENT_TIMESTAMP``, so the same statements run on PostgreSQL and on
SQLite. The conversation reference is stored as JSON text and never
queried inside, which is what keeps that portability cheap.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from botbuilder.schema import Activity, ConversationReference

from connector.settings import settings

from .postgres import PostgresDriver
from .sqlite import SqliteDriver

log = logging.getLogger("connector.store")

Driver = Union[PostgresDriver, SqliteDriver]

_driver: Optional[Driver] = None

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass(frozen=True)
class ConversationSummary:
    """A conversation the bot can reach, as offered to the other end."""

    conversation_id: str
    conversation_type: Optional[str]
    display_name: Optional[str]


def is_enabled() -> bool:
    """True once storage is configured and started."""
    return _driver is not None


def _load_schema() -> str:
    """The DDL, adapted to the active engine.

    ``schema.sql`` is authored as valid PostgreSQL so it can be handed
    to a DBA unmodified. SQLite has no TIMESTAMPTZ; it stores these as
    ISO-8601 text, which round-trips correctly for our purposes since
    the connector only ever displays these values.
    """
    ddl = _SCHEMA_PATH.read_text()
    if settings.storage_is_sqlite:
        ddl = ddl.replace("TIMESTAMPTZ", "TEXT")
    return ddl


async def startup() -> None:
    """Open the store and ensure the table exists.

    The connector applies its own schema instead of relying on a host
    migration runner — it has to work in a process where no host
    exists. The DDL is idempotent, so running beside a host that also
    knows this table is harmless.
    """
    global _driver
    if _driver is not None or not settings.storage_enabled:
        return

    _driver = (
        SqliteDriver(settings.database_url)
        if settings.storage_is_sqlite
        else PostgresDriver(settings.database_url)
    )
    await _driver.connect()
    await _driver.execute_many_statements(_load_schema())
    engine = "sqlite" if settings.storage_is_sqlite else "postgres"
    log.info(f"connector store ready (engine={engine})")


async def shutdown() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


def _reference_json(activity: Activity) -> str:
    """Serialize the reference Bot Framework needs to reply later."""
    ref: ConversationReference = Activity.get_conversation_reference(activity)
    return json.dumps(ref.serialize())


def _tenant_id(activity: Activity) -> Optional[str]:
    """Tenant id from channel data, when Teams supplies it."""
    data: Any = getattr(activity, "channel_data", None)
    if isinstance(data, dict):
        tenant = data.get("tenant")
        if isinstance(tenant, dict):
            return tenant.get("id")
    return None


async def record_conversation(activity: Activity) -> None:
    """Upsert the conversation this activity arrived in.

    Called on every inbound activity, so it stays cheap. Refreshing the
    reference each time matters: Teams rotates service URLs and the
    stale one eventually stops working.

    For a channel *thread* reply — a conversation id with a
    ``;messageid=`` suffix — the channel *root* is recorded as well. A
    reply proves the bot can reach that channel, and starting a *new*
    thread needs a reference to the root, not to some existing thread.
    Without this the bot could only ever start threads in a channel where
    its own install ``conversationUpdate`` happened to be captured; with
    it, any message the bot sees makes the channel usable.
    """
    if not is_enabled():
        return

    conv = getattr(activity, "conversation", None)
    if conv is None or not conv.id:
        return

    # Teams sets conversation_type on group contexts but omits it for
    # 1:1, where "personal" is implied.
    conv_type = getattr(conv, "conversation_type", None) or "personal"

    await _upsert_conversation(
        conversation_id=conv.id,
        conversation_type=conv_type,
        display_name=getattr(conv, "name", None),
        service_url=activity.service_url,
        tenant_id=_tenant_id(activity),
        reference_json=_reference_json(activity),
    )

    root_id = conv.id.split(";", 1)[0]
    if conv_type.lower() == "channel" and root_id != conv.id:
        # Derive a root reference from this one: same transport (service
        # url, bot, tenant), conversation id stripped back to the channel.
        #
        # Deep-copied via serialize/deserialize before the id is changed:
        # get_conversation_reference ALIASES the activity's conversation
        # object, so mutating it in place would rewrite the activity's own
        # conversation id — and everything downstream of this call (the
        # forward to the other end, correlation on the reply) would see
        # the bare channel id instead of the thread. That exact bug
        # silently broke reply routing once already.
        ref: ConversationReference = Activity.get_conversation_reference(activity)
        root_ref = ConversationReference().deserialize(ref.serialize())
        root_ref.conversation.id = root_id
        await _upsert_conversation(
            conversation_id=root_id,
            conversation_type="channel",
            display_name=getattr(conv, "name", None),
            service_url=activity.service_url,
            tenant_id=_tenant_id(activity),
            reference_json=json.dumps(root_ref.serialize()),
        )


async def _upsert_conversation(
    *,
    conversation_id: str,
    conversation_type: str,
    display_name: Optional[str],
    service_url: Optional[str],
    tenant_id: Optional[str],
    reference_json: str,
) -> None:
    """Write one conversation row. Shared by the direct and root writes."""
    await _driver.execute(
        """
        INSERT INTO connector_conversations
            (conversation_id, conversation_type, display_name,
             service_url, tenant_id, conversation_reference)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (conversation_id) DO UPDATE SET
            conversation_type      = EXCLUDED.conversation_type,
            display_name           = COALESCE(EXCLUDED.display_name,
                                        connector_conversations.display_name),
            service_url            = EXCLUDED.service_url,
            tenant_id              = EXCLUDED.tenant_id,
            conversation_reference = EXCLUDED.conversation_reference,
            last_seen_at           = CURRENT_TIMESTAMP
        """,
        (
            conversation_id,
            conversation_type,
            display_name,
            service_url,
            tenant_id,
            reference_json,
        ),
    )


async def reference_for_conversation(
    conversation_id: str,
) -> Optional[ConversationReference]:
    """The stored reference for a conversation, or None if unknown.

    None means the bot has never seen an activity there and therefore
    cannot post — the containment boundary doing its job, not a bug to
    route around.
    """
    if not is_enabled():
        return None

    row = await _driver.fetchone(
        "SELECT conversation_reference FROM connector_conversations "
        "WHERE conversation_id = ?",
        (conversation_id,),
    )
    if row is None:
        return None
    return ConversationReference().deserialize(json.loads(row[0]))


async def forget_conversation(conversation_id: str) -> bool:
    """Erase everything the connector holds about one conversation.

    The bot is removed from a channel, or an operator is asked to
    delete a record: after this the connector cannot address that
    conversation again until a new activity arrives from it. Exposed
    deliberately, because "we can delete it" is a question every
    review asks and no answer should require raw SQL.
    """
    if not is_enabled():
        return False

    removed = await _driver.execute(
        "DELETE FROM connector_conversations WHERE conversation_id = ?",
        (conversation_id,),
    )
    return bool(removed)


async def list_conversations(limit: int = 100) -> list[ConversationSummary]:
    """Conversations the bot can reach, most recently active first.

    The roster the other end reads to choose where to speak, and
    intentionally the *only* discovery surface: it lists conversations
    the bot was added to, never tenant directory contents, so it cannot
    be used to enumerate the organization.
    """
    if not is_enabled():
        return []

    rows = await _driver.fetchall(
        """
        SELECT conversation_id, conversation_type, display_name
        FROM connector_conversations
        ORDER BY last_seen_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [ConversationSummary(r[0], r[1], r[2]) for r in rows]
