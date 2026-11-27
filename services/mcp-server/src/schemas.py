from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


def parse_uuid(value: object) -> object:
    return UUID(value) if isinstance(value, str) else value


WireUUID = Annotated[UUID, BeforeValidator(parse_uuid)]

PodName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=253,
        pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*$",
    ),
]
ContainerName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$",
    ),
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class EvidenceGap(ContractModel):
    scope: Literal["pod", "container", "logs_current", "logs_previous", "events"]
    code: Literal[
        "pod_not_found",
        "pod_uid_mismatch",
        "container_not_found",
        "not_applicable",
        "no_retained_data",
        "pod_identity_unavailable",
    ]
    container: ContainerName | None = None


class K8sInspectorInput(ContractModel):
    namespace: Literal["target-apps"]
    pod_name: PodName | None = None
    pod_uid: WireUUID | None = None
    container: ContainerName | None = None
    tail_lines: int = Field(default=80, ge=1, le=200)
    lookback_seconds: int = Field(default=900, ge=30, le=3600)
    include_previous: bool = True

    @model_validator(mode="after")
    def validate_target(self) -> "K8sInspectorInput":
        if (self.pod_name is None) != (self.pod_uid is None):
            raise ValueError("pod_name and pod_uid must be supplied together")
        if self.container is not None and self.pod_uid is None:
            raise ValueError("container requires a Pod target")
        return self


class Termination(ContractModel):
    reason: str | None = None
    exit_code: int
    finished_at: AwareDatetime | None = None


class ContainerEvidence(ContractModel):
    name: ContainerName
    kind: Literal["app", "init", "ephemeral"]
    restart_count: int | None = Field(default=None, ge=0)
    waiting_reason: str | None = None
    termination_state: Literal["observed", "not_applicable", "unavailable"]
    termination: Termination | None
    limits: dict[str, str]
    requests: dict[str, str] = Field(default_factory=dict)
    image: str | None = None
    readiness_probe: bool = False
    liveness_probe: bool = False
    empty_dir_mounts: list[str] = Field(default_factory=list)
    http_fault_enabled: bool | None = None


class LogEvidence(ContractModel):
    container: ContainerName
    instance: Literal["current", "previous"]
    text: str = Field(max_length=8192)
    truncated: bool


class EventEvidence(ContractModel):
    uid: UUID
    type: str
    reason: str
    message: str = Field(max_length=512)
    observed_at: AwareDatetime


class OwnerEvidence(ContractModel):
    kind: str
    name: PodName
    uid: UUID


class WorkloadEvidence(ContractModel):
    kind: Literal["Deployment"] = "Deployment"
    namespace: Literal["target-apps"]
    name: PodName


class K8sInspectorOutput(ContractModel):
    namespace: Literal["target-apps"]
    pod_name: PodName | None
    pod_uid: UUID | None
    observed_at: AwareDatetime
    status: Literal["ok", "partial", "not_found", "unavailable"]
    containers: list[ContainerEvidence]
    logs: list[LogEvidence] = Field(max_length=16)
    events: list[EventEvidence] = Field(max_length=20)
    gaps: list[EvidenceGap]
    owner: OwnerEvidence | None = None
    workload: WorkloadEvidence | None = None


class RunbookRagInput(ContractModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must contain text")
        return value.strip()


class RunbookHit(ContractModel):
    chunk_id: UUID
    source_kind: Literal["RUNBOOK", "POSTMORTEM"]
    title: str
    source_uri: str
    source_revision: str
    section: str
    excerpt: str = Field(max_length=1200)
    cosine_distance: float = Field(ge=0.0, le=2.0)


class RunbookRagOutput(ContractModel):
    status: Literal["ok", "no_matches"]
    matches: list[RunbookHit] = Field(max_length=10)
