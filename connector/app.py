"""Standalone connector service — the whole thing, on its own.

This is the entry point for running the connector as its own web
service, with no host application around it. It is what a deployment on
Azure App Service, Container Apps, or any container platform runs:

    uvicorn connector.app:app --host 0.0.0.0 --port 8000

It mounts exactly two things:

  * ``api.router``     — the outbound surface the other end calls
                         (/threads, /say, /channels, …);
  * ``webhook.router`` — the inbound Teams endpoint Azure Bot Service
                         posts to (/api/messages).

and opens the connector's own store on startup. Nothing from the host
application is imported — that independence is the point, and the same
boundary test that guards the rest of the package guards this file.

The host application, by contrast, imports only ``api.router`` and feeds
inbound messages through its own webhook; it never uses this module. So
the two deployments share all the logic and differ only in wiring.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from connector import store
from connector.api import router as api_router
from connector.settings import settings
from connector.webhook import router as webhook_router

log = logging.getLogger("connector.app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Open the store on the way up, close it on the way down.

    The store applies its own idempotent schema, so a fresh database
    becomes usable with no separate migration step — a standalone
    deployment has no host migration runner to lean on.
    """
    await store.startup()
    if not store.is_enabled():
        # Not fatal: the API answers 503 until a database URL is set, so
        # a misconfigured deploy fails loudly at the first call rather
        # than silently dropping messages. Say so at boot to shorten the
        # diagnosis.
        log.warning(
            "connector store is not enabled — set CONNECTOR_DATABASE_URL "
            "(or DATABASE_URL). The bot cannot record conversations or "
            "reply until it is."
        )
    if not settings.teams_app_id:
        log.warning(
            "TEAMS_APP_ID is unset — the bot cannot authenticate to Teams."
        )
    try:
        yield
    finally:
        await store.shutdown()


app = FastAPI(
    title="Microsoft Teams Connector",
    summary="Bidirectional bridge between Teams and any HTTP endpoint.",
    lifespan=lifespan,
)
app.include_router(api_router)
app.include_router(webhook_router)
