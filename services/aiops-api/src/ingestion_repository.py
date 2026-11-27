from psycopg.types.json import Jsonb

IDENTITY_FIELDS = ("namespace", "pod_name", "pod_uid", "container_name", "incident_type")


class Conflict(Exception):
    pass


async def reserve(pool, principal, key, event):
    request_hash = event.request_hash()
    scope = (principal, key)
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO api_idempotency (principal_id, operation, idempotency_key, request_hash)
            VALUES (%s, 'create_incident', %s, %s) ON CONFLICT DO NOTHING
            """,
            (*scope, request_hash),
        )
        cursor = await conn.execute(
            """
            SELECT * FROM api_idempotency
            WHERE principal_id = %s AND operation = 'create_incident' AND idempotency_key = %s
            FOR UPDATE
            """,
            scope,
        )
        reservation = await cursor.fetchone()
        if reservation["request_hash"] != request_hash:
            raise Conflict("IDEMPOTENCY_CONFLICT")
        if reservation["incident_id"]:
            return reservation["incident_id"], reservation

        await conn.execute(
            """
            INSERT INTO incidents
                (event_id, namespace, pod_name, pod_uid, container_name,
                 incident_type, observed_at, signal)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                event.event_id,
                *(getattr(event, name) for name in IDENTITY_FIELDS),
                event.observed_at,
                Jsonb({"source": event.source, "summary": event.summary}),
            ),
        )
        cursor = await conn.execute(
            "SELECT * FROM incidents WHERE event_id = %s", (event.event_id,)
        )
        incident = await cursor.fetchone()
        if any(incident[field] != getattr(event, field) for field in IDENTITY_FIELDS):
            raise Conflict("EVENT_CONFLICT")
        incident_id = incident["id"]
        await conn.execute(
            "INSERT INTO investigation_jobs (incident_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (incident_id,),
        )
        await conn.execute(
            """
            UPDATE api_idempotency SET incident_id = %s
            WHERE principal_id = %s AND operation = 'create_incident' AND idempotency_key = %s
            """,
            (incident_id, *scope),
        )
        return incident_id, None


async def lookup(pool, incident_id):
    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT i.id AS incident_id, i.event_id, i.namespace, i.pod_name, i.pod_uid,
                   i.container_name, i.incident_type, i.observed_at, i.status,
                   to_jsonb(r) - 'incident_id' - 'created_at' AS report,
                   i.approved_by, i.approved_at
            FROM incidents i LEFT JOIN investigation_reports r ON r.incident_id = i.id
            WHERE i.id = %s
            """,
            (incident_id,),
        )
        row = await cursor.fetchone()
        if row:
            approved_at = row.pop("approved_at")
            approved_by = row.pop("approved_by")
            row["approval"] = (
                {"approved_by": approved_by, "approved_at": approved_at} if approved_at else None
            )
        return row
