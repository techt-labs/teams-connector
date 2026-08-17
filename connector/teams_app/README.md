# Building the Teams app package

Teams installs bots from an **app package**: a zip containing a
manifest and two icons. This folder gives you the manifest template;
here is the whole procedure.

## 1. Fill the template

Copy `manifest.template.json` to `manifest.json` and replace every
`${…}` placeholder:

| Placeholder | Value |
|---|---|
| `${TEAMS_APP_ID}` | your Azure Bot's **Microsoft App ID** (same value in all three places it appears) |
| `${YOUR_APP_NAME}` | the name users see in Teams (e.g. "Acme Agent Bridge") |
| `${YOUR_ORG_NAME}` | your organization |
| `${YOUR_CONNECTOR_HOST}` | the connector's public host (for the info links) |

Do not remove the `authorization.permissions.resourceSpecific` block —
those four **resource-specific consent (RSC)** permissions are what let
the bot read/post in a channel *of teams it is installed into*, granted
by the **team owner at install time**. This is the whole reason no
tenant-admin Graph consent is needed:

| Permission | Why the connector needs it |
|---|---|
| `ChannelMessage.Read.Group` | receive channel replies without requiring an @-mention on every message |
| `ChannelMessage.Send.Group` | post threads and replies into the channel |
| `TeamMember.Read.Group` | resolve a person's name to their id so @-mentions actually notify |
| `ChatMessage.Read.Chat` | receive messages in chats the bot is added to |

## 2. Add two icons

- `color.png` — 192×192, full-color app icon
- `outline.png` — 32×32, white-on-transparent outline

Any PNGs of those exact sizes work; your branding team likely has them.

## 3. Zip exactly three files

```bash
zip app-package.zip manifest.json color.png outline.png
```

Flat zip — the three files at the top level, no folder inside.

## 4. Upload

Teams admin center (admin.teams.microsoft.com) → **Teams apps →
Manage apps → Upload new app** → pick `app-package.zip`. Scope its
availability (e.g. to a security group) if your org requires it. Then a
**team owner** opens Teams → the target team → *Manage team → Apps →
Add* — that install is the RSC grant.

Common upload rejections: wrong icon sizes, a `${…}` placeholder left
unreplaced, or the zip containing a folder instead of the three files
directly.
