# Microsoft Teams Connector

A small, self-contained service that connects Microsoft Teams to
whatever system you want on the other end.

```
   Microsoft Teams   <->   [ connector ]   <->   your system
   (Azure Bot Service)      one table            - a homegrown assistant
                            pass-through         - a vendor agent platform
                                                 - any HTTP endpoint
```

Teams integration is the tedious part: registering an Azure Bot Service
app, getting the manifest and resource-specific consent right, capturing
a conversation reference that still works days later, and posting back
into a channel long after the original request ended. This package does
that, and deliberately nothing else.

## Why it is a separate component

The repository around it ships a homegrown assessment assistant that
uses this connector. That assistant is a *reference implementation*, not
a dependency. Nothing under `connector/` imports it, and a smoke test
enforces that mechanically — a boundary maintained only by good
intentions stops being true the first time someone is in a hurry.

So you can take this directory, drop it into your own service, and
attach your own tool. That is the intended use.

## What it does not do

No language model runs here. The connector does not decide what to say,
does not parse message content, and does not correlate Teams
conversations with sessions on your side.

That last one is worth being explicit about, because it is the easy
mistake. Systems on the other end typically run **many concurrent
sessions against the same Teams channel** — one per initiative, per
task, per assessment — and only they know which of those are still
listening. If the connector tried to hold that mapping it would be
guessing at a lifecycle it cannot see. So it doesn't:

- **Outbound**: you post to a `conversation_id`.
- **Inbound**: every message is forwarded with the `conversation_id` it
  came from.

Keeping session ↔ conversation on your side is a few lines there, and
it is the only place the answer actually exists.

That narrowness is the security argument, not modesty: there is no
prompt to inject and no model output to trust. It is also why the
storage requirement is one table rather than a vector database.

## Attaching your system

**By configuration.** Point it at an HTTP endpoint you control:

```bash
CONNECTOR_INBOUND_URL=https://your-tool.internal/teams-inbound
CONNECTOR_INBOUND_TOKEN=...      # optional bearer, sent to your endpoint
```

Every inbound Teams message arrives there as:

```json
{"conversation_id": "19:...@thread.tacv2",
 "text": "[Teams] Ann:\nthe answer is 42",
 "speaker": "Ann", "speaker_email": "ann@example.com",
 "source": "microsoft-teams"}
```

Any 2xx means accepted. Your endpoint looks up which of its sessions
care about that conversation and wakes them. To speak back, call
`POST /api/connector/say` with the same `conversation_id`.

**By code**, when your handler runs in the same process — this is how
the assistant in this repository attaches:

```python
import connector
connector.set_fallback_handler(my_handler)   # async, returns True if handled
```

The fallback is consulted only when `CONNECTOR_INBOUND_URL` is unset:
operator configuration outranks a compiled-in default.

## HTTP API

Every endpoint requires `Authorization: Bearer $CONNECTOR_API_TOKEN`.
If that variable is unset the API returns **503**, never an
unauthenticated success — an open relay would let anyone post into your
tenant as the bot.

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/connector/health` | Storage, forwarding and Teams readiness |
| `GET`  | `/api/connector/conversations` | Conversations the bot can post into |
| `GET`  | `/api/connector/channels` | Channels a new thread can start in |
| `POST` | `/api/connector/threads` | Start a thread; returns its address |
| `POST` | `/api/connector/say` | Post text into a conversation |
| `POST` | `/api/connector/forget` | Erase one conversation |

`/conversations` and `/channels` are the only discovery surfaces, and
they list places the bot was actually added — never tenant directory
contents. They cannot be used to enumerate your organization.
`/channels` is simply `/conversations` filtered to channels, because
only a channel can host a thread.

Channels are discovered at **install time**, not by enumeration: the
moment a team owner adds the bot, Teams sends a `conversationUpdate`
and the row is written with the correct regional service URL. Graph's
`get_team_channels` would list channels the bot was *not* added to,
which is exactly the containment property above — so it is not used.

## Starting a conversation

A system that only replies can never *ask*. `POST /threads` is the
other half: it starts a new thread in a channel and returns the id
Teams will stamp on every reply inside it.

```json
POST /api/connector/threads
{"channel_conversation_id": "19:...@thread.tacv2",
 "text": "Quick question about the eligibility rules — <at>Ann</at>?",
 "mentions": [{"aad_object_id": "...", "name": "Ann", "email": "ann@example.com"}]}

-> {"conversation_id": "19:...@thread.tacv2;messageid=1700000000000"}
```

Store that `conversation_id` against whatever unit of work asked the
question. When a human replies, it arrives on the inbound side carrying
the same id, so routing the answer back is a lookup rather than an
inference — that is the whole reason threads are used instead of loose
channel posts.

**One thread per piece of work, not per message.** Follow-ups go
through `/say` with the id returned here, so a twelve-question exchange
stays one readable conversation instead of twelve scattered posts.

**Mentions are not decoration.** A literal `@Ann` typed into `text` is
grey characters that notify nobody. Teams needs both the markup and a
matching entity bound to an AAD object id, which is what `mentions`
produces. Resolving a person to that id needs directory access the
connector deliberately does not hold, so the caller supplies it —
already resolved.

Two implementation notes for anyone reading the source and wondering:

- `mentions.py` duplicates pure-text logic that also exists in the host
  application. That duplication is the price of this directory being
  liftable; a drift test pins the two implementations byte-for-byte.
- `TeamsInfo.send_message_to_teams_channel` is *not* used, despite being
  the obvious call. Both of its branches are broken against a legacy
  `BotFrameworkAdapter`, so `teams_threads.py` calls
  `adapter.create_conversation` directly and captures the result through
  a closure. The module docstring explains why in full.

Posting into a channel relies on the resource-specific
`ChannelMessage.Send.Group` consent declared in the app manifest, which
a **team owner** grants by installing the app. No tenant-wide admin
grant is involved — that is why channel threads are used in preference
to group chats, which do require one.

## Storage

One table, `connector_conversations`. The DDL in `store/schema.sql` is
valid PostgreSQL and can be handed to a DBA as-is; no extensions, no
pgvector, no JSONB.

```bash
CONNECTOR_DATABASE_URL=postgresql://user@host:5432/db   # or…
CONNECTOR_DATABASE_URL=sqlite:///./connector.db          # no DB service at all
```

SQLite is a legitimate production choice here: the entire state is one
row per conversation the bot has been added to. If your platform team
cannot provision Postgres, this removes the blocker.

The table holds exactly one fact — how to reach a Teams conversation.
It exists because that reference cannot be fetched on demand; it can
only be captured when an activity arrives, and it has to survive
restarts. Rows appear only when the bot is spoken to, which is the
containment property: it can address exactly the conversations it was
added to, and no call can widen that set.

## Running it as its own service

The connector deploys on its own, with no host application. `app.py` is
the entry point and mounts both halves — the outbound API and the
inbound Teams webhook:

```bash
pip install -r requirements.txt
uvicorn connector.app:app --host 0.0.0.0 --port 8000    # from server/
```

Two endpoints face outward:

| Path | Who calls it | Purpose |
|---|---|---|
| `/api/messages` | Azure Bot Service | inbound — Teams messages arrive here |
| `/api/connector/*` | the attached system | outbound — start threads, post replies |

Point the **Azure Bot registration's messaging endpoint** at
`https://<your-host>/api/messages`. That host is the one piece that
differs by environment: locally it is an [ngrok](https://ngrok.com)
tunnel to `localhost`; in production it is a real hosted URL your
platform owns. Nothing in the code changes between the two — ngrok is
only a stand-in for a public address during testing, never a runtime
dependency.

### Container

The `Dockerfile` builds the same image for any container platform.
Build context is `server/` because the package imports itself
absolutely:

```bash
docker build -f connector/Dockerfile -t teams-connector server/
docker run -p 8000:8000 --env-file connector/.env teams-connector
```

On **Azure App Service (Web App for Containers)** or **Azure Container
Apps**, the platform supplies the public HTTPS URL and its managed
certificate — that URL is the messaging endpoint. Scale horizontally by
running more containers: each is stateless, all shared state lives in
the one database, so instances never coordinate. Supply the secret
settings (`TEAMS_APP_PASSWORD`, `CONNECTOR_API_TOKEN`,
`CONNECTOR_INBOUND_TOKEN`) as Key Vault references, not baked into the
image. See `.env.example` for the full list.

## Files in this package

You do not edit these to deploy — deployment is configuration only (see
below). This is a map for anyone reading or reviewing the code.

| File | What it does |
|---|---|
| `app.py` | **Entry point.** Starts the standalone service, mounts the API + inbound webhook, opens the store. |
| `webhook.py` | Inbound endpoint (`/api/messages`) — receives Teams messages from Azure Bot, validates the JWT, forwards them on. |
| `api.py` | Outbound HTTP API (`/api/connector/*`) — start threads, post replies, list channels/members, forget. |
| `teams_threads.py` | Starts a new channel thread (the "ask" primitive). |
| `teams_send.py` | Posts a reply into an existing conversation. |
| `teams_members.py` | Reads a channel's member roster (resolves names → object ids for mentions). |
| `mentions.py` | Turns resolved people into Teams @-mention markup. |
| `inbound.py` | Forwards a received message to the attached endpoint (`CONNECTOR_INBOUND_URL`). |
| `settings.py` | Reads all configuration from the environment. **Where config comes in.** |
| `store/` | The one-table conversation store (Postgres or SQLite). |
| `.env.example` | **The config file you copy to `.env` and fill in.** |
| `Dockerfile` | Builds the container image. |

## Configuration reference

| Variable | Purpose |
|---|---|
| `TEAMS_APP_ID` / `TEAMS_APP_PASSWORD` / `TEAMS_TENANT_ID` | Azure Bot Service registration |
| `CONNECTOR_API_TOKEN` | Shared secret for `/api/connector/*`; unset = disabled |
| `CONNECTOR_DATABASE_URL` | Postgres or SQLite; falls back to `DATABASE_URL` |
| `CONNECTOR_INBOUND_URL` | Where inbound Teams messages are POSTed |
| `CONNECTOR_INBOUND_TOKEN` | Optional bearer sent to that URL |

## Tests

```bash
python3 tests/test_phase17_connector_smoke.py       # boundary + fails closed
python3 tests/test_phase18_channel_thread_smoke.py  # threads + mentions
```

Both run against SQLite with no setup. Set `DATABASE_URL` to also
exercise PostgreSQL; the tests remove every row they create.

Two things cannot be proven without Microsoft: that a bot with no admin
consent can create a channel thread, and that the id returned at
creation is the id replies carry. A live check covers both and needs a
channel, a human, and a few minutes:

```bash
CONNECTOR_API_TOKEN=... python3 scripts/teams_channel_thread_check.py \
    --server http://localhost:8000 \
    --mention '<aad-object-id>:Ann Lee:ann@example.com'
```

It listens for the forwarded reply itself, so point
`CONNECTOR_INBOUND_URL` at it first. Exits non-zero on any mismatch.
