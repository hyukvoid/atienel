# 로컬 k3d 종단 검증

이 절차는 구현 후 실행한다. 클러스터 이름은 `atienel`로 통일한다.
Docker, k3d, kubectl, uv, curl, jq, 로컬 빌드 이미지, 시드용 회고록과 runbook,
승인자·조회자·수집자의 각 토큰을 준비한다. [배포 계약](contracts/kubernetes.md)

## Phase 1 데이터 인프라 검증

Docker 데몬이 실행 중인 상태에서 저장소 루트에서 실행한다. Phase 1은 Docker, k3d,
kubectl, Make, OpenSSL을 사용한다.

```bash
make k3d-up
make context
make phase1-apply
make phase1-verify
```

Makefile은 `k3d-atienel` 컨텍스트를 명시한다. 별도 kubectl 바이너리는
`KUBECTL=/path/to/kubectl`로 지정할 수 있다. 첫 배포 시 데이터 자격 증명을 생성하고
재적용 시 기존 Secret과 PVC를 사용한다. `postgresql-0`과 `valkey-0`은
`1/1 Running`, 초기화 Job은 `Complete`가 기대 상태다. 검증 대상에는
PVC, 벡터 확장, 마이그레이션, Consumer Group, MCP RBAC, metrics-server가 포함된다.

저장소에 로컬로 접속하려면 다음 포트 포워딩을 별도 터미널에서 실행한다.

```bash
make port-forward
make port-forward FORWARD_SERVICE=valkey PORT_FORWARD=6379:6379
```

Kubernetes API 허용 정책은 적용 시 서비스 IP와 단일 k3d 서버 IP를 사용한다.
추론 제공자 주소 범위를 구성할 때는 `PROVIDER_CIDR`에 해당 CIDR을 지정해
`make phase1-apply`를 실행한다.

## Phase 2 Hot Path 검증

Phase 1이 준비된 `k3d-atienel` 클러스터에서 실행한다. Python 3.12가 필요하다.

```bash
make phase2-apply
make phase2-verify
make phase2-verify PHASE2_VERIFY_ARGS=--recovery
```

배포 명령은 세 이미지를 빌드하고 k3d에 임포트한 뒤 워크로드와 API HPA를 적용한다.
Docker 재시작 이후에도 감시자가 API에 접속할 수 있도록 현재 k3d 노드 IP로 네트워크 정책을 다시 적용한다.
검증 명령은 OOMKilled/137, 워처의 202, PostgreSQL의 OPEN 티켓과 Valkey 작업이 같은 장애에
연결되는지 확인한다. 멱등 재요청, HTTP 500 카운터와 급증 접수, CrashLoopBackOff 접수도
확인한다. CrashLoop fixture는 검증 후 재시작한다. `--recovery`는 Valkey를 잠시
중지해 503을 확인하고, 복원한 뒤 outbox 발행과 같은 키의 202를 검증한다. 이어서 해당 테스트 엔트리의
유실을 재현하고 원래 작업 ID로 재발행되는지 확인한다.

## 환경 준비

```bash
uv sync --all-packages
k3d cluster create atienel --servers 1
kubectl config use-context k3d-atienel
```

구현된 이미지와 매니페스트로 네임스페이스, Secret, 데이터 서비스, 마이그레이션 및
시드 Job, 애플리케이션, HPA 순서로 설치한다. 컨테이너 빌드와 설치 명령은
구현에서 제공하는 진입점을 사용한다. 설치가 끝나면 다음을 확인한다.

```bash
kubectl -n aiops-data get statefulset,pods
kubectl -n aiops-core get deployment,hpa,pods
kubectl -n target-apps get deployment,pods
kubectl -n aiops-core port-forward svc/aiops-api 8000:8000
# 별도 터미널
kubectl -n target-apps port-forward svc/chaos-app 8080:8080
```

## 재현·감지·리포트·승인

운영자가 준비한 테스트 인증값을 사용한다. 재현 서버 계약이 인증을 요구하면
해당 요청에 테스트 자격 증명을 붙인다.

```bash
curl -fsS http://127.0.0.1:8000/readyz
curl -sS -X POST http://127.0.0.1:8080/faults/oom
curl -sS -X POST http://127.0.0.1:8080/faults/crashloop
for run in $(seq 1 10); do
  curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/faults/http-500
done
kubectl -n target-apps get events --field-selector type=Warning
kubectl -n aiops-core logs deployment/event-watcher --since=5m
```

OOM 재시작 중 포트 포워딩이 끊기면 준비 상태가 복원된 뒤 다시 연결한다.
세 재현은 서로 간섭하지 않도록 각각 실행한다. 워처의 접수 로그에서 각 티켓 UUID를
확인한다. 티켓별로 최초 접수와 `NEEDS_REVIEW` 전환까지 시간을 측정한다.

```bash
curl -fsS -H "Authorization: Bearer ${ATIENEL_READER_TOKEN}" \
  "http://127.0.0.1:8000/incidents/${ATIENEL_INCIDENT_ID}" | jq .
curl -fsS -X POST -H "Authorization: Bearer ${ATIENEL_APPROVER_TOKEN}" \
  -H "Idempotency-Key: approve-${ATIENEL_INCIDENT_ID}" \
  "http://127.0.0.1:8000/incidents/${ATIENEL_INCIDENT_ID}/approve" | jq .
```

리포트의 예상 원인, 출처, 패치를 시드 자료와 비교한다. 같은 승인 요청을 재전송해
응답과 최초 승인 기록이 유지되는지 확인한다. 조회 전용 토큰은 승인을 거부하게 한다.
승인 전후의 워크로드 설정을 비교한다. [REST 계약](contracts/rest.md)

## 접수 요청 재현

다음 예시는 기존 테스트 Pod 이름과 UID를 환경 변수에 설정한 뒤 사용한다.
동일 이벤트와 키를 재전송할 때는 생성된 JSON을 그대로 재사용한다.

```bash
ATIENEL_EVENT_ID="fixture:$(date -u +%s)-001"
ATIENEL_PAYLOAD=$(jq -nc --arg event "$ATIENEL_EVENT_ID" \
  --arg pod "$ATIENEL_POD_NAME" --arg uid "$ATIENEL_POD_UID" \
  --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{event_id:$event,namespace:"target-apps",pod_name:$pod,pod_uid:$uid,
    incident_type:"KubernetesWarning",observed_at:$at,source:"kubernetes_event",
    summary:"Local ingestion fixture"}')
curl -si -H "Authorization: Bearer ${ATIENEL_INGEST_TOKEN}" \
  -H "Content-Type: application/json" -H "Idempotency-Key: ${ATIENEL_EVENT_ID}" \
  --data "$ATIENEL_PAYLOAD" http://127.0.0.1:8000/incidents
```

최초 요청과 동일 재시도에서 202, Location 및 티켓 ID가 같은지 확인한다.
같은 키에 다른 본문을 보내면 409여야 한다.

## 복구 및 권한 확인

- 워커를 중지한 상태에서 유효한 이벤트 30건을 보내고 같은 이벤트를 5회 재전송한다.
  요청 시간, 티켓 수와 논리적 작업 수를 기록한다. 워커를 다시 시작해 밀린 작업이
  처리되었는지 확인한다. 선택적 워커 HPA를 사용한 경우 고정 복제 설정에서 시험한다.
- Valkey를 중지하고 새 이벤트를 제출해 `503`을 확인한다. 복구 후 처음 사용한 키로
  재시도하고 티켓과 작업이 각각 하나인지 확인한다. 처리 중 워커를 종료해 재회수와
  리포트 단일 저장도 확인한다. [스트림 계약](contracts/stream.md)
- `kubectl auth can-i get pods --subresource=log -n target-apps --as=system:serviceaccount:aiops-core:mcp-server`
  는 yes, 같은 주체의 `patch pods`와 `aiops-data`의 `list pods`는 no여야 한다.
- `kubectl top pods -A` 및 호스트 메모리를 관측하고, HPA 최대 복제 수에서 메모리
  한도와 실제 사용량을 기록한다. SC-001~004 검증 수치는 [기능 명세](spec.md)를 따른다.
