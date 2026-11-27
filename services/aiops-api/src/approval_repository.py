import hashlib
import json
from datetime import UTC

from psycopg.types.json import Jsonb

from .ingestion_repository import Conflict


async def approve(pool, principal, key, incident_id):
    scope = (principal, key)
    digest = hashlib.sha256(
        json.dumps({"incident_id": str(incident_id)}, separators=(",", ":")).encode()
    ).digest()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO api_idempotency (principal_id, operation, idempotency_key, request_hash)
            VALUES (%s, 'approve_incident', %s, %s) ON CONFLICT DO NOTHING
        """,
            (*scope, digest),
        )
        snapshot = await (
            await conn.execute(
                """
            SELECT request_hash, response_body FROM api_idempotency
            WHERE principal_id = %s AND operation = 'approve_incident' AND idempotency_key = %s
            FOR UPDATE
        """,
                scope,
            )
        ).fetchone()
        if snapshot["request_hash"] != digest:
            raise Conflict("IDEMPOTENCY_CONFLICT")
        if snapshot["response_body"] is not None:
            return snapshot["response_body"]

        ticket = await (
            await conn.execute(
                """
            SELECT status, approved_by, approved_at FROM incidents WHERE id = %s FOR UPDATE
        """,
                (incident_id,),
            )
        ).fetchone()
        if ticket is None:
            raise LookupError
        report = await (
            await conn.execute(
                """
            SELECT id FROM investigation_reports WHERE incident_id = %s
        """,
                (incident_id,),
            )
        ).fetchone()
        if not report or ticket["status"] == "OPEN":
            raise Conflict("INVALID_TRANSITION")
        if ticket["status"] == "APPROVED":
            if ticket["approved_by"] != principal:
                raise Conflict("INVALID_TRANSITION")
            approved_at = ticket["approved_at"]
        else:
            cursor = await conn.execute(
                """
                UPDATE incidents SET status = 'APPROVED', approved_by = %s,
                    approved_at = clock_timestamp(), updated_at = clock_timestamp()
                WHERE id = %s RETURNING approved_at
            """,
                (principal, incident_id),
            )
            approved_at = (await cursor.fetchone())["approved_at"]
        body = {
            "incident_id": str(incident_id),
            "report_id": str(report["id"]),
            "status": "APPROVED",
            "approved_by": principal,
            "approved_at": approved_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        await conn.execute(
            """
            UPDATE api_idempotency SET incident_id = %s, response_status = 200, response_body = %s
            WHERE principal_id = %s AND operation = 'approve_incident' AND idempotency_key = %s
        """,
            (incident_id, Jsonb(body), *scope),
        )
        return body
