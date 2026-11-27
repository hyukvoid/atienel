import asyncio
import logging

import psycopg
from atienel_shared.contracts import STREAM
from valkey.exceptions import ValkeyError

from .connections import connections
from .publication import IO_TIMEOUT, append

log = logging.getLogger(__name__)


async def publish_pending(pool, queue):
    async with pool.connection() as conn, conn.transaction():
        cursor = await conn.execute(
            """
            SELECT * FROM investigation_jobs
            WHERE published_at IS NULL AND state = 'QUEUED'
            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
            """
        )
        job = await cursor.fetchone()
        if job:
            await append(conn, queue, job)
        return job is not None


async def reconcile_entry(pool, queue, after):
    async with pool.connection() as conn, conn.transaction():
        cursor = await conn.execute(
            """
            SELECT * FROM investigation_jobs
            WHERE published_at IS NOT NULL AND (%s::uuid IS NULL OR id > %s)
                AND (state = 'QUEUED' OR (state = 'RUNNING' AND execution_expires_at <= now()))
            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
            """,
            (after, after),
        )
        job = await cursor.fetchone()
        if job is None:
            return None
        entry = job["stream_entry_id"]
        async with asyncio.timeout(IO_TIMEOUT):
            entries = await queue.xrange(STREAM, min=entry, max=entry, count=1)
        if not entries:
            await conn.execute(
                """
                UPDATE investigation_jobs SET state = 'QUEUED', execution_token = NULL,
                    execution_expires_at = NULL WHERE id = %s
                """,
                (job["id"],),
            )
            await append(conn, queue, job)
        return job["id"]


async def main():
    logging.basicConfig(level=logging.INFO)
    pool, queue = connections()
    async with pool, queue:
        after = None
        while True:
            try:
                for _ in range(20):
                    if not await publish_pending(pool, queue):
                        break
                for _ in range(20):
                    after = await reconcile_entry(pool, queue, after)
                    if after is None:
                        break
            except (psycopg.Error, ValkeyError, TimeoutError) as exc:
                log.warning("Publication recovery deferred (%s)", type(exc).__name__)
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
