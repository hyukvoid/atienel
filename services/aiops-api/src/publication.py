import asyncio

from atienel_shared.contracts import STREAM, Accepted, StreamMessage
from psycopg.types.json import Jsonb

IO_TIMEOUT = 0.15


async def append(conn, queue, job):
    message = StreamMessage(schema_version=1, job_id=job["id"], incident_id=job["incident_id"])
    async with asyncio.timeout(IO_TIMEOUT):
        entry = await queue.xadd(STREAM, {"payload": message.model_dump_json()})
    await conn.execute(
        """
        UPDATE investigation_jobs
        SET stream_entry_id = %s, published_at = now(), updated_at = now()
        WHERE id = %s
        """,
        (entry, job["id"]),
    )
    return entry


async def publish(pool, queue, incident_id, principal, key):
    body = Accepted(incident_id=incident_id).model_dump(mode="json")
    location = f"/incidents/{incident_id}"
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '150ms'")
        cursor = await conn.execute(
            "SELECT * FROM investigation_jobs WHERE incident_id = %s FOR UPDATE",
            (incident_id,),
        )
        job = await cursor.fetchone()
        if job["published_at"] is None:
            await append(conn, queue, job)
        await conn.execute(
            """
            UPDATE api_idempotency SET response_status = 202, response_body = %s,
                response_location = %s
            WHERE principal_id = %s AND operation = 'create_incident' AND idempotency_key = %s
            """,
            (Jsonb(body), location, principal, key),
        )
    return body, location
