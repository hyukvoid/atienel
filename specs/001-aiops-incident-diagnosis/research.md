# Phase 0 연구 및 설계 결정

| 결정 | 근거 | 검토한 대안 |
|---|---|---|
| DB outbox와 요청 중 비동기 발행을 함께 사용 | DB 기록 이후 큐 장애를 복구하면서 발행 완료 전에는 202를 반환하지 않는다. | 요청 경로에서만 이중 기록하면 중간 실패 복구가 어렵다. 순수 백그라운드 발행은 202 계약을 충족하지 못한다. |
| Valkey Consumer Group과 DB 기준 작업 식별자 | 작업자 장애 시 회수하고, 재전달 시 이미 완료된 리포트를 보호한다. | 메시지 수신 직후 ACK하면 처리 중 장애에서 작업을 잃는다. |
| PostgreSQL 16과 DiskANN 코사인 검색 | 384차원 임베딩을 보존하고 원문 위치를 함께 반환한다. 작은 검증 코퍼스에서는 정확 검색으로 기준 결과를 구한다. | HNSW만 사용하면 pgvectorscale 사용 요구를 충족하지 못한다. |
| Pod 상태 관측과 HTTP 카운터를 Warning 관측에 추가 | OOM 종료 상태와 HTTP 응답이 각각 고유 관측 경로에서 감지되며, 접수는 같은 계약을 사용한다. | Warning 이벤트 단독 감시는 재현 유형 전체의 탐지를 보장할 수 없다. |
| 워커의 결정적 조사 그래프와 타입화된 LLM 출력 | 두 MCP 조회를 명시적으로 수행하고 근거를 결합한다. 모델은 진단 데이터와 패치 제안만 반환한다. | 모델에 도구 선택과 수정 권한을 함께 위임하면 조회 재현성과 승인 경계를 검증하기 어렵다. |
| 로컬 Bearer 토큰과 저장된 티켓 상태 기반 HITL | 사람 승인 전까지 티켓은 데이터베이스에서 대기하고 워커는 반환한다. 승인과 감사 기록은 단일 트랜잭션으로 완료한다. | 장시간 실행 중인 그래프 또는 별도 승인 재개 큐는 이 범위에서 운영 비용을 높인다. |
| API CPU HPA, 워커 고정 복제 수부터 시작 | 로컬 예산을 유지하면서 접수를 확장한다. 추후 워커 자동 확장 시 CPU 대신 큐 지연을 신호로 쓴다. | 모든 서비스를 CPU로 확장하면 I/O 대기 및 모델 호출 병목을 제대로 반영하지 못한다. |

## 버전 및 재현 확인

2026-11-27 기준 공식 배포 자료에서 FastMCP 4.2.6, LangGraph 1.2.24, Pydantic AI 2.0.0
배포 항목을 확인했다. 구현 단계에서 Python 3.12의 uv lock을 확정하고,
MCP 출력 스키마, LangGraph 상태 전이, Pydantic AI 타입 검증을 호환성 시험으로 확인한다.
[Pypi FastMCP](https://pypi.org/project/fastmcp/4.2.6/) ·
[PyPI LangGraph](https://pypi.org/project/langgraph/1.2.24/) ·
[PyPI Pydantic AI](https://pypi.org/project/pydantic-ai/2.0.0/)

조사 그래프에는 공식 `StateGraph`와 `compile` 패턴을 적용하고, LLM 출력에는
Pydantic AI의 `Agent(output_type=...)` 패턴을 적용한다. 정확한 패키지 조합, 실제 지연,
검색 재현율, Pod 메모리 사용량은 구현 검증 항목이다.
[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) ·
[Pydantic AI 출력 문서](https://pydantic.dev/docs/ai/core-concepts/output/)
