"""SQLite driver for the connector's single table.

WHY SQLITE IS SUPPORTED AT ALL

The connector may be installed somewhere with no managed database
service. Its entire state is one table holding a handful of rows per
active conversation, so a file on disk is a legitimate production
choice here in a way it would not be for the assessment tool.

WHY NO aiosqlite DEPENDENCY

The standard library's ``sqlite3`` plus ``asyncio.to_thread`` covers
this completely. Adding a package to avoid four lines of delegation
would not earn its place in the install.

Writes are serialized behind a lock. With one table and one row per
conversation there is no contention worth optimizing, and serializing
removes a whole class of concurrency bug from a component whose main
virtue is being boring.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional, Sequence

log = logging.getLogger("connector.store.sqlite")


def _path_from_url(url: str) -> str:
    """Extract a filesystem path from a sqlite:// URL.

    Accepts ``sqlite:///relative.db``, ``sqlite:////absolute.db`` and
    the bare ``sqlite://:memory:`` form used by tests.
    """
    rest = url.split("://", 1)[1] if "://" in url else url
    if rest in (":memory:", "/:memory:"):
        return ":memory:"
    # sqlite:///foo.db -> /foo.db -> foo.db (relative)
    # sqlite:////foo.db -> //foo.db -> /foo.db (absolute)
    if rest.startswith("//"):
        return rest[1:]
    return rest.lstrip("/")


class SqliteDriver:
    """Thin async wrapper over a single serialized connection."""

    def __init__(self, url: str) -> None:
        self._path = _path_from_url(url)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._conn is not None:
            return
        if self._path != ":memory:":
            parent = Path(self._path).parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because to_thread may hand work to a
        # different worker; the lock provides the actual serialization.
        self._conn = await asyncio.to_thread(
            sqlite3.connect, self._path, 30.0, 0, None, False
        )

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    def _run(self, sql: str, params: Sequence[Any], mode: str):
        cur = self._conn.execute(sql, tuple(params))
        try:
            if mode == "one":
                return cur.fetchone()
            if mode == "all":
                return list(cur.fetchall())
            self._conn.commit()
            return cur.rowcount
        finally:
            cur.close()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._run, sql, params, "exec")

    async def execute_many_statements(self, script: str) -> None:
        async with self._lock:
            def _script():
                self._conn.executescript(script)
                self._conn.commit()

            await asyncio.to_thread(_script)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[tuple]:
        async with self._lock:
            return await asyncio.to_thread(self._run, sql, params, "one")

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        async with self._lock:
            return await asyncio.to_thread(self._run, sql, params, "all")
