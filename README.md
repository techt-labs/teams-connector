# Microsoft Teams Connector

> **This repo is one of two.** The full bridge that lets an agent ask a
> human in Teams and get the answer back is:
>
> ```
> ┌───────────────────┐   ┌───────────────┐   ┌─────────────────┐   ┌──────────────┐
> │ Microsoft 365      │◄─►│  connector    │◄─►│  MCP server     │◄─►│  your agent  │
> │ Teams + Azure Bot  │   │  ★ THIS REPO  │   │ teams-agent-mcp │   │ SaaS/in-house│
> └───────────────────┘   └───────────────┘   └─────────────────┘   └──────────────┘
> ```
>
> - **Use this repo alone** if you just want Teams ↔ your own HTTP
>   endpoint (receive every message, post threads/replies) — no agent
>   tooling involved.
> - **Use both repos** for the agent experience (`ask_human` tools,
>   reply-to-session correlation): deploy this one **first**, then its
>   companion **[teams-agent-mcp](https://github.com/techt-labs/teams-agent-mcp)**,
>   which is a client of this service.
>
> Deploy order and wiring are in [`GETTING_STARTED.md`](GETTING_STARTED.md).

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

## Why it is this narrow

The connector is developed inside a larger source monorepo whose host
application consumes it — but nothing under `connector/` imports that
host, and a smoke test enforces the boundary mechanically (a boundary
maintained only by good intentions stops being true the first time
someone is in a hurry). This repo is the connector lifted out whole:
attach your own system and it neither knows nor cares.

**Documentation map:** deploy step-by-step →
[`GETTING_STARTED.md`](GETTING_STARTED.md) · the database →
[`DATABASE.md`](DATABASE.md) · the Teams app package →
[`connector/teams_app/README.md`](connector/teams_app/README.md) ·
architecture & APIs → this file.

## Every API, at a glance

**This service EXPOSES 8 endpoints** — 1 for Microsoft, 7 for your
system:

| # | Endpoint | Who calls it | What it does |
|---|---|---|---|
| 1 | `POST /api/messages` | **Azure Bot Service** (JWT-signed) | every Teams activity arrives here — the bot's messaging endpoint |
| 2 | `GET /api/connector/health` | your system / operators | readiness: storage, forwarding, Teams creds |
| 3 | `GET /api/connector/conversations` | your system | every conversation the bot can post into |
| 4 | `GET /api/connector/channels` | your system | just the channels (where new threads can start) |
| 5 | `GET /api/connector/members` | your system | people in a channel, with the ids @-mentions need |
| 6 | `POST /api/connector/threads` | your system | start a new channel thread; returns its id |
| 7 | `POST /api/connector/say` | your system | post a reply into an existing conversation |
| 8 | `POST /api/connector/forget` | your system | erase everything held about one conversation |

Endpoints 2–8 require `Authorization: Bearer <CONNECTOR_API_TOKEN>`.
"Your system" is whatever you attach — the companion
[teams-agent-mcp](https://github.com/techt-labs/teams-agent-mcp) uses
endpoints 4, 5, 6, 7.

**This service CALLS 2 things:**

| Direction | Where | When |
|---|---|---|
| → Microsoft Bot Framework | `login.microsoftonline.com` + the regional `smba.trafficmanager.net` endpoint | posting every thread/reply, reading rosters |
| → `CONNECTOR_INBOUND_URL` | one `POST` of `{conversation_id, text, speaker, speaker_email, source}` | forwarding each inbound human message to your system |

That is the complete surface — nothing else listens, nothing else is
called.

## Using both repos: how this connects to teams-agent-mcp

When this connector pairs with
[teams-agent-mcp](https://github.com/techt-labs/teams-agent-mcp), the
entire integration is **two HTTP links and two shared tokens** — there
is no other coupling (no shared database, no shared code):

```
            OUTBOUND (the agent asks)
   mcp-server ──────────────────────────────► this connector
     calls: GET /api/connector/channels ·  GET /members
            POST /threads               ·  POST /say
     auth:  Authorization: Bearer CONNECTOR_API_TOKEN

            INBOUND (a human answers)
   this connector ──────────────────────────► mcp-server
     calls: POST {CONNECTOR_INBOUND_URL}   (= its /teams-inbound)
     body:  {conversation_id, text, speaker, speaker_email, source}
     auth:  Authorization: Bearer CONNECTOR_INBOUND_TOKEN
```

The four configuration lines that make it work — two on each side:

| Where | Setting | Must be |
|---|---|---|
| **mcp-server** | `CONNECTOR_BASE_URL` | this connector's base URL |
| **mcp-server** | `CONNECTOR_API_TOKEN` | **identical** to this connector's `CONNECTOR_API_TOKEN` |
| **this connector** | `CONNECTOR_INBOUND_URL` | `https://<mcp-host>/teams-inbound` |
| **this connector** | `CONNECTOR_INBOUND_TOKEN` | **identical** to the mcp-server's `MCP_INBOUND_TOKEN` |

Deploy order: this connector first (the mcp-server cannot start its
work without a connector to call), then the mcp-server, then come back
and set `CONNECTOR_INBOUND_URL` here — the loop is closed at that
moment. If either token pair mismatches, the symptom is a 401 in the
callee's log; if `CONNECTOR_INBOUND_URL` is empty, outbound asking
still works but every human reply is received and dropped here.

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

**By code**, when your handler runs in the same process (for a host
application that embeds the package):

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
| `GET`  | `/api/connector/members` | People in a channel (ids for mentions) |
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

One table, `connector_conversations` — the full annotated DDL, column
meanings, sizing and retention notes are in
[`DATABASE.md`](DATABASE.md), written to be handed to a database team.
The DDL itself (`connector/store/schema.sql`) is
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

## Running it

Deployment step-by-step (including every Azure value and where it comes
from) lives in [`GETTING_STARTED.md`](GETTING_STARTED.md); the database
DDL and its reasoning in [`DATABASE.md`](DATABASE.md). The short form:

```bash
cp .env.example .env      # fill in — see GETTING_STARTED.md
docker compose up --build # service + PostgreSQL, one command
```

or without Docker (Python 3.11+):

```bash
pip install -r requirements.txt
uvicorn connector.app:app --host 0.0.0.0 --port 8000
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

The `Dockerfile` builds the same image for any container platform:

```bash
docker build -t teams-connector .
docker run -p 8000:8000 --env-file .env teams-connector
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

This package is developed in a source monorepo whose smoke suite runs
on every change: the liftability boundary (no host imports, verified in
a bare subprocess), fail-closed auth, thread creation, mention
rendering, and store round-trips on both SQLite and PostgreSQL. Per-repo
CI is on the roadmap; until then the monorepo suite is the contract.

Two things cannot be proven without Microsoft: that a bot with no
tenant-admin consent can create a channel thread, and that the id
returned at creation is the id replies carry. Both have been verified
against a real tenant. To re-verify in *yours*, run the three curl
checks in [`GETTING_STARTED.md`](GETTING_STARTED.md) Step 6 and reply
to the smoke-test thread.
