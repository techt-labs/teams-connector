"""PostgreSQL driver for the connector's single table.

Owns its own connection pool rather than borrowing the host
application's. That independence is the point: the connector has to
run in a process where no host application exists.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("connector.store.postgres")


class PostgresDriver:
    """Thin async wrapper: enough SQL surface for one table."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Optional[AsyncConnectionPool] = None

    # Statements are authored with '?' placeholders so one set of SQL
    # serves both engines. Safe here because none of the connector's
    # statements contain a literal '?'.
    @staticmethod
    def _translate(sql: str) -> str:
        return sql.replace("?", "%s")

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = AsyncConnectionPool(self._dsn, min_size=1, max_size=4, open=False)
            await self._pool.open()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _cursor(self, conn):
        return conn.cursor()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._translate(sql), tuple(params))
                return cur.rowcount

    async def execute_many_statements(self, script: str) -> None:
        """Run a multi-statement DDL script."""
        async with self._pool.connection() as conn:
            await conn.execute(script)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[tuple]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._translate(sql), tuple(params))
                return await cur.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._translate(sql), tuple(params))
                return list(await cur.fetchall())
