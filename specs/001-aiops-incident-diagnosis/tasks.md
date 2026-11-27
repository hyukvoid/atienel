---
description: "Atienel 장애 감지 및 AI 진단 구현 태스크"
---

# Tasks: Kubernetes 장애 감지 및 AI 진단

**Input** — [명세](spec.md), [설계](plan.md), [설계 근거](research.md),
[데이터 모델](data-model.md), [계약](contracts/), [종단 검증 절차](quickstart.md).

**형식** — `[P]`는 명시된 선행 작업 이후 같은 묶음에서 병렬로 진행할 수 있음을 뜻한다.
`[USn]`은 명세의 사용자 스토리 번호다. 파일 경로는 저장소 루트 기준이다.
계약을 변경할 경우 해당 명세를 먼저 갱신한다.

## Phase 1 — k3d 및 데이터 인프라

**완료 조건** — 로컬 노드에서 두 저장소가 준비되고 마이그레이션과 Stream 그룹 초기화가
완료된다. MCP의 로그 조회는 허용되고 교차 네임스페이스 조회·쓰기·삭제는 거부된다.

- [X] T001 [P] `Makefile`의 k3d 시작 경로를 로컬 단일 서버 및 metrics-server 활성 상태에 맞추고, 포트 포워딩을 통한 접근과 현재 kubectl 컨텍스트 확인을 지원한다.
- [X] T002 [P] `k8s/namespaces.yaml`에 target-apps, aiops-core, aiops-data를 정의한다.
- [X] T003 [P] [Kubernetes 계약](contracts/kubernetes.md)을 따라 `k8s/rbac/service-accounts.yaml`, `k8s/rbac/target-read.yaml`에 워크로드별 SA와 대상 네임스페이스 Role/RoleBinding을 작성한다. 작성 후 적용은 T002에 의존한다.
- [X] T004 [P] `k8s/data/postgresql.yaml`에 PG16 및 두 벡터 확장이 포함된 고정 이미지, StatefulSet, PVC, Service, 리소스와 프로브를 구성한다.
- [X] T005 [P] `k8s/data/valkey.yaml`에 StatefulSet, PVC, Service, AOF, noeviction, 메모리 상한과 프로브를 구성한다.
- [X] T006 [P] `k8s/data/migrations/001_incidents.sql`에 [데이터 모델 DDL](data-model.md#ddl)의 `incidents.id`, `incidents.event_id`, `incidents.namespace`, `incidents.pod_name`, `incidents.pod_uid`, `incidents.container_name`, `incidents.incident_type`, `incidents.observed_at`, `incidents.signal`, `incidents.status`, `incidents.approved_by`, `incidents.approved_at`, `incidents.created_at`, `incidents.updated_at`을 구현한다.
- [X] T007 [P] `k8s/data/migrations/002_reports_runbooks.sql`에 [데이터 모델 DDL](data-model.md#ddl)의 `investigation_reports.id`, `investigation_reports.incident_id`, `investigation_reports.suspected_cause`, `investigation_reports.evidence`, `investigation_reports.citations`, `investigation_reports.limitations`, `investigation_reports.recommended_yaml_patch`, `investigation_reports.patch_unavailable_reason`, `investigation_reports.created_at`, `runbook_chunks.id`, `runbook_chunks.source_kind`, `runbook_chunks.source_uri`, `runbook_chunks.source_revision`, `runbook_chunks.title`, `runbook_chunks.section`, `runbook_chunks.chunk_no`, `runbook_chunks.body`, `runbook_chunks.embedding`, `runbook_chunks.created_at`과 vector/vectorscale 확장 및 DiskANN 인덱스를 구현한다. SQL 적용 시 T006이 선행한다.
- [X] T008 [P] `k8s/data/migrations/003_jobs_idempotency.sql`에 [데이터 모델 DDL](data-model.md#ddl)의 `investigation_jobs.id`, `investigation_jobs.incident_id`, `investigation_jobs.state`, `investigation_jobs.stream_entry_id`, `investigation_jobs.published_at`, `investigation_jobs.attempt_count`, `investigation_jobs.next_attempt_at`, `investigation_jobs.execution_token`, `investigation_jobs.execution_expires_at`, `investigation_jobs.last_error_code`, `investigation_jobs.created_at`, `investigation_jobs.updated_at`, `api_idempotency.principal_id`, `api_idempotency.operation`, `api_idempotency.idempotency_key`, `api_idempotency.request_hash`, `api_idempotency.incident_id`, `api_idempotency.response_status`, `api_idempotency.response_body`, `api_idempotency.response_location`, `api_idempotency.created_at`과 관련 인덱스를 구현한다. SQL 적용 시 T006이 선행한다.
- [X] T009 `k8s/data/migrations/004_roles.sql` 및 `k8s/data/bootstrap-jobs.yaml`에 역할별 DB 권한, Secret 참조, 순서가 있는 마이그레이션, 0-0부터 시작하는 멱등 Consumer Group 초기화 Job을 구성한다. Job은 동시에 하나만 실행한다. T004~T008 이후 진행한다.
- [X] T010 `k8s/network-policies.yaml`에 기본 제한과 계약에 명시된 DNS·API·업무 연결을 구성한다. T001~T009 적용 후 [검증 절차](quickstart.md)에 따라 확장, PVC, 그룹, 유효 RBAC와 연결을 확인한다.

## Phase 2 — 공유 스키마 및 Hot Path

### 공통 기반

- [ ] T011 `packages/shared/src/atienel_shared/contracts.py` 및 `packages/shared/pyproject.toml`에 [REST](contracts/rest.md) 이벤트·응답·오류와 [Stream](contracts/stream.md) v1 메시지의 정규화와 입력 검증을 구현한다. 스키마 직렬화에서 날짜, UUID, enum, 추가 필드 처리를 계약과 맞춘다.

### US1 — 테스트 장애 재현

**독립 검증** — 워처와 워커가 중지된 상태에서 세 엔드포인트를 각각 호출하여 종료 코드 137,
CrashLoopBackOff, HTTP 500 및 카운터 증가를 직접 관측한다.

- [ ] T012 [P] [US1] `services/chaos-app/src/main.py`, `services/chaos-app/src/fixtures.py`에 테스트 전용 OOM·설정 오류 재현 제어와 HTTP 500/Pod별 계측을 구현한다. fixture 재설정과 재현 범위는 [Kubernetes 계약](contracts/kubernetes.md)을 따른다.
- [ ] T013 [US1] `services/chaos-app/Dockerfile`, `k8s/apps/chaos-app.yaml`에 로컬 전용 재현 서비스, 전용 fixture Deployment와 명시적 CPU·메모리 예산을 구성하고 세 재현을 독립 확인한다. T012 이후 진행한다.

### US2 — 실시간 감지와 중복 없는 접수

**독립 검증** — 진단 워커와 MCP 없이 신호를 수집하고, 최초 접수의 202와 중복 방지,
발행 장애의 503 및 복구 후 재시도를 확인한다.

- [ ] T014 [P] [US2] `services/event-watcher/src/observer.py`, `services/event-watcher/src/http_metrics.py`에 대상 Core Warning·Pod 종료 list/watch, 재연결, 카운터 기반 5xx 검출, OOM 신호 상관관계와 고정 이벤트 ID를 구현한다. T011 이후 진행한다.
- [ ] T015 [P] [US2] `services/aiops-api/src/ingestion_repository.py`에 비동기 psycopg 접수 트랜잭션, 이벤트 식별 충돌 판정, 키 예약 및 티켓·작업 단일 생성을 구현한다. T008·T011 이후 진행한다.
- [ ] T016 [P] [US2] `services/aiops-api/src/auth.py`에 토큰 검증, 안정적인 주체 ID 매핑, 조회·접수·승인 권한 구분을 구현한다.
- [ ] T017 [P] [US2] `services/event-watcher/src/webhook.py`, `services/event-watcher/src/main.py`에 키와 본문을 유지하는 비동기 웹훅 전송, 일시 오류 재시도, 접수 결과 로깅을 구현한다. T011·T014 이후 진행한다.
- [ ] T018 [US2] `services/aiops-api/src/publication.py`, `services/aiops-api/src/reconciler.py`에 작업 행 잠금, 상한 있는 XADD, 발행·응답 기록, 미발행 복구와 유실 대조를 구현한다. T009·T015 이후 진행한다.
- [ ] T019 [US2] `services/aiops-api/src/main.py`, `services/aiops-api/src/incident_routes.py`에 POST 접수, GET 조회, liveness/readiness, 멱등 응답 재생과 계약 오류를 연결한다. DB 커밋과 스트림 발행 확인 뒤 202를 반환한다. T016·T018 이후 진행한다.
- [ ] T020 [US2] `services/event-watcher/Dockerfile`, `services/aiops-api/Dockerfile`, `k8s/apps/hot-path.yaml`, `k8s/hpa/aiops-api.yaml`에 워처·API·복구 프로세스 배포와 metrics-server 기반 API HPA를 구성한다. T017·T019 이후 [접수 검증 절차](quickstart.md)를 실행한다.

## Phase 3 — MCP 및 Runbook 임베딩

### US3 — 근거 조회 기반

**독립 검증** — 워커 없이 인증된 MCP 클라이언트에서 두 도구를 호출한다.
Pod 증거, 인용 가능한 문단, 입력 오류와 근거 누락을 구분하고 재시드 결과가 중복되지 않는지 확인한다.

- [ ] T021 [P] [US3] `services/mcp-server/pyproject.toml`, `services/mcp-server/src/schemas.py`에 공식 문서 또는 Context7에서 시그니처를 확인한 FastMCP 4.x 의존성과 [MCP 계약](contracts/mcp.md)의 Pydantic 입출력 타입을 반영한다.
- [ ] T022 [P] [US3] `services/mcp-server/src/embedding.py`, `data/runbooks/seed-manifest.json`에 동일하게 고정한 로컬 다국어 임베딩 리비전과 질의/문서 인코딩 방식을 정의하고 세 재현 유형에 대응하는 초기 Runbook·회고록을 준비한다.
- [ ] T023 [P] [US3] `services/mcp-server/src/k8s_inspector.py`에 Pod UID 검증, 최근/이전 로그·이벤트·종료 코드·리소스 한도 조회, 부분 결과와 오류를 구현한다. T003·T021 이후 진행한다.
- [ ] T024 [P] [US3] `services/mcp-server/src/seed_runbooks.py`, `k8s/data/seed-runbooks.yaml`에 문서 분할, 384차원 벡터 생성과 재실행 가능한 적재 Job을 구현한다. T007·T009·T022 이후 진행한다.
- [ ] T025 [P] [US3] `services/mcp-server/src/runbook_rag.py`에 코사인 거리 검색, 후보 필터와 인용 위치 반환을 구현한다. T007·T021·T022 이후 진행한다.
- [ ] T026 [US3] `services/mcp-server/src/main.py`, `services/mcp-server/Dockerfile`, `k8s/apps/mcp-server.yaml`에 읽기 전용 도구 두 개와 stateless `/mcp`, 내부 TLS, 워커 전용 Bearer 및 전용 DB 읽기 역할을 연결한다. T023~T025 이후 MCP wire에서 도구 검색과 스키마, 빈 결과 및 권한 실패를 확인한다.

## Phase 4 — Cold Path 및 HITL

### US3 — 자동 진단과 리포트 저장

**독립 검증** — 시드된 증거와 OPEN 티켓으로 작업을 발행한다. 두 MCP 도구의 결과로 리포트가
저장되고 NEEDS_REVIEW로 전환되며, 워커 재시작과 동일 메시지 재전달에도 리포트가 하나인지 확인한다.

- [ ] T027 [P] [US3] `services/aiops-worker/src/consumer.py`에 Consumer Group 읽기, PEL 회수, 버전·ID 검증, 잘못된 메시지 격리와 완료 확인 뒤 ACK/정리를 구현한다. T009·T011 이후 진행한다.
- [ ] T028 [P] [US3] `services/aiops-worker/src/job_repository.py`에 DB 실행권, 제한 시간과 재시도, 토큰 검증 후 리포트·NEEDS_REVIEW·성공 상태 동시 커밋을 구현한다. T006~T009 이후 진행한다.
- [ ] T029 [P] [US3] `services/aiops-worker/pyproject.toml`, `services/aiops-worker/src/agent.py`에 공식 문서 또는 Context7로 시그니처를 확인한 LangGraph 1.2·Pydantic AI 2.0 의존성과 엄격한 진단 출력, 인용 검증, 패치 또는 제안 불가 사유를 구현한다. T021 이후 진행한다.
- [ ] T030 [US3] `services/aiops-worker/src/main.py`, `services/aiops-worker/src/graph.py`, `services/aiops-worker/Dockerfile`, `k8s/apps/aiops-worker.yaml`에 작업 소비부터 두 MCP 조회, 진단, 결과 저장과 종료까지 연결한다. TLS와 자격 증명은 워커에 주입하고 업무 리소스 한도를 명시한다. T020·T026~T029 이후 진행한다.

### US4 — 담당자 승인

**독립 검증** — 저장된 NEEDS_REVIEW 티켓을 준비하고, 승인자와 비인가 사용자 및 OPEN 상태의
승인 요청을 각각 보낸다. 첫 승인 기록이 유지되고 워크로드 설정이 바뀌지 않는지 확인한다.

- [ ] T031 [US4] `services/aiops-api/src/approval_repository.py`, `services/aiops-api/src/approval_routes.py`, `services/aiops-api/src/main.py`에 POST 승인, 트랜잭션 행 잠금, 승인자·시각 기록, 멱등 재생과 충돌 처리를 구현한다. T016·T019 이후 저장된 리포트로 독립 검증하며, 전체 실행은 T030과 통합한다.
- [ ] T032 `uv.lock`에서 workspace 의존성을 확정하고 `pyproject.toml`의 Ruff 설정으로 전체 서비스를 검사한다. T026·T030·T031 이후 고정 의존성으로 모든 서비스 이미지를 빌드하고 로컬 배포를 확인한다.

## Phase 5 — E2E·부하·프로파일링 검증

- [ ] T033 [P] `tests/e2e/test_incident_lifecycle.py`에 로컬 k3d에서 OOMKilled/137 → OPEN → NEEDS_REVIEW → APPROVED를 검증할 E2E를 작성한다. 동일 이벤트 재전송, 증거 인용, 승인 재시도와 설정 유지도 비교하고, CrashLoopBackOff 및 HTTP 5xx는 각각 독립 수용 사례로 검증한다.
- [ ] T034 [P] `tests/load/aiops_api_hpa.js`에 k6로 고유 신호와 멱등 재전송을 생성하는 ramp/hold/cooldown 부하를 작성한다. 202 비율·p95, 중복 여부, CPU와 HPA 1→2→1 관측 및 큐 적체를 기록할 수 있게 한다.
- [ ] T035 [P] `tests/profiling/profile_worker.sh`에 통제된 로컬 프로파일링 환경에서 py-spy로 워커를 샘플링하고 flamegraph를 생성하는 절차를 작성한다. 유휴/진단 구간을 비교하며, 임시 프로파일링 권한은 검증 환경에만 둔다.
- [ ] T036 `tests/e2e/test_publication_recovery.py`로 Valkey 발행 실패 뒤 재접수, 워커 중단 뒤 재회수를 시험하고, T033~T035를 순서대로 실행해 [성공 기준](spec.md#measurable-outcomes), HPA 이벤트, 메모리 실측과 프로파일링 증거를 `tests/validation-results.md`에 기록한다. T032~T035 이후 진행한다.

## Dependencies & Execution Order

| 구간 | 선행 조건 및 병렬 묶음 |
|---|---|
| 인프라 | T001~T008 초안 병렬 → T009 초기화 → T010 적용·확인. DDL 적용은 번호순. |
| US1·US2 | T010 후 T011. US1 T012~T013과 US2 작업은 독립 진행 가능. US2는 T014와 T015 및 T016을 병렬 처리하고, 워처와 접수 API 양쪽 완료 후 T020. |
| US3 | Phase 2 이후 T021·T022 병렬, 조건 충족 후 T023·T024·T025 병렬 → T026. 워커의 T027·T028·T029는 각 선행 조건 충족 후 병렬 → T030. |
| US4 | T016·T019 이후 승인 작업을 저장된 리포트로 독립 검증할 수 있다. T030과 종단 통합 후 T032. |
| 검증 | 재현·로드·프로파일링 스크립트는 T033~T035에서 병렬 작성하고, T036에서 동일 로컬 환경을 순서대로 사용한다. |

## Parallel Examples

- **US1** — T012는 T014와 다른 서비스에서 진행할 수 있다.
- **US2** — T014 워처 개별 관측, T015 접수 저장, T016 인증 구현은 서로 다른 파일이다.
- **US3** — T023 Kubernetes 조사와 T025 문서 검색을 각각 구현하며 T024 시드 배치를 준비한다.
- **US4** — T031 승인 흐름은 DB에 준비한 리포트를 사용해 T030 워커 통합과 병행할 수 있다.
  T031이 변경하는 API 진입점의 T019 변경을 먼저 마친다.

## Implementation Strategy

1. **MVP** — T001~T020을 통해 로컬 장애 재현과 워커 없이 동작하는 실시간 접수를 검증한다.
2. **진단 확장** — T021~T030으로 관측 증거와 문서 인용이 있는 리포트를 자동 생성한다.
3. **승인·검증** — T031~T036으로 사람 승인을 연결하고 복구, 부하, 리소스, 워커 실행 특성을 측정한다.
