# Kubernetes 배치 계약

로컬 k3d(k3s)의 애플리케이션 네임스페이스는 `target-apps`, `aiops-core`, `aiops-data`다.
Kubernetes 시스템 구성 요소는 기존 시스템 네임스페이스에 유지한다. 표의 요청과 한도는
컨테이너 하나를 기준으로 하며 CPU 단위는 millicore, 메모리 단위는 MiB다.

## 네임스페이스별 워크로드

| 네임스페이스 | 워크로드 | 종류 | 기본 / 최대 복제본 | CPU 요청 / 한도 | 메모리 요청 / 한도 | 서비스 또는 역할 |
| --- | --- | --- | --- | --- | --- | --- |
| `target-apps` | `chaos-app` | Deployment | 1 / 1 | 50m / 300m | 64Mi / 256Mi | ClusterIP 8080, HTTP 장애 재현 및 Pod별 카운터 |
| `target-apps` | `chaos-oom-fixture` | Deployment | 1 / 1 | 25m / 200m | 32Mi / 128Mi | ClusterIP 8080, 테스트용 메모리 제한 검증 |
| `target-apps` | `chaos-crash-fixture` | Deployment | 1 / 1 | 25m / 200m | 32Mi / 128Mi | ClusterIP 8080, 테스트용 설정 오류와 반복 시작 실패 검증 |
| `aiops-core` | `event-watcher` | Deployment | 1 / 1 | 50m / 200m | 96Mi / 256Mi | 대상 Pod와 Event 구독, 5xx 관측, 접수 웹훅 |
| `aiops-core` | `aiops-api` | Deployment | 1 / 2 | 100m / 500m | 128Mi / 384Mi | ClusterIP 8000, 접수 및 승인 |
| `aiops-core` | `aiops-worker` | Deployment | 1 / 2 | 150m / 750m | 256Mi / 896Mi | 작업 소비 및 진단, 기본 프로필은 복제본 1개 고정 |
| `aiops-core` | `mcp-server` | Deployment | 1 / 1 | 100m / 500m | 512Mi / 1024Mi | ClusterIP 8000의 `/mcp` |
| `aiops-core` | `outbox-reconciler` | Deployment | 1 / 1 | 50m / 250m | 64Mi / 256Mi | 데이터베이스에 남은 미완료 작업의 발행 복구 |
| `aiops-data` | `postgresql` | StatefulSet | 1 / 1 | 250m / 1000m | 768Mi / 2048Mi | ClusterIP 5432, ordinal용 Headless Service, 8Gi PVC |
| `aiops-data` | `valkey` | StatefulSet | 1 / 1 | 100m / 400m | 128Mi / 512Mi | ClusterIP 6379, ordinal용 Headless Service, 2Gi PVC |

- 각 Deployment의 업무 컨테이너와 추가 init/sidecar에 CPU 및 메모리의 요청과 한도를 명시한다.
  StatefulSet과 DB 마이그레이션 및 문서 적재 Job에도 같은 규칙을 적용한다.
  상한 산정에 포함된 최대 복제본을 넘지 않도록 Deployment 업데이트는
  `maxSurge: 0`, `maxUnavailable: 1`을 사용한다. 단일 복제본 갱신 시 일시 중단을 허용한다.
- 초기 컨테이너 메모리 한도 합은 `target-apps` 512Mi, `aiops-core` 4096Mi,
  `aiops-data` 2560Mi로 최대 7168Mi(7Gi)다. 512Mi 한도의 초기화 Job은 한 번에
  하나씩 실행해 애플리케이션 한도 합을 7.5Gi 이내로 유지한다. 남는 RAM을 OS, Docker,
  k3s 제어 플레인과 시스템 Pod에 배정한다. 노드 가용 메모리 및 디스크, 실제 메모리,
  CPU 제한에 따른 처리량을 초기 검증에서 확인한다.
- PostgreSQL은 PG16용 `vector`, `vectorscale` 확장이 포함된 고정 이미지로 실행하고,
  확장 설치 및 테이블 초기화는 별도 마이그레이션 Job의 권한으로 수행한다.
  PostgreSQL과 Valkey의 PVC는 로컬 StorageClass를 사용한다. Valkey는 영속 볼륨에 AOF를
  기록하고 `noeviction`을 적용하며, AOF 재작성과 연결 버퍼를 위해
  `maxmemory`를 컨테이너 한도보다 낮게 잡는다. 영속 큐 및 재발행 규칙은 스트림 계약을 따른다.
- MCP 임베딩 모델은 이미지에 포함하고 아티팩트 버전과 이미지 digest를 고정한다.
  검증 프로필에서 모델 초기화와 동시 검색 시 메모리 최대치를 측정한다.
- API 및 MCP는 startup/readiness/liveness에 자체 상태 신호를 사용한다.
  API readiness는 DB와 Valkey 접속을 확인하고, liveness는 내부 프로세스 상태를 확인한다.
  워커는 종료 신호를 받으면 새 메시지 수신을 멈추고 완료 상태가 저장된 작업만 ACK한다.

## 장애 재현과 감지 경계

- 장애 재현 엔드포인트는 로컬 운영자 범위에서 활성화한다.
  OOM과 설정 오류는 사전 배포된 테스트 컨테이너에서 재현한다. `chaos-app`의 제어 요청은
  미리 구성된 내부 테스트 경로에 전달한다. 테스트 워크로드에는 Kubernetes API 권한을 부여하지 않는다.
- `POST /faults/oom`은 제한된 메모리 검증을 시작하는 로컬 제어 요청이며,
  `POST /faults/crashloop`는 전용 테스트 컨테이너의 잘못된 설정 시나리오를 시작한다.
  CrashLoop fixture는 로컬 담당자가 승인한 재설정 이후 정상 준비 상태로 돌아온다.
  `GET /faults/http-500`은 요청당 HTTP 500을 반환한다.
  각 호출은 `chaos-app`의 포트 8080에서 로컬 테스트 담당자만 이용한다.
- 감시자는 대상 네임스페이스의 Core API Pod와 Event를 list/watch하고 재연결 시 관측을 이어간다.
  `OOMKilled`는 Pod 종료 상태에서 관측하며 대응 Warning이 있으면 같은 종료로 연결한다.
- HTTP 5xx 관측은 감시자가 대상 Pod의 사전에 지정된 계측 포트를 약 5초마다 비동기로 조회하는
  방식으로 시작한다. Pod UID별 누적 카운터 증가량을 30초 관측 구간에 합산해 10건 이상이면
  신호를 생성한다. 카운터 초기화나 Pod 교체는 이전 카운터와 연결하지 않는다. 관측이
  끊긴 구간의 상태는 확인 불가로 기록한다. 워크로드 매핑은 대상 Pod에 명시된 계측 라벨을 사용한다. Pod UID별로 임계값을 넘는
  하나의 에피소드에 같은 이벤트 식별자를 부여하고 임계값 미만으로 회복하면 다음
  에피소드를 허용한다.

## ServiceAccount와 RBAC

ServiceAccount는 워크로드별로 분리한다. Kubernetes API 접근은 `event-watcher`와
`mcp-server`에만 부여하고 나머지 Pod에는 `automountServiceAccountToken: false`를
설정한다. 권한 리소스는 관측 대상인 `target-apps`에 만들고, 바인딩 주체의
`namespace`는 ServiceAccount가 있는 `aiops-core`로 명시한다.

| ServiceAccount | Role 및 범위 | API 그룹 | 리소스 | 동사 |
| --- | --- | --- | --- | --- |
| `aiops-core/event-watcher` | `target-apps/event-watcher-read` | Core `""` | `pods`, `events` | `list`, `watch` |
| `aiops-core/mcp-server` | `target-apps/mcp-read` | Core `""` | `pods`, `events` | `get`, `list`, `watch` |
| `aiops-core/mcp-server` | `target-apps/mcp-read` | Core `""` | `pods/log` | `get` |

MCP의 로그 조회에 필요한 서브리소스 동사는 `get`이다. 위 Role은 헌법에 허용된
동사와 리소스의 부분집합으로 제한하며, 로그 이외의 Pod 서브리소스를 생략한다.
역할은 네임스페이스 RoleBinding으로 연결한다. `events.k8s.io` API 권한은 설계에
포함하지 않으며 두 읽기 서비스는 Core API 이벤트를 사용한다. 리포트 승인 이후에도
이 권한은 동일하다. 실제 조치에 필요한 Kubernetes 변경은 별도 승인 절차를 거친다.
[Kubernetes RBAC 문서](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)

예를 들어 관리 권한을 가진 검증 실행자는 다음 결과를 확인한다.

```bash
kubectl auth can-i --as=system:serviceaccount:aiops-core:mcp-server get pods --subresource=log -n target-apps
kubectl auth can-i --as=system:serviceaccount:aiops-core:mcp-server watch events -n target-apps
kubectl auth can-i --as=system:serviceaccount:aiops-core:mcp-server patch pods -n target-apps
kubectl auth can-i --as=system:serviceaccount:aiops-core:mcp-server delete pods -n target-apps
kubectl auth can-i --as=system:serviceaccount:aiops-core:mcp-server get secrets -n target-apps
kubectl auth can-i --as=system:serviceaccount:aiops-core:mcp-server get pods -n aiops-core
```

첫 두 명령은 `yes`, 나머지는 `no`여야 한다. 정적 검토에서 RBAC 전체를 검사해
다른 바인딩으로 권한이 넓어지지 않았는지 확인한다.

## 네트워크 및 서비스 권한

- 기본 구성에서 외부 공개 Ingress, NodePort, LoadBalancer를 생성하지 않는다.
  로컬 운영자는 필요할 때 의도적으로 port-forward하고, API 호출 권한을 별도로 검증한다.
- 네트워크 정책은 네임스페이스별 수신과 송신을 기본 제한하고 DNS 및 필요한 경로만 연다.
  chaos-app → 두 테스트 fixture, 감시자 → 계측 엔드포인트 및 API, 워커 → MCP,
  API·워커·복구 프로세스 → PostgreSQL/Valkey,
  MCP → PostgreSQL, 워커 → 설정한 추론 제공자가 기본 경로다.
  MCP와 감시자의 Kubernetes API 접근과 kube-system DNS 통신도 명시한다.
  서비스 주소 DNAT와 CNI 정책의 실제 매칭은 k3d에서 검증한다.
  [K3s 네트워크 서비스](https://docs.k3s.io/networking/networking-services)
- Kubernetes 인가와 DB 인가를 분리한다. MCP는 RAG 검색에 필요한 DB 읽기 자격 증명을
  사용하고, 승인과 진단 쓰기 권한은 API/워커의 DB 자격 증명으로 분리한다.
  PostgreSQL 및 Valkey 접속 비밀과 내부 호출 인증 정보는 해당 소비자에게만 제공한다.
  MCP ClusterIP는 내부 TLS로 제공한다. 워커에는 내부 CA와 MCP 전용 Bearer 자격 증명을
  주입하고 서비스 DNS 이름에 대한 인증서를 검증한다.

## HPA와 큐 처리량

기본 HPA는 `aiops-api`에 적용한다. 최소 1개, 최대 2개 복제본이며 CPU 평균 사용률
70%를 목표로 한다. CPU 요청을 설정하고 metrics-server가 제공하는
`metrics.k8s.io`를 사용한다. 축소 안정화 창은 120초로 시작한다.
[Kubernetes HPA 문서](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/)

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: aiops-api
  namespace: aiops-core
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: aiops-api
  minReplicas: 1
  maxReplicas: 2
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 120
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

워커는 기본 프로필에서 단일 복제본이다. 큐 확장이 필요하면 외부 지표 어댑터를 통해
대기 중인 논리적 작업 수를 제공하고 `autoscaling/v2` External 지표의
`averageValue: "5"`에 최소 1개, 최대 2개를 적용한다. 지표는 DB의 실행 가능 대기 작업
집계를 사용해 Stream 중복 엔트리 및 완료 이력을 제외한다. 선택 프로필에서는 어댑터 메모리도
남은 노트북 예산에 포함하여 측정한다. 나머지 구성 요소는 단일 인스턴스로 유지한다.
리소스 단위와 스케줄링 검증은 공식 문서를 따른다.
[Kubernetes 컨테이너 리소스 관리](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)

CrashLoop fixture 재설정은 로컬 운영자가 fixture Deployment를 재시작해 수행한다.
