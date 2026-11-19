from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import aiomysql


class NewsDatabase:
    def __init__(self, settings, *, max_size: int = 6):
        self.settings = settings
        self.max_size = max_size
        self.pool = None
        self.slots = asyncio.Semaphore(max_size)

    async def start(self):
        settings = self.settings
        self.pool = await aiomysql.create_pool(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            db=settings.database,
            charset=settings.charset,
            minsize=0,
            maxsize=self.max_size,
            pool_recycle=180,
            connect_timeout=4,
            autocommit=False,
            init_command="SET SESSION transaction_read_only = 1, time_zone = '+00:00'",
        )

    @asynccontextmanager
    async def cursor(self):
        pool = self.pool
        if pool is None:
            raise RuntimeError("News database pool has not been started")
        async with asyncio.timeout(6):
            await self.slots.acquire()
        try:
            async with asyncio.timeout(5):
                connection = await pool.acquire()
            try:
                async with asyncio.timeout(8):
                    async with connection.cursor(aiomysql.DictCursor) as cursor:
                        yield cursor
                    await connection.rollback()
            except BaseException:
                # An interrupted connection can still have unread MySQL packets.
                connection.close()
                raise
            finally:
                await pool.release(connection)
        finally:
            self.slots.release()

    async def close(self):
        if self.pool is not None:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None
