from uuid import uuid4

from psycopg.types.json import Jsonb

MAX_ATTEMPTS = 4
BACKOFF = (5, 15, 45)


async def one(conn, sql, values):
    return await (await conn.execute(sql, values)).fetchone()


async def lock_job(conn, job_id):
    return await one(
        conn,
        """
        SELECT *, clock_timestamp() AS db_now FROM investigation_jobs WHERE id = %s FOR UPDATE
    """,
        (job_id,),
    )


async def claim(pool, message):
    async with pool.connection() as conn, conn.transaction():
        job = await lock_job(conn, message.job_id)
        if not job or job["incident_id"] != message.incident_id:
            return "invalid", None
        if job["state"] in ("SUCCEEDED", "FAILED"):
            return "done", None
        now = job["db_now"]
        if (
            not job["published_at"]
            or job["next_attempt_at"] > now
            or (job["execution_expires_at"] and job["execution_expires_at"] > now)
        ):
            return "wait", None
        if job["attempt_count"] >= MAX_ATTEMPTS:
            await release(conn, job["id"], "FAILED", "LEASE_EXHAUSTED")
            return "done", None
        token = uuid4()
        await conn.execute(
            """
            UPDATE investigation_jobs SET state = 'RUNNING', attempt_count = attempt_count + 1,
                execution_token = %s,
                execution_expires_at = clock_timestamp() + interval '120 seconds',
                updated_at = clock_timestamp() WHERE id = %s
        """,
            (token, job["id"]),
        )
        incident = await one(conn, "SELECT * FROM incidents WHERE id = %s", (message.incident_id,))
        return "run", (incident, token)


def owns(job, token):
    return (
        job
        and job["state"] == "RUNNING"
        and job["execution_token"] == token
        and job["execution_expires_at"] > job["db_now"]
    )


async def release(conn, job_id, state, error=None, delay=0):
    await conn.execute(
        """
        UPDATE investigation_jobs SET state = %s, last_error_code = %s,
            execution_token = NULL, execution_expires_at = NULL,
            next_attempt_at = clock_timestamp() + %s * interval '1 second',
            updated_at = clock_timestamp() WHERE id = %s
    """,
        (state, error, delay, job_id),
    )


async def fail(pool, job_id, token, code, *, retry):
    async with pool.connection() as conn, conn.transaction():
        job = await lock_job(conn, job_id)
        if not owns(job, token):
            return False
        terminal = not retry or job["attempt_count"] >= MAX_ATTEMPTS
        delay = 0 if terminal else BACKOFF[job["attempt_count"] - 1]
        await release(conn, job_id, "FAILED" if terminal else "QUEUED", code, delay)
        return terminal


async def complete(pool, job_id, token, report):
    async with pool.connection() as conn, conn.transaction():
        job = await lock_job(conn, job_id)
        if not owns(job, token):
            return False
        incident_id = job["incident_id"]
        ticket = await one(
            conn, "SELECT status FROM incidents WHERE id = %s FOR UPDATE", (incident_id,)
        )
        # Recheck the lease after waiting for the ticket lock.
        if not owns(await lock_job(conn, job_id), token):
            return False
        if ticket["status"] != "OPEN":
            await release(conn, job_id, "FAILED", "INVALID_TICKET_STATE")
            return True
        await conn.execute(
            """
            INSERT INTO investigation_reports (
                incident_id, suspected_cause, evidence, citations, limitations,
                recommended_yaml_patch, patch_unavailable_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
            (
                incident_id,
                report["suspected_cause"],
                Jsonb(report["evidence"]),
                Jsonb(report["citations"]),
                Jsonb(report["limitations"]),
                report["recommended_yaml_patch"],
                report["patch_unavailable_reason"],
            ),
        )
        await conn.execute(
            """
            UPDATE incidents SET status = 'NEEDS_REVIEW', updated_at = clock_timestamp()
            WHERE id = %s
        """,
            (incident_id,),
        )
        await release(conn, job_id, "SUCCEEDED")
        return True
