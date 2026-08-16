-- The Teams connector: one table, and it is only an address book.
--
-- This file is valid PostgreSQL and can be handed to a DBA as-is. For
-- SQLite the loader rewrites TIMESTAMPTZ to TEXT and nothing else —
-- every other construct here is common to both engines.
--
-- WHAT THIS TABLE IS
--
-- One fact per row: *how to reach a Teams conversation.* Posting into
-- a channel or chat needs a Bot Framework conversation reference, and
-- that reference has to survive across days, restarts and deploys.
-- There is no way to obtain it on demand — it can only be captured
-- when an activity arrives. So it has to be stored, and this is the
-- whole reason the connector has a database at all.
--
-- Rows appear only when an activity reaches us. That gives the
-- containment property which makes this defensible to a security
-- reviewer: the connector can address exactly the conversations the
-- bot was added to, and no directory call can widen the set.
--
-- WHAT THIS TABLE DELIBERATELY DOES NOT HOLD
--
-- No session ids. No bindings. No correlation between a Teams
-- conversation and whatever is running on the other end.
--
-- That was an earlier design and it was wrong. The system on the other
-- end routinely runs *many* concurrent sessions against the *same*
-- Teams channel — one per initiative, per task, per assessment. The
-- relationship is one-to-many and it changes constantly. Modelling it
-- here would mean the connector owning a lifecycle it cannot see:
-- which session is still interested, which has finished, which of
-- three sessions a given human reply was meant for.
--
-- The other end already knows all of that. It created the sessions. So
-- correlation lives there, and the connector stays an address book:
-- it delivers outbound text to a conversation, and forwards inbound
-- text with the conversation id attached. Who cares about that
-- conversation is not its question.
--
-- PORTABILITY
--
-- Plain SQL on purpose: no extensions, no pgvector, no JSONB
-- operators. The reference is stored as TEXT holding JSON because the
-- connector only ever round-trips it and never queries inside it. That
-- keeps the ask to a platform team as small as "one table" — or no
-- database service at all, since this runs on SQLite.

CREATE TABLE IF NOT EXISTS connector_conversations (
    -- Bot Framework conversation id. Opaque, tenant-scoped, stable for
    -- the life of the channel or chat. This is the only identifier the
    -- connector ever hands to the other end.
    conversation_id TEXT PRIMARY KEY,

    -- "personal" | "channel" | "groupChat". Advisory: used to render a
    -- useful roster, never for routing.
    conversation_type TEXT,

    -- Human-readable label (team/channel name, or chat topic). Teams
    -- does not always supply one; NULL is normal, not an error.
    display_name    TEXT,

    -- Regional Bot Framework endpoint. Replayed verbatim — the global
    -- URL happens to work today but is not guaranteed to.
    service_url     TEXT,
    tenant_id       TEXT,

    -- Serialized ConversationReference as JSON text. Stored whole
    -- because the SDK adds fields between versions, and a reference
    -- that round-trips exactly is worth more than one we fully model.
    conversation_reference TEXT NOT NULL,

    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
