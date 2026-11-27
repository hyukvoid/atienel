# FastAPI REST 계약

업무 경로는 JSON을 사용한다. `POST /incidents`는 `incidents:ingest`,
`GET /incidents/{id}`는 `incidents:read`, 승인 요청은 `incidents:approve` 권한이 필요하다.
로컬 환경은 Secret으로 주입한 주체별 불투명 Bearer 토큰을 사용한다. API는 토큰을 검증하여
안정적인 주체 ID와 권한을 결정한다. 토큰 교체 시에도 주체 ID는 유지된다.
클러스터 안에서는 내부 서비스로 노출하고, 로컬 운영자는 포트 포워딩으로 접근한다.

| 메서드와 경로 | 요청 | 성공 응답 |
|---|---|---|
| `POST /incidents` | JSON 이벤트와 `Idempotency-Key` | `202`, `Location: /incidents/{id}`, `{"incident_id":"<uuid>"}` |
| `GET /incidents/{id}` | UUID 경로 변수 | `200`, 티켓 상세 |
| `POST /incidents/{id}/approve` | UUID 경로 변수와 `Idempotency-Key`. 본문 필드는 없다. | `200`, `incident_id`, `report_id`, `status: APPROVED`, `approved_by`, `approved_at` |
| `GET /healthz` | 없음 | `200`, 프로세스 생존 |
| `GET /readyz` | 없음 | `200`, 접수 의존성 준비. 미준비 시 `503` |

준비 확인은 DB와 Valkey만 검사한다. 응답의 UUID는 소문자 표준 문자열로, 날짜는 UTC RFC 3339로
직렬화한다. FastAPI는 헤더 이름을 대소문자 구분 없이 받고 다중 헤더를 확인할 수 있도록
`Header(alias="Idempotency-Key")`의 목록 바인딩을 사용한다. [FastAPI 헤더 문서](https://fastapi.tiangolo.com/tutorial/header-params/)

## 장애 이벤트 요청

Pydantic v2 모델은 추가 필드를 거부한다. 필드는 기본적으로 필수이며 아래 nullable 필드는
누락 시 null로 정규화한다. 총 요청 크기는 16 KiB를 상한으로 둔다.

| 필드 | 타입 및 검증 |
|---|---|
| `event_id` | 1–256자, 경로 구분자를 제외한 설계상 허용 문자 집합 `[A-Za-z0-9:._-]` |
| `namespace` | `target-apps` |
| `pod_name`, `pod_uid` | Pod DNS 이름과 UID. 신호는 실제 Pod와 연계한다. |
| `container_name` | 컨테이너 DNS 라벨 또는 null |
| `incident_type` | `OOMKilled`, `CrashLoopBackOff`, `Http5xxSpike`, `KubernetesWarning` |
| `observed_at` | 타임존이 있는 RFC 3339 날짜 |
| `source` | `kubernetes_event`, `pod_status`, `http_metrics` |
| `summary` | 1–2048자. 관측 사실을 요약한다. |

OOM 원인이 확인된 경우 워처는 Warning과 Pod 상태를 동일한 종료 식별자로 정규화한다.
`oom:<pod_uid>:<container_name>:<종료 세대>`에서 종료 세대는 해당 종료의
`finishedAt`을 UTC epoch 나노초의 10진 문자열로 정규화한 값이다. 종료와 일치한다는
근거가 없는 Warning은 OOM으로 분류하지 않는다. CrashLoopBackOff와 기타 Warning은
`k8s:<event_uid>`를 사용하며, HTTP 신호는 `http:<pod_uid>:<급증 구간 시작 epoch 초>`를 쓴다.
이벤트의 불변 식별 필드는 namespace, pod UID·이름, container name, incident type이다.
두 신호가 같은 OOM 종료를 표현할 때 source, 시각, summary의 차이는 허용하고 최초 값은 보존한다.

## 멱등성 및 오류

1. 쓰기마다 헤더 한 개가 필요하다. 값은 1–128자의 `[A-Za-z0-9._:-]+`이다.
   누락, 중복, 포맷 오류는 `400 INVALID_IDEMPOTENCY_KEY`다.
2. DB 키 범위는 `(principal_id, operation, idempotency_key)`다. operation은
   `create_incident` 또는 `approve_incident`이며, 승인 요청 해시에는 경로의 티켓 UUID를 넣는다.
   요청 해시는 검증 및 UTC 정규화 후 키 순서를 고정한 UTF-8 JSON의 SHA-256이다.
3. 같은 키와 같은 해시는 성공 응답의 상태 코드·JSON·Location을 재생한다.
   같은 키와 다른 해시는 `409 IDEMPOTENCY_CONFLICT`다. 인증과 권한을 재생보다 먼저 확인한다.
4. 다른 키로 같은 이벤트를 접수하면 동일 티켓에 연결한다. 불변 식별 필드가 다르면
   `409 EVENT_CONFLICT`다. 발행이 확인된 이벤트의 재접수는 기존 `202`를 반환한다.
   발행 중이거나 결과가 불명확하면 `503`과 `Retry-After: 1`을 내고 같은 키를 재사용하게 한다.
   접수 후의 조회 상태가 변해도 `202`의 응답은 계속 동일하다.
5. 승인은 티켓 행을 잠그고 현재 상태, 리포트 존재 여부와 승인 권한을 확인한 다음
   상태와 승인 이력, 성공 응답을 한 트랜잭션에 저장한다.
   이미 확정됐다면 같은 담당자의 재요청은 기존 승인 기록을 반환하고, 다른 담당자의
   새 승인은 `409 INVALID_TRANSITION`이다. 이 경로에서는 클러스터 변경을 실행하지 않는다.
6. 정상 재시도 기간을 명확히 하려고 멱등 키는 연결된 티켓과 함께 보관한다.
   `503`과 입력·권한 오류는 성공 스냅샷으로 캐시하지 않는다.

티켓 상세 응답은 `incident_id`, `event_id`, `namespace`, `pod_name`, `pod_uid`,
`container_name`, `incident_type`, `observed_at`, `status`, `report`, `approval`을 포함한다.
`report`는 저장된 진단의 ID, 추정 원인, 관측 증거, 문서 인용, YAML 제안, 한계를 반환하고,
`approval`은 승인자 ID 및 시각을 반환한다. 아직 생성되지 않은 객체는 null이다.

오류 본문은 `{"error":{"code":"...","message":"..."}}`로 통일한다. `401`은 자격 증명 오류,
`403`은 권한 부족, `404`는 없는 티켓, `413`은 요청 초과, `422`는 잘못된 본문이나 UUID다.
`503`은 저장 또는 확인 가능한 발행 실패 등 일시적 의존성 오류다.
