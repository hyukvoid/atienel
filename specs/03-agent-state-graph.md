# Phase 4 진단 및 승인 흐름

워커는 Valkey Consumer Group 메시지의 작업 UUID를 PostgreSQL의 실행권과 대조한다.
LangGraph는 Pod 근거 조회 → 매뉴얼 검색 → Pydantic AI 구조화 진단 → 증거 및 인용 검증 →
리포트 저장 순으로 실행한다. 모델에 도구 실행 권한을 부여하지 않는다. 리포트 저장과
NEEDS_REVIEW 전이를 함께 커밋하고 이 상태에서 진단을 끝낸다. 승인 API가 별도 트랜잭션에서
APPROVED를 기록한다. YAML 제안은 사람이 읽을 산출물이다.

로컬 배포 기본값인 verification 모드는 외부 호출 없이 Pydantic AI TestModel로
관측 근거에 따른 출력을 생성한다. provider 모드는 워커 전용 Kubernetes Secret에서
모델 ID, API 키와 OpenAI 호환 HTTPS 엔드포인트를 읽는다. 환경에 맞는 제공자 egress
CIDR은 배포 시 명시한다. 두 모드는 같은 그래프, 진단 스키마, 인용 검증과 저장 경로를 사용한다.

SC-003 검토용 패치는 Pod UID, 소유자에서 추론한 대상, 현재 설정 및 진단 시각과
일치하는 장애 증거, 해당 런북 절을 모두 요구한다. 검증 모드와 제공자 모드는 동일한
허용 규칙을 사용한다. 제공자 출력이 근거로 산출된 선언과 일치하지 않으면 패치 대신
사유를 저장한다. 로컬 fixture 규칙은 다른 워크로드에 적용되지 않는다.
