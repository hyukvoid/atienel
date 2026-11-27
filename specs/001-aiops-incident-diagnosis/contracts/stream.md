# Valkey Streams 작업 계약

## 라우팅

| 항목 | 값 |
| --- | --- |
| 스트림 | `aiops:incidents:v1` |
| 소비자 그룹 | `aiops-workers-v1` |
| 소비자 이름 | `<pod-name>:<pod-uid>` |
| 생산자 | `aiops-api` 및 DB 미발행 작업 복구 루프 |
| 소비자 | `aiops-worker` |
| 전달 보장 | 최소 한 번 전달, PostgreSQL의 `job_id` 기준 결과 멱등성 |

JSON은 Valkey 엔트리의 `payload` 필드 하나에 UTF-8로 저장한다. 최대 2 KiB이며 아래
스키마를 적용한다. DB 조회가 원본 신호의 권위 있는 경로다. 로그, 자격 증명, 문서 내용은
스트림에 넣지 않는다.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "job_id", "incident_id"],
  "properties": {
    "schema_version": {"type": "integer", "const": 1},
    "job_id": {"type": "string", "format": "uuid"},
    "incident_id": {"type": "string", "format": "uuid"}
  }
}
```

생산자는 PostgreSQL `investigation_jobs.id`를 `job_id`로 사용한다. 재발행 시 같은
페이로드를 보낸다. Valkey가 지정하는 물리 엔트리 ID와 논리 작업 UUID는 별개다.
소비자는 UUID 파싱을 실행하고 두 ID가 동일한 DB 작업 행에 속하는지도 검증해야 한다.
호환성을 깨는 계약은 새 버전 스트림과 그룹에서 시작하며 기존 그룹을 완료 후 종료한다.

## 발행과 복구

1. API는 티켓과 작업을 같은 DB 트랜잭션으로 커밋한다. 그 후 작업 행을 잠그고
   `published_at`이 없는 경우에만 비동기 `XADD aiops:incidents:v1 * payload <json>`를
   수행한다. 이 트랜잭션의 잠금 대기와 단일 Valkey 호출에 짧은 상한을 둔다
   (초기값 각각 150ms). 한도를 넘으면 실패를 반환하고 복구에 맡긴다.
2. `XADD` 성공 직후 엔트리 ID와 발행 시각을 저장한다. API는 현재 멱등성 키에 대한
   성공 스냅샷도 기록한 후 커밋하고 `202`를 반환한다. 이미 발행된 티켓은 새
   `XADD` 없이 같은 티켓에 대한 접수 응답을 기록할 수 있다.
3. 발행 실패나 응답 유실 시 요청에는 `503`을 반환할 수 있다.
   별도 비동기 복구 루프는 미발행 DB 작업을 `FOR UPDATE SKIP LOCKED`로 한 건씩
   잠그고 동일한 발행 절차를 실행한다. 같은 이벤트의 HTTP 재시도도 미발행 작업을
   완료할 수 있다. 동시 생산자는 같은 작업 행의 잠금으로 직렬화된다.
4. `XADD`는 성공했지만 DB 표시가 커밋되지 않으면 Valkey 엔트리가 중복될 수 있다.
   같은 `job_id`가 재사용되며, 소비자는 실행권과 결과를 DB로 검증한다.
5. Valkey는 PVC와 AOF를 사용하고 큐 키에 TTL이나 eviction을 적용하지 않는다.
   Valkey 데이터 유실이 의심되면 미완료 작업과 엔트리의 존재를 대조한다.
   작업이 만료된 실행권을 갖고 있거나 대기 중이고 엔트리가 사라졌으면 상태를 대기 중으로
   복구하여 같은 `job_id`의 발행을 재시도한다. 재점검과 상태 변경은 작업 잠금 내에서 한다.

[PostgreSQL 16 행 잠금](https://www.postgresql.org/docs/16/sql-select.html) ·
[Valkey XADD](https://valkey.io/commands/xadd/) ·
[Valkey 지속성](https://valkey.io/topics/persistence/)

## Consumer Group과 ACK

1. 초기화 시 `XGROUP CREATE aiops:incidents:v1 aiops-workers-v1 0-0 MKSTREAM`을
   실행한다. 이미 그룹이 존재하는 오류는 초기화 완료로 처리한다. `0-0`을 써서
   그룹 생성 전에 발행된 작업도 읽는다.
2. 각 소비자는 비동기로
   `XREADGROUP GROUP aiops-workers-v1 <consumer> COUNT 1 BLOCK 2000 STREAMS aiops:incidents:v1 >`
   를 호출한다. 프로세스당 진단 실행은 기본 1개다. 종료 신호에는 신규 요청을 중단하고
   진행 중인 작업의 완료 또는 다음 소비자의 인계를 허용한다.
3. 작업 행을 잠그고 실행 자격을 확인한다. 이미 `SUCCEEDED` 또는 `FAILED`면 ACK한다.
   발행 표시가 아직 없거나 차례를 기다리는 상태면 엔트리를 PEL에 둔다. 실행할 때에는
   시도 횟수를 올리고 `RUNNING`과 실행 토큰, 만료 시각을 커밋한 뒤 외부 도구를 호출한다.
   초기 실행 제한은 90초, 실행권은 120초다. 임대가 만료되면 남은 시도가 있는 작업만
   새 토큰으로 인계하고, 횟수가 소진된 경우 실패를 커밋한 뒤 ACK한다.
4. 리포트 저장과 `NEEDS_REVIEW` 전이 및 작업 성공을 커밋한 후
   `XACK aiops:incidents:v1 aiops-workers-v1 <entry-id>` 한다.
   완료 트랜잭션은 현재 토큰과 만료 시각을 검사한다. 이미 만료됐거나 인계된 워커는
   결과를 덮어쓰거나 미완료 엔트리를 ACK할 수 없다.
5. 일시 실패는 토큰을 해제하고 다음 실행 가능 시각을 설정한 뒤 ACK 없이 PEL에 둔다.
   백오프 초기값은 5초, 15초, 45초이며 실제 재시도는 PEL 회수 시점 이후다. 최대 네 번의
   실행 시도 또는 복구 불가능한 도구 실패 시 작업을 DB에서 `FAILED`로 기록하고 ACK한다.
   티켓은 `OPEN`를 유지한다. 종료된 작업과 실패 코드는 운영 조회와 수동 재처리에 남긴다.
6. 5초마다 `XAUTOCLAIM aiops:incidents:v1 aiops-workers-v1 <consumer> 10000 <cursor> COUNT 10`
   로 PEL을 회수한다. 응답의 다음 커서를 사용하고 `0-0`으로 끝나면 다음 주기에 다시
   시작한다. 최근에 PEL을 회수했더라도 유효한 DB 실행권은 유지한다. 여러 물리 엔트리가
   같은 작업을 가리켜도 DB 토큰으로 실행을 제어한다.
   실패 제한은 Valkey 전달 카운트 대신 실제 DB 실행 시도 횟수에 적용한다.
7. 해석할 수 없는 페이로드나 잘못된 DB 대상은 별도 `aiops:incidents:invalid:v1` 스트림에
   원본 엔트리 ID와 오류 코드만 기록하고 성공한 뒤 ACK한다. 격리 기록은 엔트리 ID로
   중복 판정할 수 있다. 업무 실패의 원장은 DB의 `FAILED` 상태다.
8. 정상 스트림의 크기는 단일 그룹에서 처리 완료한 엔트리만 ACK 후 삭제하여 제어한다.
   ACK와 삭제 사이의 실패는 완료된 엔트리가 남는 방향으로만 발생해야 한다. 미처리
   메시지를 잃을 수 있는 일괄 `MAXLEN` 트림은 사용하지 않는다. 큐 적체, 오래된 PEL,
   발행되지 않은 작업 및 `FAILED`를 운영 지표로 노출한다.

[Valkey XGROUP CREATE](https://valkey.io/commands/xgroup-create/) ·
[Valkey XREADGROUP](https://valkey.io/commands/xreadgroup/) ·
[Valkey XAUTOCLAIM](https://valkey.io/commands/xautoclaim/) ·
[Valkey XACK](https://valkey.io/commands/xack/)
