# MCP 도구 계약

[기능 명세](../spec.md)의 FR-006, FR-007, FR-010을 구현한다.

## 전송 및 인증

- `fastmcp` 패키지 4.x에서 Streamable HTTP를 사용한다. ASGI 앱의 구성 경계는
  `mcp.http_app(path="/mcp", stateless_http=True)`다. 기본 서비스 주소는
  `https://mcp-server.aiops-core.svc:8000/mcp`다. 외부 Ingress를 열지 않고 세션 고정,
  레거시 `/sse`, 진행 알림 및 서버 측 세션 상태를 사용하지 않는다.
- 워커는 FastMCP 클라이언트로 MCP 프로토콜을 협상한다. 통합 검증 기준은 4.x 클라이언트와
  서버가 MCP 2026-07-28로 도구를 검색하고 호출하는 것이다. 프로토콜 헤더와 메타데이터는
  클라이언트가 생성한다. 두 도구의 성공 본문은 MCP 결과의 `structuredContent`다.
- 로컬 배포에서는 FastMCP `StaticTokenVerifier`에 워커 전용 Bearer 자격증명을
  Secret으로 주입한다. 워커는 내부 TLS 서버 인증서를 검증한다. 자격증명이 없거나
  검증이 실패하면 도구 핸들러를 실행하기 전에 요청을 거부한다. `aiops-api`에
  MCP 자격증명을 제공하지 않으며 요청 및 응답 본문의 로깅은 기본적으로 끈다.
- 두 도구에 `readOnlyHint=true`, `destructiveHint=false`를 표시한다. 실제 권한은
  Kubernetes의 네임스페이스 범위 Pod/로그/Event 읽기 RBAC 및 PostgreSQL
  `runbook_chunks` SELECT 전용 역할로 제한한다. Kubernetes Secret, Pod exec, 패치 실행,
  임의 SQL, 임의 URL 요청을 노출하는 도구는 없다.
- FastMCP 4.x는 프로토콜 2026-07-28 및 Pydantic 구조화 결과를 지원한다. 도구 시그니처,
  입력 모델, 반환 타입이 생성하는 스키마와 stateless HTTP 모드는 아래 공식 문서를 기반으로
  선택했다. 정확한 4.x 패치 버전은 구현 시 잠금 파일에 고정하고 실제 wire 응답을 검증한다.
  [FastMCP 4 출시 안내](https://blog.gofastmcp.com/3mufbh2vcv22o)
  [HTTP 배포](https://gofastmcp.com/docs/deployment/http#enabling-stateless-mode)
  [도구 타입 및 검증](https://gofastmcp.com/docs/servers/tools)
  [Pydantic strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/)

## 공통 Pydantic v2 규칙

아래 코드 블록은 계약용 스키마 선언이다. 두 함수는 `request`라는 Pydantic 모델 인자를 받고
구체적인 출력 모델을 반환한다. 따라서 두 MCP 호출의 `params.arguments`는
`{"request": {...}}` 형태이며, `structuredContent`의 루트에는 출력 모델 필드가 직접 위치한다.
알 수 없는 입력 키를 거부하고 숫자 문자열을 정수로 변환하지 않는다.
시각은 시간대가 있는 RFC 3339 문자열로 직렬화하고 UUID는 표준 문자열 형식을 사용한다.

```python
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

PodName = Annotated[str, StringConstraints(
    min_length=1, max_length=253,
    pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*$",
)]
ContainerName = Annotated[str, StringConstraints(
    min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$",
)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class EvidenceGap(ContractModel):
    scope: Literal["pod", "container", "logs_current", "logs_previous", "events"]
    code: Literal[
        "pod_not_found", "pod_uid_mismatch", "container_not_found",
        "not_applicable", "no_retained_data", "pod_identity_unavailable",
    ]
    container: ContainerName | None = None
```

## `k8s_inspector`

`async def k8s_inspector(request: K8sInspectorInput) -> K8sInspectorOutput`

```python
class K8sInspectorInput(ContractModel):
    namespace: Literal["target-apps"]
    pod_name: PodName | None = None
    pod_uid: UUID | None = None
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
```

- 워커는 티켓의 Pod 이름과 UID를 전달한다. 같은 이름의 새 Pod는 다른 대상이다.
  서버가 Pod 조회에서 UID를 확인한 후 컨테이너별 로그를 조회한다. 로그 API는 이름으로
  호출하므로 조회 후 UID를 다시 확인하고 Pod 교체 또는 삭제가 확인되면 로그를 폐기한다.
  Event는 대상 UID로 한정하며, Pod가 삭제된 후에도 남아 있는 Event는 반환할 수 있다.
- `container=null`이면 대상 Pod의 컨테이너 상태를 모두 조회하고 로그는 이름순 최대
  8개 컨테이너에 대해 현재 로그와, 요청 및 재시작 이력이 있는 경우 이전 로그를 조회한다.
  컨테이너를 지정하면 그 컨테이너의 상태와 로그를 조회한다. Event는 지정된 관측 구간 내에서
  해당 Pod의 최신 20개로 한정하고 최근 순으로 반환한다.
- 종료 이력은 현재 `state.terminated`를 우선하고 `lastState.terminated`를 확인한다.
  이력이 없으면 `not_applicable`, 상태를 조회할 수 없으면 `unavailable`, 실제 종료
  정보가 있으면 `observed`로 표시한다. `termination`은 `observed`일 때만 객체다.
  `limits`는 해당 컨테이너의 선언된 Kubernetes Quantity 문자열이다. 컨테이너 정의에
  한도가 없으면 빈 객체를 반환한다.
- 도구는 로그 수집 시 컨테이너·인스턴스당 최대 8192바이트로 제한한다. 로그 및 Event
  메시지에서 자격증명 패턴을 가리고, 로그가 일부만 채집되면 `truncated=true`를 기록한다.
  조회가 성공하고 근거 누락이 없으면 `ok`다. Pod는 확인했지만 요청한 근거를 얻을 수
  없으면 `partial`과 해당 `gaps`를 반환한다. 종료 이력이나 이전 로그가 구조적으로
  해당하지 않으면 `not_applicable`로 표시하고 상태는 `ok`로 유지할 수 있다. 삭제
  또는 UID 불일치는 `not_found`이며 해당 사유를 `gaps`에 기록한다. 일시적인
  API·인증 실패는 도구 오류로 처리한다.
- HTTP 5xx처럼 Pod 식별자가 없는 신호는 Pod 이름과 UID를 함께 생략하여 호출한다.
  도구는 클러스터에 조회를 보내지 않고 빈 근거 목록, `status="unavailable"` 및
  `pod_identity_unavailable`을 반환한다. 워커는 별도 관측 신호의 증거와 로그 조회 한계를
  진단 리포트에 기록한다.

## `runbook_rag`

`async def runbook_rag(request: RunbookRagInput) -> RunbookRagOutput`

```python
class RunbookRagInput(ContractModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)


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
```

- 워커는 장애 유형, 종료 사유 및 확인된 증거를 바탕으로 검색어를 구성한다. 검색어 공백을
  제거한 결과가 비어 있으면 입력 오류다. 서버는 동일한 모델 리비전으로 색인된
  `multilingual-e5-small`의 384차원 코사인 벡터를 조회한다. 질의에는 `query: `,
  코퍼스에는 `passage: ` 접두사를 넣는다. 모델은 이미지에 고정하고 로컬 CPU에서 실행한다.
  [데이터 모델](../data-model.md)
- 코사인 거리 오름차순으로 최대 `top_k`건을 반환하며 초기 채택 임계값은 거리 0.35
  이하다. 임계값은 대표 회고록 및 매뉴얼로 검증한다. 임계값을 충족하는 행이 없으면
  `status="no_matches", matches=[]`를 반환한다. 임베딩 또는 DB 실패는 도구 오류다.
- `excerpt`는 저장된 문단의 일부이며, `source_uri`와 `source_revision`, `section`,
  `chunk_id`로 인용을 추적한다. 도구는 `source_uri`에 접속하지 않는다. 워커는
  리포트에 실제 결과에 존재하는 인용만 저장한다.
- 검색 결과와 Kubernetes 로그의 텍스트는 증거 데이터로만 처리한다. 리포트에 들어갈
  YAML 패치는 텍스트 제안이며 도구 호출이나 클러스터 변경을 유발하지 않는다.

## 실패와 수용 검증

- FastMCP 입력 오류, 인증·권한 오류, 시간 초과를 성공 결과로 변환하지 않는다. 워커는
  일시 실패를 재시도 대상으로 분류하고, 스키마와 권한 오류는 설정 점검이 필요한 실패로
  기록한다. 오류에는 원본 자격증명, SQL, 로그 본문을 포함하지 않는다.
- 도구 검색이 두 이름과 각 입출력 스키마를 공개하는지 확인한다. 정상 호출, 숫자 문자열,
  추가 필드, 대상 밖 네임스페이스, 잘못된 UID, 삭제된 Pod, 이전 로그 없음, 빈 검색,
  검색 저장소 장애를 검증한다. 성공 응답을 Pydantic JSON 모드로 역검증하고 같은 도구를
  별도 복제본에 연속 호출해 세션 의존성이 없는지 확인한다.
