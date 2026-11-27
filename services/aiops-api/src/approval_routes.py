from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request

from .approval_repository import approve
from .incident_routes import KEY_PATTERN, ApiError, authorize
from .ingestion_repository import Conflict

router = APIRouter()


@router.post("/incidents/{incident_id}/approve")
async def approval(
    request: Request,
    incident_id: str,
    idempotency_key: Annotated[list[str] | None, Header(alias="Idempotency-Key")] = None,
):
    principal = authorize(request, "incidents:approve")
    if (
        not idempotency_key
        or len(idempotency_key) != 1
        or not KEY_PATTERN.fullmatch(idempotency_key[0])
    ):
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY", "Exactly one valid key is required")
    try:
        identifier = UUID(incident_id)
    except ValueError as exc:
        raise ApiError(422, "INVALID_REQUEST", "Invalid incident UUID") from exc
    async for chunk in request.stream():
        if chunk:
            raise ApiError(422, "INVALID_REQUEST", "Approval does not accept a request body")
    try:
        return await approve(request.app.state.pool, principal, idempotency_key[0], identifier)
    except LookupError as exc:
        raise ApiError(404, "NOT_FOUND", "Incident not found") from exc
    except Conflict as exc:
        raise ApiError(409, str(exc), "Approval conflicts with the ticket or request") from exc
