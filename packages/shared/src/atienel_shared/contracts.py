import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

STREAM = "aiops:incidents:v1"
MAX_EVENT_BYTES = 16 * 1024
DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DNS_NAME = rf"^{DNS_LABEL}(?:\.{DNS_LABEL})*$"
RFC3339 = re.compile(r"\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d(?:\.\d+)?(?:[Zz]|[+-]\d\d:\d\d)")


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IncidentType(StrEnum):
    OOM = "OOMKilled"
    CRASH = "CrashLoopBackOff"
    HTTP = "Http5xxSpike"
    WARNING = "KubernetesWarning"


class IncidentEvent(ContractModel):
    event_id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9:._-]+$")
    namespace: Literal["target-apps"]
    pod_name: str = Field(max_length=253, pattern=DNS_NAME)
    pod_uid: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    container_name: str | None = Field(default=None, pattern=rf"^{DNS_LABEL}$")
    incident_type: IncidentType
    observed_at: datetime
    source: Literal["kubernetes_event", "pod_status", "http_metrics"]
    summary: str = Field(min_length=1, max_length=2048)

    @field_validator("observed_at", mode="before")
    @classmethod
    def require_timestamp(cls, value: object) -> object:
        if isinstance(value, datetime) or (isinstance(value, str) and RFC3339.fullmatch(value)):
            return value
        raise ValueError("observed_at must be an RFC 3339 timestamp")

    @field_validator("observed_at")
    @classmethod
    def to_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("summary")
    @classmethod
    def require_database_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("summary contains a null character")
        value.encode("utf-8")
        return value

    def request_hash(self) -> bytes:
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).digest()


class Accepted(ContractModel):
    incident_id: UUID


class IncidentDetail(ContractModel):
    incident_id: UUID
    event_id: str
    namespace: str
    pod_name: str
    pod_uid: str
    container_name: str | None
    incident_type: IncidentType
    observed_at: datetime
    status: Literal["OPEN", "NEEDS_REVIEW", "APPROVED"]
    report: dict | None
    approval: dict | None


class ErrorDetail(ContractModel):
    code: str
    message: str


class ErrorResponse(ContractModel):
    error: ErrorDetail


class StreamMessage(ContractModel):
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)]
    job_id: UUID
    incident_id: UUID
