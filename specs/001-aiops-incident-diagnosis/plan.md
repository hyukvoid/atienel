# Implementation Plan: Kubernetes 장애 감지 및 AI 진단

**Branch**: `main` (현재 체크아웃), Spec Kit 기능 ID `001-aiops-incident-diagnosis`  
**Date**: 2026-11-27  
**Spec**: [spec.md](spec.md)

## Summary

테스트 워크로드의 Warning, OOM 종료, HTTP 5xx 급증을 감지한다. Hot Path는 DB에
티켓과 작업을 저장하고 Valkey 발행을 확인한 뒤 202를 반환한다. Cold Path는
MCP로 관측 증거와 문서 근거를 수집하고, 사람이 검토할 리포트를 기록한다.

## Technical Context

| 항목 | 설계 |
|---|---|
| Language/Version | Python 3.12+, uv workspace, Ruff |
| Primary Dependencies | FastAPI, Pydantic v2, 비동기 psycopg v3, FastMCP 4.x, LangGraph 1.2, Pydantic AI 2.0 |
| Storage | PostgreSQL 16 + pgvector + pgvectorscale, Valkey Streams |
| Testing | pytest 단위·계약·통합 시험과 k3d 종단 검증 |
| Target Platform | 로컬 k3d(k3s), RAM 16GB의 단일 개발 호스트 |
| Project Type | 사고 감지·접수·비동기 진단 서비스 |
| Performance Goals | 신호 감지 후 30초 접수, 접수 요청의 95%가 1초 이내, 대표 진단 2분 이내 |
| Constraints | Hot Path 비동기 I/O, MCP 읽기 전용, 승인된 리포트의 불변성 |
| Scale/Scope | 로컬 단일 클러스터, 대상 네임스페이스 1개, 검증 시나리오 약 30건 |

## Constitution Check

| 게이트 | 설계 전 및 설계 후 검토 |
|---|---|
| 접수 분리 | 통과. DB와 Valkey만 참조한다. 진단 런타임과 독립적으로 202를 검증한다. |
| 최소 권한과 HITL | 통과. MCP 권한을 target-apps 읽기로 제한한다. 승인 API는 리포트만 확정한다. |
| 로컬 RAM 예산 | 설계 통과. 컨테이너별 requests·limits와 동시 복제 수를 정의했다. 실측은 구현 후 확인한다. |
| 명세 우선·버전 확인 | 통과. 기존 명세와 헌법을 읽고 아래 계약을 작성했다. 고정 버전 설치와 런타임 호환성 시험은 구현 전에 수행한다. |

## Project Structure

### Documentation (this feature)

- [research.md](research.md) — 설계 결정과 대안
- [data-model.md](data-model.md) — DDL, 인덱스, 상태와 트랜잭션
- [contracts/stream.md](contracts/stream.md) — Valkey 페이로드, 작업 분배와 복구
- [contracts/rest.md](contracts/rest.md) — 접수·조회·승인과 멱등성
- [contracts/mcp.md](contracts/mcp.md) — Stateless Streamable HTTP와 Pydantic 도구 스키마
- [contracts/kubernetes.md](contracts/kubernetes.md) — 네임스페이스, 배포, RBAC, HPA
- [quickstart.md](quickstart.md) — 종단 검증 절차

### Source Code (repository root)

```text
services/
├── chaos-app/
├── event-watcher/
├── aiops-api/
├── aiops-worker/
└── mcp-server/
packages/shared/
k8s/{apps,data,rbac,hpa}/
data/runbooks/
tests/{load,profiling}/
```

기존 uv workspace 서비스 경계를 사용한다. 복구 발행 프로세스는 API 패키지의 접수
저장소와 발행 로직을 공유하는 별도 Deployment다. DB 마이그레이션과 매니페스트 구현은
후속 작업의 범위다.

## 구현 순서와 Cold Path

1. 버전 호환성을 확인하고 DB와 Valkey 계약을 구현한다. 작업 ID를 DB에서 유지하여
   DB 커밋과 Valkey 발행 사이의 장애를 복구한다.
2. 세 재현 시나리오와 워처를 구현하고 접수의 입력 검증, 중복 제거, 202 시점을 검증한다.
3. MCP 두 도구를 구현한다. 워커의 LangGraph는 작업 획득 → Pod 조사 → 문서 검색 →
   구조화된 진단 생성 → 증거 및 인용 검증 → 저장 순으로 진행한다. 원문·로그는
   입력 데이터로 처리하고, 모델이 해당 텍스트에 있는 지시를 실행할 수 있는 권한을 부여하지 않는다.
4. 리포트 저장과 `NEEDS_REVIEW` 전이에서 자동 진단 그래프의 실행을 종료한다.
   DB에 보존된 리뷰 상태가 HITL 경계다. 승인 API는 별도 트랜잭션에서
   `APPROVED`를 기록한다. 패치 문자열은 승인 후에도 조회용 산출물로 보존한다.
5. HPA·RBAC·리소스 예산과 장애 복구를 로컬 환경에서 검증한다.
