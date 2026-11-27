# PostgreSQL 16 데이터 모델

PostgreSQL은 인시던트, 승인, 작업 상태의 기준 저장소다. 아래 DDL은 마이그레이션의
설계 계약이다. 애플리케이션은 psycopg v3 비동기 연결과 역할별 연결 풀을 사용한다.

## DDL

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vectorscale;

CREATE TABLE incidents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id text NOT NULL UNIQUE CHECK (
        char_length(event_id) <= 256 AND event_id ~ '^[A-Za-z0-9:._-]+$'
    ),
    namespace text NOT NULL CHECK (namespace = 'target-apps'),
    pod_name text NOT NULL,
    pod_uid text NOT NULL,
    container_name text,
    incident_type text NOT NULL CHECK (
        incident_type IN ('OOMKilled', 'CrashLoopBackOff', 'Http5xxSpike', 'KubernetesWarning')
    ),
    observed_at timestamptz NOT NULL,
    signal jsonb NOT NULL CHECK (jsonb_typeof(signal) = 'object'),
    status text NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'NEEDS_REVIEW', 'APPROVED')),
    approved_by text,
    approved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (status = 'APPROVED' AND approved_by IS NOT NULL AND approved_at IS NOT NULL)
        OR (status <> 'APPROVED' AND approved_by IS NULL AND approved_at IS NULL)
    )
);

CREATE TABLE investigation_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid NOT NULL UNIQUE REFERENCES incidents(id),
    suspected_cause text NOT NULL CHECK (length(btrim(suspected_cause)) > 0),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'array'),
    citations jsonb NOT NULL CHECK (jsonb_typeof(citations) = 'array'),
    limitations jsonb NOT NULL CHECK (jsonb_typeof(limitations) = 'array'),
    recommended_yaml_patch text,
    patch_unavailable_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(recommended_yaml_patch, patch_unavailable_reason) = 1)
);

CREATE TABLE runbook_chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_kind text NOT NULL CHECK (source_kind IN ('RUNBOOK', 'POSTMORTEM')),
    source_uri text NOT NULL,
    source_revision text NOT NULL,
    title text NOT NULL,
    section text NOT NULL,
    chunk_no integer NOT NULL CHECK (chunk_no >= 0),
    body text NOT NULL CHECK (length(btrim(body)) > 0),
    embedding vector(384) NOT NULL CHECK (vector_norm(embedding) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_uri, source_revision, chunk_no)
);

CREATE INDEX runbook_chunks_embedding_diskann
    ON runbook_chunks USING diskann (embedding vector_cosine_ops);

CREATE TABLE investigation_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid NOT NULL UNIQUE REFERENCES incidents(id),
    state text NOT NULL DEFAULT 'QUEUED'
        CHECK (state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    stream_entry_id text,
    published_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    execution_token uuid,
    execution_expires_at timestamptz,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((stream_entry_id IS NULL) = (published_at IS NULL)),
    CHECK (
        (state = 'RUNNING' AND execution_token IS NOT NULL AND execution_expires_at IS NOT NULL)
        OR (state <> 'RUNNING' AND execution_token IS NULL AND execution_expires_at IS NULL)
    )
);

CREATE INDEX investigation_jobs_unpublished
    ON investigation_jobs (created_at, id)
    WHERE published_at IS NULL AND state = 'QUEUED';

CREATE TABLE api_idempotency (
    principal_id text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('create_incident', 'approve_incident')),
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'),
    request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
    incident_id uuid REFERENCES incidents(id),
    response_status integer,
    response_body jsonb,
    response_location text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, operation, idempotency_key),
    CHECK (
        (response_status IS NULL AND response_body IS NULL AND response_location IS NULL)
        OR (
            response_status IS NOT NULL AND incident_id IS NOT NULL
            AND response_body IS NOT NULL AND jsonb_typeof(response_body) = 'object'
            AND (
                (operation = 'create_incident' AND response_status = 202 AND response_location IS NOT NULL)
                OR (operation = 'approve_incident' AND response_status = 200 AND response_location IS NULL)
            )
        )
    )
);

CREATE INDEX api_idempotency_incident ON api_idempotency (incident_id);
```

## 상태, 중복 방지 및 권한

- 접수 트랜잭션은 인증 주체와 작업 종류에 한정된 멱등성 키를 예약하고 SHA-256 요청
  해시를 기록한다. 키가 이미 있으면 해당 행을 잠그고 해시를 먼저 비교한다. 같은 키에
  다른 요청은 409다. 같은 요청에서 성공 응답이 저장된 경우 해당 상태, 본문, Location을
  재사용한다. 실패 응답은 성공 스냅샷에 기록하지 않는다.
- 새 이벤트는 정규화된 `event_id`로 한 행만 생성한다. 신규 키가 같은 이벤트를 제출하면
  기존 티켓에 바인딩한다. 동일 `event_id`로 네임스페이스, Pod UID 등 정규화된 불변
  대상 정보가 충돌하면 409다. 관측 시간과 반복 횟수 등 가변 관측 정보는 이벤트를
  분리하는 키에 쓰지 않는다. 티켓·작업·키 바인딩은 같은 접수 트랜잭션에서 커밋한다.
- API는 [발행 절차](contracts/stream.md)에 따라 발행 완료를 확인하고, 현재 키의 202 응답을
  기록한 뒤 반환한다. 발행 중에 키를 갱신하는 트랜잭션과의 교착을 피하기 위해
  접수 단계는 멱등성 행을 잠근 채 별도 발행 트랜잭션을 대기하지 않는다.
  발행 표시는 남았지만 현재 키의 응답이 미기록이면 재시도에서
  티켓 ID와 접수 시점의 고정 값으로 동일한 접수 응답을 구성한다.
- 워커는 진단 결과의 타입과 인용 출처를 검증한 뒤 하나의 트랜잭션에서 실행 토큰을
  확인하고 티켓을 잠근다. `OPEN`인 티켓에 최초 리포트를 삽입하고
  `NEEDS_REVIEW`로 바꾸며 작업 상태를 `SUCCEEDED`로 기록한다.
- 승인 트랜잭션은 권한 확인 후 인시던트 행을 잠근다. `NEEDS_REVIEW`이고 저장된 리포트를
  대상으로 할 때만 최초 승인자와 시각을 기록하고 `APPROVED`로 전환한다. 승인 응답
  스냅샷은 상태와 같은 트랜잭션에 기록한다. 리포트는 티켓당 하나이며 승인 후에도 변경하지 않는다.
- DB 소유·마이그레이션 역할을 런타임에서 사용하지 않는다. API는 접수, 발행 표기, 승인에
  필요한 쓰기 권한과 리포트 조회 권한을 가진다. 워커는 작업 상태, 리포트 생성, 진단 상태
  전이에 필요한 권한을 가진다. MCP는 `runbook_chunks` 조회만 하며 청크 업로더의 쓰기
  권한은 별도 역할에 둔다. 교차 테이블 상태 전이는 위 트랜잭션으로 보장한다.

## Runbook 검색

- 초기 임베딩은 한국어·영어 검색에 사용할 `intfloat/multilingual-e5-small`의 384차원
  벡터다. 배포 시 모델 리비전을 고정하고, 같은 리비전과 정규화 방식으로 청크와 질의를
  생성한다. 검색에는 각각 `passage: `와 `query: ` 접두사를 사용한다.
  모델 교체 시 한 인덱스에 서로 다른 모델의 임베딩을 섞지 않는다.
  [공식 모델 카드](https://huggingface.co/intfloat/multilingual-e5-small)
- `ORDER BY embedding <=> $1::vector LIMIT $2`로 근사 후보를 구한다. 정확한 결과 정렬이
  필요하면 후보를 구체화한 뒤 거리와 ID 순으로 다시 정렬한다. 작은 검증 코퍼스에서는
  전체 벡터의 정확 검색 결과와 비교해 관련 문서가 결과에 포함되는지 확인한다. 반환 후보는
  MCP 계약의 거리 상한으로 거르며 초기 상한 0.35를 검증 자료에서 보정한다.
  [pgvectorscale DiskANN과 정렬 동작](https://github.com/timescale/pgvectorscale#order-by-vector-distance)
- 인용은 청크의 문서 URI, 리비전, 섹션, 발췌문을 리포트에 복사한다. 이후 재인덱싱해도
  승인자가 당시 사용한 근거를 확인할 수 있다.

[PostgreSQL 16 제약 조건](https://www.postgresql.org/docs/16/ddl-constraints.html) ·
[PostgreSQL 16 행 잠금](https://www.postgresql.org/docs/16/sql-select.html)

발행 복구 역할은 유실된 엔트리에 대한 만료 실행권을 해제할 수 있도록
`investigation_jobs`의 `state`, `execution_token`, `execution_expires_at`에도
열 단위 UPDATE 권한을 갖는다. Phase 2 배포에서 이 권한을 적용한다.
