# Database — Teams Connector

Everything the connector persists, for whoever owns databases in your
organization. **One table. No extensions, no vector types, no JSONB.**
Any PostgreSQL 13+ works — including **Azure Database for PostgreSQL
(Flexible Server)** — and SQLite is supported for pilots.

## Do I have to run this DDL?

No. The service applies it at **startup, idempotently**
(`CREATE TABLE IF NOT EXISTS`), against whatever
`CONNECTOR_DATABASE_URL` points at. This document exists so a DBA can
review it, pre-provision it, or manage it with their own migration
tooling — all three are fine; the startup apply is a no-op on an
already-provisioned database.

## The DDL

The authoritative copy is [`connector/store/schema.sql`](connector/store/schema.sql)
(the service loads that exact file). Reproduced here for review:

```sql
CREATE TABLE IF NOT EXISTS connector_conversations (
    conversation_id        TEXT PRIMARY KEY,
    conversation_type      TEXT,
    display_name           TEXT,
    service_url            TEXT,
    tenant_id              TEXT,
    conversation_reference TEXT NOT NULL,
    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

*(If the copy above and `schema.sql` ever differ, `schema.sql` wins —
it is the file the service executes.)*

## What each column is

| Column | Meaning |
|---|---|
| `conversation_id` | The Teams conversation address — a channel root (`19:…@thread.tacv2`), a thread (`…;messageid=<n>`), or a 1:1 chat id. Primary key: sends look this up point-wise. |
| `conversation_type` | `channel`, `groupChat`, or `personal` — drives the `/channels` filter. |
| `display_name` | Channel/chat name when Teams supplies one (listing UX only). |
| `service_url` | The regional Bot Framework endpoint this conversation answers on. Captured, never assumed — Teams rotates regions. |
| `tenant_id` | The M365 tenant the conversation lives in. |
| `conversation_reference` | The serialized Bot Framework reference (JSON as text) required to post later. Stored verbatim, never queried inside — which is what keeps this portable across databases. |
| `first_seen_at` / `last_seen_at` | Audit + "newest first" ordering. |

## Sizing and access patterns

- **One row per conversation the bot was added to** — tens to thousands
  of rows, not millions. Growth is bounded by real installs, not by
  message volume (messages are *not* stored here).
- Reads: point lookup by primary key on every outbound send; a
  `LIMIT`-ed scan ordered by `last_seen_at` for listings.
- Writes: one upsert per inbound activity (`ON CONFLICT … DO UPDATE`).
- No additional indexes are required at this scale; the primary key
  covers the hot path.

## Retention / deletion

`POST /api/connector/forget` deletes a conversation's row — the
API-level answer to "can you delete what you hold about X?". There is
no other personal data in this store: no message content, no user
directory, no tokens.

## SQLite note

Set `CONNECTOR_DATABASE_URL=sqlite:///./connector.db` and the service
substitutes `TEXT` for `TIMESTAMPTZ` at apply time. Everything else is
identical. Suitable for a pilot; use PostgreSQL when more than one
container instance runs.
