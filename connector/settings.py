"""Connector configuration, read straight from the environment.

WHY THIS EXISTS RATHER THAN REUSING ``server/config.py``

The connector is meant to be lifted out of this repository and
installed on its own. Importing the host application's settings object
would drag in provider keys, Confluence credentials, embedding
dimensions and every other concern of the assessment tool — none of
which the connector has any business knowing.

So it reads its own, small set of variables. The Teams names are
deliberately the *same* ones the host already uses, so an existing
``.env`` keeps working unchanged when the connector runs beside it.

Deliberately plain ``os.environ`` and a mutable dataclass: no
pydantic-settings, no config framework. Tests set attributes directly,
which is the whole ergonomics requirement.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(*names: str, default: str = "") -> str:
    """First non-empty value among ``names``, else ``default``.

    Several settings accept a connector-specific override *and* fall
    back to the host's generic variable, so one ``.env`` can serve
    both when they run together.
    """
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


@dataclass
class ConnectorSettings:
    """Everything the connector needs, and nothing else."""

    # --- Microsoft Teams (Azure Bot Service) ---------------------
    # Shared with the host app on purpose: same bot registration.
    teams_app_id: str = ""
    teams_app_password: str = ""
    teams_tenant_id: str = ""

    # --- Inbound API auth ---------------------------------------
    # Shared secret the other end presents to /api/connector/*.
    # Empty means the API is unavailable (503), never unauthenticated.
    api_token: str = ""

    # --- Storage -------------------------------------------------
    # postgresql://…  or  sqlite:///path/to/connector.db
    # Falls back to the host's DATABASE_URL so nothing changes here.
    database_url: str = ""

    # --- The other end -------------------------------------------
    # One URL. Every inbound Teams message is POSTed there with the
    # conversation id attached, and that is the entire contract.
    #
    # WHY A SINGLE URL AND NOT A CHOICE OF VENDOR ADAPTERS: the systems
    # on the other end address their own work by *session*, and one
    # Teams conversation routinely has many concurrent sessions running
    # against it. Only that system knows which of them a given human
    # reply concerns. So it owns the fan-out, and the connector needs
    # exactly one door to knock on — see ``store/schema.sql``.
    inbound_url: str = ""
    inbound_token: str = ""

    def load_from_env(self) -> "ConnectorSettings":
        """(Re)read every value from the process environment."""
        self.teams_app_id = _env("TEAMS_APP_ID")
        self.teams_app_password = _env("TEAMS_APP_PASSWORD")
        self.teams_tenant_id = _env("TEAMS_TENANT_ID")

        self.api_token = _env("CONNECTOR_API_TOKEN")
        self.database_url = _env("CONNECTOR_DATABASE_URL", "DATABASE_URL")

        self.inbound_url = _env("CONNECTOR_INBOUND_URL")
        self.inbound_token = _env("CONNECTOR_INBOUND_TOKEN")
        return self

    @property
    def forwarding_enabled(self) -> bool:
        """True when inbound messages have somewhere to go.

        Restricted to http(s) so a typo cannot reach a ``file://``
        handler. The URL is operator configuration and never user
        input, so this is a guardrail rather than an SSRF defence.
        """
        return self.inbound_url.startswith(("http://", "https://"))

    @property
    def storage_is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def storage_enabled(self) -> bool:
        return bool(self.database_url)


settings = ConnectorSettings().load_from_env()
