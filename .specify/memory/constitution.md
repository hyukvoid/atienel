<!--
Sync Impact Report
- 버전 변경: 미제정 → 1.0.0 (최초 제정안)
- 원칙 신설: 기술 스택, Hot/Cold Path 분리, 최소 권한과 사람 승인, 로컬 리소스 예산
- 섹션 구체화: 로컬 클러스터 구성, 개발 및 검토 절차, Governance
- 삭제된 기존 규범: 없음
- 후속 확인: TODO(RATIFICATION_DATE) 공식 채택일 기록
- 검토가 끝나면 커밋 전에 이 주석을 제거한다.
-->
# Atienel 엔지니어링 헌법

## Core Principles

### I. 기술 스택과 버전

- 서비스는 Python 3.12 이상을 사용해야 한다. 의존성은 uv workspace로 관리하고
  린트는 Ruff로 수행해야 한다.
- API는 FastAPI와 Pydantic v2를 사용하고, PostgreSQL에는 psycopg v3 비동기
  인터페이스로 접근해야 한다. 데이터 계층은 PostgreSQL 16, pgvector, pgvectorscale을
  사용해야 하며, 비동기 작업 전달에는 Valkey Streams를 사용해야 한다.
- MCP 서버는 FastMCP 4.x의 Stateless Streamable HTTP를 `/mcp`에서 제공해야 한다.
  레거시 `/sse` 전송 방식은 금지한다.
- 진단 에이전트는 LangGraph 1.2로 상태 그래프와 HITL을 구성하고, Pydantic AI 2.0으로
  엄격한 타입의 진단 출력을 검증해야 한다. 로컬 Kubernetes 환경에는 k3d(k3s)를 사용해야 한다.

### II. Hot Path와 Cold Path의 엄격한 분리

- `aiops-api`의 `POST /incidents`는 인시던트를 PostgreSQL에 기록하고
  Valkey Streams에 발행한 직후 `202 Accepted`를 반환해야 한다.
- Hot Path에서 직접 LLM 또는 MCP를 호출하거나 동기 폴링을 수행하는 것은 금지한다.
- Cold Path는 스트림 이벤트를 비동기로 소비하고 읽기 전용 MCP 및 pgvector 기반 RAG로
  원인을 진단해야 한다. 진단 완료는 수집 응답의 선행 조건이 될 수 없다.

### III. 최소 권한 RBAC와 사람 승인

- `mcp-server`의 ServiceAccount에 부여되는 Kubernetes 리소스 권한은
  `pods`, `pods/log`, `events`에 대한 `get`, `list`, `watch`로 한정해야 한다.
  모든 쓰기 및 삭제 권한과 와일드카드 권한은 금지한다.
- 사고 대응 과정에서 실제 클러스터 상태를 변경하려면 실행 전에 사람의 명시적 승인(HITL)을
  받아야 한다. 승인 이후에도 MCP ServiceAccount는 읽기 전용 권한만 사용해야 한다.

### IV. 로컬 리소스 예산

- 로컬 배포는 RAM 16GB 노트북을 기준으로 설계해야 한다.
- 모든 Kubernetes Deployment는 각 컨테이너에 `resources.requests`와
  `resources.limits`를 명시해야 한다. 리뷰에서는 두 필드의 존재를 확인하고,
  클러스터 구성 요소와 워크로드를 함께 실행할 때의 메모리 예산을 검토해야 한다.

## 로컬 클러스터 구성

Atienel은 OOMKilled, CrashLoopBackOff, HTTP 5xx 급증과 같은 Kubernetes 장애를 수집하고
비동기로 원인을 진단한다. 로컬 k3d 클러스터는 대상 워크로드에 `target-apps`,
수집 및 진단 서비스에 `aiops-core`, 데이터 서비스에 `aiops-data`를 사용해야 한다.

## 개발 및 검토 절차

- 서비스 코드를 작성하거나 수정하기 전에 `specs/`에서 관련 명세를 읽어야 한다.
- API 계약, Valkey 스트림 스키마, DB 테이블, Kubernetes RBAC 정책을 변경할 때는
  구현 전에 관련 명세를 갱신해야 한다.
- FastMCP, LangGraph, Pydantic AI 코드를 구현하기 전에 Context7 MCP 또는 공식 문서로
  사용할 버전의 API 시그니처를 확인해야 한다.
- 변경 검토에서는 관련 명세와 구현의 일치 여부를 확인해야 한다. 수집 경로 변경은
  DB 기록과 스트림 발행 후 `202`가 반환되는지, LLM 및 MCP 호출이 없는지 검증해야 한다.
  RBAC 변경은 ServiceAccount의 유효 권한을 검증해야 한다.

## Governance

- 이 헌법은 Atienel의 설계, 구현, 코드 리뷰에 적용한다. 변경 작성자와 리뷰어는
  변경에 관련된 원칙의 준수 여부를 확인하고, 위반 사항은 병합 전에 해결해야 한다.
- 개정안에는 변경 이유, 영향을 받는 원칙과 산출물, 필요한 이행 조치를 기록해야 한다.
  프로젝트 유지관리자의 승인을 거쳐 개정안을 채택하고 버전 및 최근 개정일을 갱신해야 한다.
- 버전은 SemVer를 따른다. 기존 규범을 제거하거나 호환되지 않게 재정의하면 MAJOR,
  원칙을 추가하거나 지침을 실질적으로 확장하면 MINOR, 의미가 유지되는 문구 수정은
  PATCH를 올려야 한다.

**Version**: 1.0.0 | **Ratified**: TODO(RATIFICATION_DATE): 공식 채택일 확인 후 기록 | **Last Amended**: 2026-11-27
