# 도메인 피드백 노드 배치 변경 명세

## 목표 및 범위

다음 Dynalist 문서와 동작을 변경한다.

- work 문서 `dRd00TIrXZgv54KNNVvmsaYV`: 사용자가 새로 생성하거나 최근 수정한 사용자 노드 각각의 자식에 피드백 노드를 생성한다.
- life 문서 `Mf-0fwM1mE5IzRP4_pzlF1KE`: 사용자가 최근 24시간 안에 생성하거나 수정한 사용자 노드 각각의 자식에 피드백 노드를 생성한다.
- career 문서 `BKHTXLOAfsJSxmyAOcUvyZx7`: 사용자가 직접 최근 생성/최근 수정한 사용자 노드가 있으면 해당 노드 각각의 자식에 피드백을 만든다. 직접 노드가 없거나 일반 분석 경로로 실행된 경우에는 career target의 루트 직계 자식 중 기존 분석 대상이 될 수 있는 마지막 사용자 노드의 자식에 피드백을 만든다.
- 기존 career의 work 전체 분석 + life 24시간 증분 분석 조합과 life의 24시간 증분 정책은 유지한다.
- 기존 봇이 만든 노드와 그 하위 트리는 재분석 및 피드백 중첩 대상에서 제외한다.
- `domain_state.source_hash`와 `write_op.op_key`의 결정적 중복 방지 및 재시도 reconcile 동작을 깨뜨리지 않는다.

범위 밖: `writer.py`와 `tree.py`의 외부 프로토콜 변경, Dynalist API 스키마 변경, 새로운 테이블 추가, source/study 파이프라인 변경은 요구사항이 아니며 필요한 최소 수정만 허용한다.

## 현재 구조와 의존성

### `src/tutorbot/engine/domains.py`

- 프로필별 문서 목록을 `settings.domains`에서 읽는다.
- `_profile_sources()`는 work/life를 각각 자기 문서의 source로 쓰고, career는 설정된 `source_domains`의 work/life 문서를 합친다.
- `_source_content()`는 domain이 life이거나 career의 life source인 경우 `_life_incremental_text()`로 24시간 증분을 만들고, 그 외 work/career 문서는 전체 텍스트를 사용한다.
- `_life_incremental_text()`는 이미 봇 하위 트리를 제외하고 최근 변경 노드 및 관련 조상 경로를 렌더링한다.
- 분석 성공 시 단일 insert `write_op`를 현재 구현에서는 `node:root` 아래에 추가한다.
- `_validate_feedback()`는 `summary`, `feedback`, `evidence`를 검증하며 도메인별 프롬프트와 결합한다.
- `source_hash`는 분석에 실제로 기여한 `source_text`의 SHA-256이고, 동일 문서의 동일 소스에 대해 `domain_state`에서 중복 실행을 막는다.

### `src/tutorbot/dynalist/writer.py`

- `enqueue_op()`는 `op_key` UNIQUE 규칙으로 중복 insert를 막는다.
- crash recovery는 아직 미완료 op의 `attempts > 0`인 경우 `_reconcile_insert()`를 실행한다.
- insert의 현재 reconcile 기준은 marker 또는 `parent_id` 아래의 미등록 동명 content이다.
- insert payload의 `content`와 `note`는 각각 Dynalist 노드의 content/note로 전송된다.
- marker가 있으면 writer가 Dynalist `note` 앞에 marker를 추가한다.
- 성공 시 `bot_node(file_id, node_id, kind, ref_id, op_key)`를 등록하고 `domain_state.feedback_json` 및 각 domain run 상태를 갱신한다.

### `src/tutorbot/dynalist/tree.py`

- `NodeIndex`는 root와 전체 노드의 관계, 형제 순서, 최근 변경 timestamp 조회에 필요한 문서 트리를 제공한다.
- `walk()`, `children_of()`, `ancestors()`, `max_modified()`는 기존 위치 탐색과 증분 필터 구현에 재사용한다.
- 별도 계층 테이블이나 API는 필요 없으며, 추가가 필요하면 배치 대상 탐색용 읽기 헬퍼 수준으로 제한한다.

### 기존 테스트

- `tests/test_domains.py`는 career source 조합, life 증분, work/life/career source hash, 봇 하위 트리 제외를 검증한다.
- `tests/test_m2.py`는 outbox, crash reconciliation, marker, 중복 방지 동작을 검증한다.

## 입출력 인터페이스 및 데이터 구조

### 입력

- `process_domains(..., indices, settings, llm, run_id, run_date)`의 기존 인터페이스를 유지한다.
- work/life/career target은 모두 `NodeIndex`로 읽는다.
- work/life/career 소스는 기존 `_profile_sources()`와 `_source_content()` 규칙을 유지한다.
- 사용자 노드 판별에는 기존 `Classifier.is_bot()`을 사용하며 다음을 모두 배제한다.
  - `bot_node(deleted_at IS NULL)`에 등록된 노드
  - `#tutorbot` 또는 설정 봇 태그가 있는 노드
  - `tb:`로 시작하는 marker가 있는 노드
  - 위 노드의 모든 하위 트리

### 최근 노드 필터

- 기준 시각은 `run_date`의 Asia/Seoul 자정에서 24시간을 뺀 기존 `cutoff`이다.
- 최근 판정은 기존 life 정책과 동일하게 `created >= cutoff_ms || modified >= cutoff_ms`이다.
- work/life/career의 각 target에서 봇 판별 후 최근 사용자 노드만 배치 대상 후보로 한다.
- work는 기존 career용 전체 분석 소스 정책을 유지하며, 자기 문서 피드백 배치는 위의 work target 최근 노드 정책을 사용한다.
- life는 `_life_incremental_text()`로 소스 텍스트를 만들고, 자기 문서 피드백 배치도 최근 노드만 사용한다.

### 피드백 노드 payload

insert payload는 기존 JSON 구조를 유지하되 content는 한 줄의 요약 제목으로 만들고 note는 구체적 피드백과 근거로 구성한다.

- `content`: `🤖 {domain_id} 피드백: {짧은 summary} !(YYYY-MM-DD) #tutorbot`
  - 한 줄이며 개행을 포함하지 않는다.
  - `summary`는 공백을 정리하고 짧은 제목으로 제한한다.
  - 날짜는 `run_date`의 정확한 `YYYY-MM-DD` 형식을 사용한다.
- `note`: 구체적인 피드백 항목과 근거를 저장한다.
  - 예시 형태: `피드백: ...\n근거: ...`
  - `result["feedback"]`의 항목은 구체적으로 보존한다.
  - `result["evidence"]`가 있으면 `근거: ` 한 줄로 결합한다.
  - 예외적으로 evidence가 없는 경우에는 근거 없는 노트도 허용하되, content의 피드백 제목은 유지한다.
- marker는 `tb:domain_feedback:{domain_id}:{source_hash[3:19]}` 또는 노드별 marker suffix를 포함한 결정적 값으로 별도 필드에 둔다.

기존 writer가 marker를 payload의 `note` 첫 줄에 prepend하므로 domain 구현은 payload 자체를 실제 응답 근거만 담는 순수 텍스트로 유지하고 reconcile marker는 별도 `marker` 필드에 둔다.

### 배치 규칙

1. work/life/career 공통으로 분석 성공 후 배치 대상 노드 수를 계산한다.
2. work/life:
   - target의 최근 사용자 노드마다 insert op를 한 개 생성한다.
   - 각 insert의 `parent_ref`는 `node:<node_id>`이고 자식 위치는 `idx=-1`이다.
   - 각 노드마다 payload를 동일하게 만들되, op_key와 marker는 같은 노드를 식별하는 결정적 suffix를 사용한다.
   - 여러 노드가 있으면 노드별로 각각 하나의 op를 만들고 단일 노드에 중첩하지 않는다.
   - 단순히 현재 root 아래로 추가하는 기존 동작은 최근 노드가 없을 때 work/life에는 사용하지 않는다.
3. career:
   - target에서 최근 사용자 노드가 있으면 그 노드 각각을 배치 대상으로 사용한다.
   - target에 직접 최근 노드가 없거나 일반 분석 경로로 판단된 경우에는 root 직계 자식 중 마지막 사용자 노드의 자식으로 배치한다.
   - root 직계 자식이 모두 봇 노드이거나 사용자가 남긴 노드가 없으면 synthetic root 자체의 자식으로 배치하는 fallback을 허용한다.
   - career도 여러 직접 노드가 존재하면 노드별로 각각 한 개의 피드백 자식을 만든다.

## 파일별 생성/수정 계획

### `src/tutorbot/engine/domains.py`

- `_validate_feedback()`는 유지하되, title 제한 용도의 요약 정규화 helper를 별도로 추가할 수 있다.
- `_life_incremental_text()`는 유지하고 재분석이 일어나도 봇 하위 트리를 포함하지 않도록 기존 Classifier 사용을 유지한다.
- 다음 읽기 helper 중 하나를 추가한다.
  - `_recent_user_nodes(index, cutoff, classifier)` 또는 동급: `walk()`에서 봇이 아닌 노드만 필터하고 `created/modified`를 cutoff 이상으로 필터한다.
  - `_career_fallback_parent(index, classifier)`: root 직계 자식 역순에서 봇이 아닌 마지막 노드를 찾고 없으면 `"root"`를 반환한다.
- work/life/career의 target 배치용 op 생성을 기존 단일 root insert 분기에서 교체한다.
- work/life는 배치 대상이 비어 있으면 피드백 insert op를 만들지 않는다. 기존 life의 no-analyzable-source 분기는 유지하고, work는 일반 전체 source가 있어도 직접 변경 노드가 없으면 새로운 write를 만들지 않는다.
- career는 직접 최근 노드가 있으면 노드별 배치, 없으면 fallback 배치를 사용한다.
- 배치 op의 기존 `domain_state.source_hash`와 `domain_run` 갱신은 성공한 분석당 한 번만 유지한다.
- 여러 insert를 하나의 source_hash 아래 enqueuing할 때도 SQLite transaction 안에서 일관되게 원자성을 유지한다.
- op_key는 기존 형식에 배치 대상 node id 또는 stable ordinal을 추가해 노드별로 고유하게 만든다.
  - 예: `domain-feedback:{domain_id}:{target_id}:{source_hash}:{parent_node_id}`
  - 동명 단일 노드가 중복 source_hash로 재분석되지 않는 것은 기존 이전 상태 검사로 유지한다.
- marker도 배치 노드별로 unique하거나 최소한 source hash와 domain을 포함하도록 결정적이어야 한다.
- 기존 `source_hash`가 같고 `status='processed'`이면 LLM 재실행 및 새 write op 생성을 피한다.

### `src/tutorbot/dynalist/writer.py`

- 신규 코드는 최소화하되, `_reconcile_insert()`가 payload의 실제 content와 marker 양쪽을 정확히 검증하도록 유지한다.
- 노드별 op의 marker가 서로 다르면 crash recovery가 다른 피드백 노드를 오인식하지 않는다.
- `_apply_domain_done()`의 `kind="domain_feedback"` 등록 동작은 유지한다.
- 단일 파일의 여러 insert가 `_reconcile_insert()`와 `_build_change()`로 차례 반영될 때도 marker-first note와 child index 규칙을 깨뜨리지 않는다.
- 기존 `bot_node` 등록은 성공한 각 result_node_id에 대해 유지되어 다음 run의 `_source_content()`와 Classifier가 해당 subtree를 다시 봇으로 판단한다.
- source_hash/write_op 중복 방지를 위해 기존 `enqueue_op()`의 op_key UNIQUE 처리와 `_reconcile_insert()`의 이전 attempt reconcile을 재사용하며 재해석하지 않는다.

### `src/tutorbot/dynalist/tree.py`

- 현재 제공되는 `walk()`, `children_of()`, `ancestors()`, `parent_of`로 요구사항 구현이 가능하므로 기본적으로 구조 변경은 불필요하다.
- 변경이 필요하면 최근 노드 및 fallback 부모를 찾는 읽기 전용 helper를 추가하되, 반환 타입은 기존 `Node` 또는 node id로 제한한다.
- 문서 수정, 봇 등록, write queue 변경은 이 파일이 아니라 domains/writer에서 처리한다.

### `tests/test_domains.py`

- work/life/career 분기별로 배치 대상 개수와 부모 id를 검증한다.
- work/life의 최근 사용자 노드별 피드백 op가 각각 생성되는지 검증한다.
- life의 24시간 cutoff 및 기존 source_hash 규칙이 유지되는지 검증한다.
- career의 work 전체/life 증분 조합이 유지되는지 검증한다.
- career 직접 노드가 여러 개면 노드별 자식 op를 각각 만들고, 직접 노드가 없으면 root 마지막 사용자 노드 아래를 검증한다.
- payload의 content에 summary와 `!(YYYY-MM-DD)`가 한 줄로 있는지 검증한다.
- payload의 note에 feedback과 evidence가 구체적으로 포함되는지 검증한다.
- 기존 봇 노드 및 봇 하위 트리는 recent-node 후보와 소스 텍스트 양쪽에서 제외되는지 검증한다.
- 기존 단일 source hash 재실행 시 op가 추가로 생기지 않는지 검증한다.

### `tests/test_m2.py`

- outbox가 domain feedback의 노드별 parent_ref를 정상 적용하는지 검증한다.
- `marker`가 포함된 insert crash recovery가 기존 노드로 reconcile되어 1건만 남는지 검증한다.
- 서로 다른 대상 노드에 대한 여러 insert가 중복 생성되지 않는지 검증한다.
- marker 포함 payload의 `note`와 payload 자체의 구체적 feedback/note가 document에 정상 저장되는지 검증한다.

## 엣지 케이스 및 검증 기준 (Acceptance Criteria)

### 배치

- work에 두 개의 최근 사용자 노드가 있으면 write_op도 두 개이며, 각각의 parent가 해당 노드 id와 일치한다.
- life에 두 개의 최근 사용자 노드가 있으면 노드별 피드백 자식을 각각 만든다.
- life의 오래된 노드와 기존 봇 노드는 24시간 변경 후보와 source text 모두에 포함되지 않는다.
- career에 두 개의 직접 최근 사용자 노드가 있으면 각각 자식 피드백을 하나씩 만든다.
- career에 직접 노드가 없고 root 직계 자식이 `[user1, bot1, user2]`라면 마지막 직계 사용자 노드 `user2` 아래에 피드백을 만든다.
- career에 직접 노드도 사용자 root 자식도 없으면 synthetic root 아래 피드백 insert로 fallback한다.
- 기본 career 분석과 직접 career 노드 분석을 구분하지 못하면 일반 분석 fallback 조건에 빠지지 않고 직접 노드 수만큼 배치한다.

### payload

- 피드백 content는 개행 없는 짧은 단일 제목이며 `!(YYYY-MM-DD)`를 정확히 포함한다.
- 피드백 note는 `result["feedback"]` 항목과 evidence 근거를 포함한다.
- bot_kind은 `domain_feedback` 또는 기존 명명과 호환되는 값으로 유지한다.
- 기존 bot_node가 이미 등록된 노드나 marker가 `tb:`인 노드는 재분석 대상에 넣지 않는다.

### 중복 방지 및 복구

- 동일한 `domain_id + target_id + source_hash`의 processed 상태에서는 새 LLM 호출이나 새 write_op를 만들지 않는다.
- 첫 분석 실패 후 같은 hash로 재실행하면 `domain_state.status='error'`를 다시 processed로 전환할 수 있다.
- 노드별 write_op의 op_key가 UNIQUE하므로 같은 노드와 hash에 대한 재enqueue는 새 row를 만들지 않는다.
- crash 후 response lost로 attempts가 증가한 op는 `_reconcile_insert()`로 기존 marker 노드를 찾고 중복 insert를 만들지 않는다.
- 여러 노드가 있을 때 한 노드의 insert가 reconcile되더라도 나머지 노드의 pending op를 별도로 처리한다.

### 소스 정책

- life는 cutoff 이전 노드만 있는 경우 LLM을 호출하지 않고 no-analyzable-source warning 또는 기존 equivalent 상태를 유지한다.
- career는 work 전체 텍스트와 life 최근 증분 텍스트를 결합한다.
- career의 life source에 최근 변경이 없으면 기존 warning과 work-only 분석을 유지한다.
- work 단독 분석은 기존 전체 work 텍스트 분석과 별개로 work 대상 최근 노드에 배치된다.

### 최종 테스트 기준

다음 전체 검증이 통과할 때까지 구현을 종료한 것으로 판단하지 않는다.

- `pytest tests/test_domains.py tests/test_m2.py -q`
- 관련 전체 스위트가 필요하면 `pytest -q`
- ruff 또는 프로젝트에 정의된 lint를 실행한 경우 해당 검사가 통과한다.

## 재작업 조건

다음 중 하나라도 발생하면 구현을 수정하고 위 테스트 기준을 다시 실행한다.

- work/life/career 중 하나라도 피드백을 root에 일괄 삽입하거나 단일 노드에만 중첩하는 회귀가 있다.
- 서로 다른 최근 노드의 피드백 payload 또는 parent가 섞인다.
- payload `content`에 요약과 date가 한 줄로 있지 않거나 payload `note`에 구체적 피드백/evidence가 빠진다.
- 기존 봇 노드 또는 하위 트리가 recent-node 후보, life 소스, career fallback 후보로 다시 포함된다.
- 동일 source_hash 재실행에서 추가 LLM 호출 또는 추가 write_op가 발생한다.
- 노드별 op_key가 충돌하거나 marker가 다른 노드로 reconcile되는 잘못된 중복 방지가 발생한다.
- crash/retry 이후 같은 노드에 피드백 노드가 두 개 이상 생긴다.
- life 24시간 정책 또는 career의 work 전체 + life 증분 조합이 깨진다.
- 모든 관련 테스트가 green이 되기 전에 구현 완료를 선언한다.

---

# Study 문서 ID 설정 이전 — 변경 지점 및 테스트 계획

## 목표 및 범위

학습 문서 ID의 설정 위치를 `[dynalist].file_ids`에서 `[domains.study].file_ids`로 이전한다. 설정 로더와 pipeline은 legacy ID 및 `DYNALIST_FILE_ID` fallback을 사용하지 않는다. CLI 문서 존재 검사는 study 문서와 활성 도메인 문서를 대상으로 한다.

코드베이스는 `pyproject.toml` 기반 Python 패키지이며, 설정 로딩은 `config.py`, 문서 선택은 `pipeline.py`, CLI 검사는 `cli.py`가 담당한다. 현재 doctor만 Dynalist 문서 존재를 확인하고, init-db에는 원격 존재 검사가 없다.

## 입출력 인터페이스 및 데이터 구조

- 실제 설정의 study ID `x5hNxn1nrQ5xKAiRuziVNoUh`는 `[domains.study].file_ids`에 둔다.
- `[dynalist]`에는 `token_env`만 둔다. `DYNALIST_FILE_ID` fallback과 legacy `[dynalist].file_ids`를 지원하지 않는다.
- CLI 검사 목록은 설정된 study 문서 및 활성 도메인 문서의 고유 ID 목록이다.

## 파일별 생성/수정 계획

- `src/tutorbot/config.py`: `DynalistCfg.file_ids` 및 환경변수 fallback 제거, legacy 키 미지원 처리.
- `src/tutorbot/pipeline.py`: study 처리 목록에서 `domains.study.file_ids`만 사용하고 legacy 합산 제거. 다른 활성 도메인 및 career source 수집은 유지.
- `src/tutorbot/cli.py`: 공통 문서 목록/존재 검사 기준을 doctor와 init-db에 적용. init-db에는 현재 없는 원격 문서 검사를 추가.
- `tests/test_domains.py`: 새 설정 구조, legacy/env 제거, study만 학습 처리하는 동작을 검증.
- CLI 테스트(`tests/test_client_db.py`, 필요 시 `tests/test_llm_client.py`): 두 명령의 문서 검사 범위와 누락 문서 처리를 검증하고 legacy doctor fixture를 갱신.
- `config.toml`: 실제 ID를 `[domains.study].file_ids`로 이동.
- `config.example.toml`, `docs/how_to_use.md`: legacy 설정/fallback 설명을 study 도메인 설정으로 교체.

## 엣지 케이스 및 검증 기준 (Acceptance Criteria)

- study 문서는 study 설정으로만 학습 처리되며, legacy ID가 추가 문서로 합산되지 않는다.
- `DYNALIST_FILE_ID`만 설정해도 study ID가 생기지 않고, legacy `[dynalist].file_ids` 입력은 지원되지 않는다.
- init-db와 doctor는 study 및 활성 도메인 문서를 검사하고 중복 ID를 한 번만 검사한다. 비활성 비-study 도메인은 제외한다.
- 설정 예시와 사용 설명에 legacy file ID 또는 `DYNALIST_FILE_ID` fallback 설명이 남지 않는다.
- Focused 테스트: `pytest tests/test_domains.py tests/test_client_db.py tests/test_llm_client.py -q`
- 전체 테스트: `pytest -q`
