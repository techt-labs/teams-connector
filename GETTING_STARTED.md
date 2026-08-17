# Getting Started — Teams Connector

This guide takes you from zero to a running connector your bot posts
through. **No prior knowledge of this codebase is assumed.** Follow it
top to bottom; every value you must supply says exactly where it comes
from.

What this service is, in one line: *the bridge between Microsoft Teams
and anything that speaks HTTP* — it receives every Teams message at its
webhook, and it posts threads/replies into channels on request.

---

## Step 1 — Collect your Microsoft values

You need three values from your Azure tenant. If an Azure Bot already
exists for this project, read them off it; otherwise create one first
(Azure portal → *Create a resource* → **Azure Bot** → single tenant).

| Variable | Where to get it |
|---|---|
| `TEAMS_APP_ID` | portal.azure.com → your **Azure Bot** → **Configuration** → *Microsoft App ID* |
| `TEAMS_APP_PASSWORD` | Entra ID → **App registrations** → that app → **Certificates & secrets** → *New client secret* → copy the **Value** immediately (shown only once) |
| `TEAMS_TENANT_ID` | Entra ID → **Overview** → *Tenant ID* |

## Step 2 — Choose a database

The connector stores **one table** (see [`DATABASE.md`](DATABASE.md) for
the full annotated DDL — hand it to your DBA if you provision databases
centrally). Two supported options:

- **PostgreSQL** (production): any Postgres 13+ works. On Azure, create
  an **Azure Database for PostgreSQL — Flexible Server**, create an
  empty database, and note the connection string:
  `postgresql://<user>:<password>@<server>.postgres.database.azure.com:5432/<db>`
- **SQLite** (pilot/dev, no database service at all):
  `sqlite:///./connector.db`

You do **not** need to run the DDL yourself — the service applies it at
startup, idempotently. The file exists so your database team can review
or pre-provision it.

## Step 3 — Configure

```bash
cp .env.example .env      # then edit .env
```

| Variable | Set it to |
|---|---|
| `TEAMS_APP_ID` / `TEAMS_APP_PASSWORD` / `TEAMS_TENANT_ID` | the Step-1 values |
| `CONNECTOR_DATABASE_URL` | the Step-2 connection string |
| `CONNECTOR_API_TOKEN` | generate: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` — callers of this connector's API must present it |
| `CONNECTOR_INBOUND_URL` | where inbound Teams messages are forwarded — for the agent bridge, the mcp-server's `https://<mcp-host>/teams-inbound` |
| `CONNECTOR_INBOUND_TOKEN` | generate another token; the receiving service must expect the same one |

Keep every secret in your secret store (Azure Key Vault) in real
deployments; `.env` is the local-file form of the same settings.

## Step 4 — Run

**Docker (recommended — includes Postgres):**

```bash
docker compose up --build
```

**Or directly (your own Python 3.11+):**

```bash
pip install -r requirements.txt
uvicorn connector.app:app --host 0.0.0.0 --port 8000
```

On Azure, the same container runs on **App Service (Web App for
Containers)** or **Container Apps** — both give you the public HTTPS URL
the next step needs.

## Step 5 — Point Microsoft at it

1. The connector must be reachable from the internet over HTTPS
   (App Service URL, or your gateway).
2. Azure portal → your **Azure Bot** → **Configuration** →
   **Messaging endpoint** = `https://<your-host>/api/messages`
3. Upload the Teams app package to the **Teams admin center** and have a
   **team owner** add the app to the channel where questions will be
   asked. (No tenant-admin Graph consent is involved — the team-owner
   install *is* the permission grant.)
4. Post one message in that channel — that first activity is how the
   connector learns the channel exists.

## Step 6 — Verify

```bash
TOKEN=<your CONNECTOR_API_TOKEN>
BASE=https://<your-host>

# alive and credentialed?
curl -H "Authorization: Bearer $TOKEN" $BASE/api/connector/health
#  → {"storage":true,"forwarding_configured":true,"teams_configured":true}

# did Step 5.4 register the channel?
curl -H "Authorization: Bearer $TOKEN" $BASE/api/connector/channels
#  → your channel, by name and id

# can it post? (a real thread appears in the channel)
curl -X POST $BASE/api/connector/threads \
  -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" \
  -d '{"channel_conversation_id":"<id from /channels>","text":"Connector smoke test"}'
```

All three good → the connector is live. The `README.md` documents every
endpoint and the design behind it.

## Step 7 — Next: the MCP server (for the agent experience)

This connector is half of the bridge. To let an **agent** ask humans in
Teams and receive answers back into its session, now deploy the
companion repo —
**[teams-agent-mcp](https://github.com/techt-labs/teams-agent-mcp)** —
following *its* `GETTING_STARTED.md`. It will need two values from this
deployment: this connector's **base URL** and its
**`CONNECTOR_API_TOKEN`**; and you will set this connector's
`CONNECTOR_INBOUND_URL` to point at it. (If you only wanted Teams ↔
your own HTTP endpoint, you are done — point `CONNECTOR_INBOUND_URL` at
your endpoint instead.)

## Troubleshooting the usual first-run failures

| Symptom | Cause → fix |
|---|---|
| Adding the Teams app fails ("something went wrong") | messaging endpoint unreachable or wrong credentials → recheck Step 5.2 and Step 1 values |
| `/channels` empty | the bot never saw a message → Step 5.4 |
| `health` shows `"storage": false` | `CONNECTOR_DATABASE_URL` unset or unreachable |
| `401` on every call | wrong/missing `Authorization: Bearer` token |
