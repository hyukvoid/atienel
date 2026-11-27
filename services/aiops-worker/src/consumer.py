import asyncio
import logging
from time import monotonic

import psycopg
from atienel_shared.contracts import STREAM, StreamMessage
from pydantic import ValidationError
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from valkey.exceptions import ResponseError, ValkeyError

from .job_repository import claim, fail

GROUP = "aiops-workers-v1"
INVALID_STREAM = "aiops:incidents:invalid:v1"
log = logging.getLogger(__name__)


async def ensure_group(queue):
    try:
        await queue.xgroup_create(STREAM, GROUP, id="0-0", mkstream=True)
    except ResponseError as exc:
        if not str(exc).startswith("BUSYGROUP"):
            raise


async def settle(queue, entry_id, error=None):
    if error:
        await queue.xadd(INVALID_STREAM, {"entry_id": entry_id, "error_code": error})
    await queue.xack(STREAM, GROUP, entry_id)
    await queue.xdel(STREAM, entry_id)


async def handle(pool, queue, graph, entry_id, fields):
    try:
        payload = fields.get("payload")
        if set(fields) != {"payload"} or not payload or len(payload.encode()) > 2048:
            raise ValueError("Invalid stream envelope")
        message = StreamMessage.model_validate_json(payload)
    except (ValueError, UnicodeError):
        await settle(queue, entry_id, "INVALID_PAYLOAD")
        return
    state, lease = await claim(pool, message)
    if state == "invalid":
        await settle(queue, entry_id, "UNKNOWN_JOB")
    if state == "done":
        await settle(queue, entry_id)
    if state != "run":
        return

    incident, token = lease
    try:
        async with asyncio.timeout(90):
            result = await graph.ainvoke(
                {
                    "incident": incident,
                    "job_id": message.job_id,
                    "token": token,
                }
            )
        settled = result["committed"]
        if settled:
            log.info(
                "Investigation committed incident_id=%s job_id=%s",
                message.incident_id,
                message.job_id,
            )
    except Exception as exc:
        permanent = isinstance(
            exc, (ValidationError, ValueError, KeyError, TypeError, UnexpectedModelBehavior)
        )
        if isinstance(exc, ModelHTTPError):
            permanent = 400 <= exc.status_code < 500 and exc.status_code not in (408, 429)
        log.warning(
            "Investigation failed job_id=%s category=%s provider_status=%s",
            message.job_id,
            type(exc).__name__,
            exc.status_code if isinstance(exc, ModelHTTPError) else None,
        )
        settled = await fail(
            pool,
            message.job_id,
            token,
            "INVALID_DIAGNOSIS" if permanent else "INVESTIGATION_ERROR",
            retry=not permanent,
        )
    if settled:
        await settle(queue, entry_id)


async def consume(pool, queue, graph, consumer, stop):
    cursor, next_claim = "0-0", 0.0
    while not stop.is_set():
        try:
            await ensure_group(queue)
            while not stop.is_set():
                if monotonic() >= next_claim:
                    cursor, entries, *_ = await queue.xautoclaim(
                        STREAM, GROUP, consumer, 10000, start_id=cursor, count=10
                    )
                    next_claim = monotonic() + 5
                    for entry_id, fields in entries:
                        if stop.is_set():
                            return
                        await handle(pool, queue, graph, entry_id, fields)
                if stop.is_set():
                    return
                batches = await queue.xreadgroup(
                    GROUP,
                    consumer,
                    {STREAM: ">"},
                    count=1,
                    block=2000,
                )
                for _, entries in batches:
                    for entry_id, fields in entries:
                        if stop.is_set():
                            return
                        await handle(pool, queue, graph, entry_id, fields)
        except (psycopg.Error, ValkeyError, TimeoutError) as exc:
            log.warning("Consumer reconnecting (%s)", type(exc).__name__)
            await asyncio.sleep(1)


async def queue_metrics(pool, queue, stop):
    while not stop.is_set():
        try:
            pending = await queue.xpending(STREAM, GROUP)
            old = await queue.xpending_range(STREAM, GROUP, "-", "+", 1, idle=10000)
            async with pool.connection() as conn:
                cursor = await conn.execute("""
                    SELECT count(*) FILTER (
                        WHERE published_at IS NULL AND state = 'QUEUED'
                    ) AS unpublished, count(*) FILTER (WHERE state = 'FAILED') AS failed
                    FROM investigation_jobs
                """)
                jobs = await cursor.fetchone()
            log.info(
                "queue_metrics stream_length=%d pending=%d old_pending=%d unpublished=%d failed=%d",
                await queue.xlen(STREAM),
                pending["pending"],
                bool(old),
                jobs["unpublished"],
                jobs["failed"],
            )
        except (psycopg.Error, ValkeyError, TimeoutError) as exc:
            log.warning("Queue metrics unavailable (%s)", type(exc).__name__)
        try:
            async with asyncio.timeout(30):
                await stop.wait()
        except TimeoutError:
            pass
