import re
from typing import Annotated
from uuid import UUID

from atienel_shared.contracts import MAX_EVENT_BYTES, IncidentDetail, IncidentEvent
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .auth import authenticate
from .ingestion_repository import Conflict, lookup, reserve
from .publication import publish

router = APIRouter()
KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message


def authorize(request: Request, permission: str):
    headers = request.headers.getlist("authorization")
    principal = authenticate(headers[0] if len(headers) == 1 else None, request.app.state.tokens)
    if principal is None:
        raise ApiError(401, "UNAUTHENTICATED", "A valid Bearer token is required")
    if permission not in principal.permissions:
        raise ApiError(403, "FORBIDDEN", "Insufficient permissions")
    return principal.id


@router.post("/incidents", status_code=202)
async def ingest(
    request: Request,
    idempotency_key: Annotated[list[str] | None, Header(alias="Idempotency-Key")] = None,
):
    principal = authorize(request, "incidents:ingest")
    if (
        not idempotency_key
        or len(idempotency_key) != 1
        or not KEY_PATTERN.fullmatch(idempotency_key[0])
    ):
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY", "Exactly one valid key is required")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_EVENT_BYTES:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "The event exceeds 16 KiB")
        body.extend(chunk)
    try:
        event = IncidentEvent.model_validate_json(body)
    except ValidationError as exc:
        raise ApiError(422, "INVALID_REQUEST", "Invalid incident event") from exc
    pool, queue = request.app.state.pool, request.app.state.queue
    key = idempotency_key[0]
    try:
        incident_id, reservation = await reserve(pool, principal, key, event)
    except Conflict as exc:
        raise ApiError(409, str(exc), "The key or event conflicts with an earlier request") from exc
    if reservation and reservation["response_status"] is not None:
        return JSONResponse(
            reservation["response_body"],
            status_code=reservation["response_status"],
            headers={"Location": reservation["response_location"]},
        )
    payload, location = await publish(pool, queue, incident_id, principal, key)
    return JSONResponse(payload, status_code=202, headers={"Location": location})


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
async def detail(request: Request, incident_id: str):
    authorize(request, "incidents:read")
    try:
        incident_id = UUID(incident_id)
    except ValueError as exc:
        raise ApiError(422, "INVALID_REQUEST", "Invalid incident UUID") from exc
    row = await lookup(request.app.state.pool, incident_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Incident not found")
    return IncidentDetail.model_validate(row)
