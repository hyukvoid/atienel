# Phase 5 검증 결과

## 실행 조건

2026-11-27, 로컬 단일 노드 `k3d-atienel`, k3d v5.9.0 / K3s v1.35.5-k3s1.
노드 용량은 12 vCPU와 7,998,672 KiB 메모리였다. `aiops-worker` 한 복제본을
`DIAGNOSIS_MODE=verification`으로 실행했다. 워커는 실제 Valkey Consumer Group,
PostgreSQL 및 MCP를 사용했고, 모델 출력만 Pydantic AI TestModel로 생성했다.

k6 1.5.0은 k3d 노드의 네트워크에서 `aiops-api` ClusterIP를 사용했다. 10초
상승, 35초 유지, 10초 하강을 적용했다. 신규 시나리오는 최대 3 VU에서 반복당
고유 요청 2개를 병렬로 실행했고, 재전송은 최고 60 RPS로 설정했다.
재전송 시나리오는 setup에서 접수한 한 이벤트에 같은 본문과 `Idempotency-Key`를
반복 제출하고, 모든 응답의 티켓 ID와 Location이 일치하는지 검사했다.

## 부하 결과

| 시나리오 | 202 건수 | 평균 성공 RPS | p50 | p95 | p99 | 오류율 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A 신규 | 92 | 1.58 | 9.93 ms | 12.25 ms | 21.92 ms | 0% |
| B 재전송 | 2,705 | 46.51 | 3.58 ms | 4.52 ms | 5.54 ms | 0% |

평균 RPS는 58.16초 전체 실행 구간 기준이다. 두 시나리오의 p95 1초 미만,
오류율 1% 미만, 누락 반복 0건 임계치를 모두 통과했다. 재전송 setup 1건을 더한
93개 논리 이벤트에서 PostgreSQL 티켓·작업·리포트·성공 작업은 각각 93건이었다.
이벤트별 중복 티켓과 리포트, 이번 실행의 실패 작업과 미발행 작업은 각각 0건이었다.
Outbox 최대 지연은 17.55 ms였다. 접수 DB 시각에서 리포트 저장까지 p95는
254.64 ms, p99는 401.80 ms, 최대는 816.33 ms였다.

## 리소스, 대기열 및 프로파일

| 파드 역할 | 샘플 CPU 최대 | 샘플 메모리 최대 |
| --- | ---: | ---: |
| aiops-api (Pod당) | 149m | 55 MiB |
| aiops-worker | 118m | 164 MiB |
| mcp-server | 91m | 273 MiB |
| event-watcher | 1m | 34 MiB |
| outbox-reconciler | 3m | 41 MiB |
| PostgreSQL | 61m | 90 MiB |
| Valkey | 12m | 4 MiB |

- HPA 이벤트와 리소스 스냅샷에서 `1 → 2 → 1`을 확인했다. CPU가 목표 70%를
  넘었을 때 확장했고, 하강 안정화 기간 후 한 복제본으로 돌아왔다.
- 전체 Stream 길이와 PEL 최대 샘플은 각각 2건과 1건이었다. 부하 프로세스 종료를
  확인한 뒤 약 1.40초에 Stream과 PEL이 모두 0임을 처음 확인했다. 샘플 간격은
  약 5초다. `old_pending`과 `unpublished`는 기록한 워커 스냅샷에서 0이었다.
  누적 `failed` 7건은 부하 시작 전과 종료 후 같았다.
- PostgreSQL의 샘플 연결 최대는 역할별로 API 8개, 워커 1개, MCP 1개,
  reconciler 1개였다. 같은 시각에 각 역할의 최대에 도달했다는 의미는 아니다.
  PostgreSQL 로그에서 실행 중 `statement timeout`은 0건이었다.
  관측 구간 중 업무 컨테이너 재시작 카운터 증가는 0건이었다.
- py-spy 0.4.1의 49 Hz, 유휴 10초(978스택) 및 부하 20초(1,958스택)
  샘플에서 수집 오류는 0건이었다. 메인 스레드에서 selector 대기는 각각
  489/489, 881/979스택이었다. 부하 중 asyncio 콜백 처리 스택이 늘었고
  CPU 제한, 지속적인 연결 대기, 큐 누적을 뒷받침하는 증거는 관측되지 않았다.
  이번 부하 조건을 근거로 서비스 코드나 리소스 한도를 튜닝하지 않았다.

## 기능 수용 및 CI

- SC-001은 실물 fixture 3종을 각각 새 Pod에서 10회 재현해 30/30건을 관측 시각부터
  30초 이내 발행했다. 유형별 최대는 OOMKilled 1.430초, CrashLoopBackOff 0.726초,
  HTTP 5xx 급증 0.972초였다. 5xx의 기준 시각은 워처가 카운터 급증을 관측한 시각이다.
- SC-002는 워커를 멈춘 채 신규 30건 및 이벤트당 5회 재전송을 확인했다. 신규 최대
  지연은 28.0 ms였고, 티켓과 발행 작업은 이벤트당 1개였다.
- SC-004는 별도 티켓 10개의 승인 및 동일 키 재시도에서 최초 승인 기록을 확인했다.
  OOM E2E에서 권한 없는 승인과 OPEN 상태의 승인을 거부했으며 대상 Deployment
  설정은 승인 전후 일치했다. Valkey 중단 시 503, 같은 키의 202 재생, 유실 엔트리
  재발행 및 중단된 소비자가 남긴 PEL 항목으로 단일 리포트가 저장되는 것도 검증했다.
- 로컬 `make phase4-verify`와 `PHASE5_SLO=1 make phase5-verify`가 통과했다.
  확장 pytest는 10개 테스트가 모두 통과했다. Ruff, Python·Bash 구문 검사, k6
  구문 검사, actionlint 및 서비스 이미지 5종 빌드도 로컬에서 실행했다.

## SC-003 검토용 YAML 재검증

같은 로컬 클러스터를 verification 모드로 두고 MCP와 워커 이미지를 갱신했다.
MCP는 Pod UID와 제어 소유자를 확인했다. Deployment 이름은 명시적인 Pod 템플릿 선언,
앱 라벨, ReplicaSet 이름과 템플릿 해시가 일치할 때만 추론했다. 이 경로의 Kubernetes
조회 권한은 기존 Pod, 로그, 이벤트 범위다.

세 패치의 대상, 변경 필드, 해당 런북 절의 인용을 DB 리포트에서 대조한다. YAML은
`yaml.safe_load`로 파싱하고 검증된 패치와 대조한다. OOM은 재현 요청 로그와
128Mi 한도에서 종료된 이력을 확인한다. CrashLoop는 시작 오류 로그와 emptyDir
마운트를, HTTP 5xx는 활성화된 재현 플래그와 해당 구간의 500 로그 10건을 요구한다.
확인되지 않은 대상, 과거 종료, 인용 누락 또는 근거와 다른 값은 NULL과
`patch_unavailable_reason`으로 처리한다.

| 대표 시나리오 | 접수 DB 시각 → 리포트 저장 | 런북 인용 | 파싱 및 변경값 검증 |
| --- | ---: | ---: | --- |
| OOMKilled | 11.481초 | 3 | 통과. 현재 128Mi에서 검토용 메모리 한도 384Mi |
| CrashLoopBackOff | 0.164초 | 3 | 통과. 새 Pod에서 emptyDir를 초기화하는 템플릿 주석 |
| Http5xxSpike | 0.145초 | 3 | 통과. HTTP_FAULT_ENABLED를 false로 설정 |

확장 감지 표본은 장애 유형별 10회로 총 30건이었고 모두 패치가 저장됐다. 같은 구간의
일반 Kubernetes Warning 40건은 패치 대신 사유를 저장했다. 회귀 테스트는 Pod·소유자·
현재 값·로그·인용 누락, 오래된 종료 이력, 잘못된 대상이나 변경값을 확인했다.
OOM 승인 전후와 나머지 두 진단 전후의 Deployment 사양은 일치했다. live OOM 한도는
128Mi, HTTP 재현 스위치는 true였고 MCP ServiceAccount의 Deployment patch와 Pod delete
인가는 모두 거부됐다.

`make phase3-verify`, `make phase4-verify`, `PHASE5_SLO=1 make phase5-verify`가
통과했다. 확장 pytest는 27개 테스트가 통과했다. 로컬 `make phase5-ci`, actionlint,
`make phase5-images`도 통과했다. 별도의 로컬 이미지 실행에서 HTTP 스위치가 false일 때
재현 경로는 404를 반환하고 건강 상태는 200을 유지했다.

## 재현

```bash
DIAGNOSIS_MODE=verification make phase4-apply
make phase4-verify
PHASE5_PROFILE=1 make phase5-load
PHASE5_SLO=1 make phase5-verify
make phase5-ci phase5-images
```

수집 파일은 실행 시 출력한 임시 디렉터리 아래의 `k6.json`, `telemetry.json`,
`worker-idle.svg`, `worker-load.svg`다. 실측 파일 위치는 `/tmp/atienel-phase5-q0knwsjx`다. 이 보고서의 실행 ID는
`phase5:07511a0457f94416a4ca4ce7231b6d59:`다. 프로파일러의 ptrace 권한은
로컬 k3d 노드 PID 네임스페이스에 붙는 일회용 Docker 컨테이너에만 부여한다.
`PHASE5_FRESH_VUS`, `PHASE5_REPLAY_RATE`, `PHASE5_HOLD_SECONDS`로 부하를
조정할 수 있다.

GitHub Actions는 정적 검사, 이미지 빌드, 임시 k3d의 전체 통합 테스트를 별도
작업으로 실행한다. 로컬과 CI는 같은 Makefile 진입점을 사용한다.
