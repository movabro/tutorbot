# 현재 작업: 피드백 규칙 M4~M6

## 목표 및 범위

기준 명세는 [v0.5 §23](docs/specs/spec_v0.5.md#23-승인된-운영-확장과-m4m6-실행-명세)이다. 기존 도메인을 유지하면서 승인된 마지막 성공 기준, 업무 슬롯, 주간 종료와 career root 출력을 추가한다. 아래 과거 작업의 도메인 수정 계획은 이번 작업에 적용하지 않는다.

## 입출력 인터페이스 및 데이터 구조

다섯 feedback task와 페르소나, success 기반 시간 필터, 고정 업무 슬롯, 주 종료 옵션 및 career note 출력은 기준 명세 §23.2~23.3을 따른다. 기존 시간 필터와 일반 주간 일정은 불변이다.

## 파일별 생성/수정 계획

기준 명세 §23.5의 M4→M5→M6 순서를 따른다. 금지된 writer·tree·도메인·study·LLM client·classifier와 `pipeline.FileLock`은 수정하지 않는다.

## 엣지 케이스 및 검증 기준 (Acceptance Criteria)

기준 명세 §23.4 및 §23.6의 크래시 복구, 승인, dry-run, 동시성, 네 출력 조합과 전체 회귀 검증을 모두 통과해야 한다. career 문서 ID 대소문자 불일치와 실제 선택 범위는 운영 전 확인이 필요하다.

---

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

# Devlog 실행 로그 및 코멘트 피드백 명세

## 목표 및 범위

`devlog`를 설정 가능한 비학습 도메인으로 추가하고, `tutorbot run`의 실행 로그와 devlog 문서의 사용자 코멘트를 같은 Dynalist 문서에서 관리한다.

- 설정 대상 문서 ID: `TZ9DVqlTaO5vYR825vdSkhq0`
- 실행 로그: 날짜 노드 `!(YYYY-MM-DD)` 아래 `HH:mm:ss` 노드를 만들고, 해당 실행 구간의 `logs/tutorbot.log` 신규 기록 전체를 시간 노드의 note에 저장한다.
- 코멘트 피드백: 사용자가 작성한 코멘트 각각을 `domain_feedback` 작업으로 분석하고 자식 피드백 노드를 생성한다.
- 실행 로그 노드와 자동 피드백은 `bot_node`에 각각의 bot kind로 등록해 사용자 코멘트 분석 대상에서 제외한다.
- 기존 일반 도메인 분석, `run`, `--dry-run`, outbox 동작을 유지한다. devlog Dynalist 쓰기는 실제 실행에서만 한다.
- 재실행·재시도 시 op_key와 기존 bot_node/marker reconciliation으로 같은 실행 로그 및 코멘트 피드백의 중복 생성을 방지한다.
- 코드 변경 범위의 설계만 정의한다. DB 스키마 추가는 우선 요구하지 않는다.

## 현재 구조 및 의존성

- `config.py`의 `_default_domains()`가 허용 도메인 이름을 고정한다. 현재 모르는 도메인은 설정 검증에서 거부된다.
- `cli.py`가 `logs/tutorbot.log`를 구성하고 `cmd_run()`에서 실행 요약을 로깅한다. `_configure_logging()`은 기존 핸들러를 교체하며 파일 로그는 append 모드이다.
- `pipeline.py`가 활성 도메인 파일을 읽고, `process_domains()`를 호출한 뒤 실제 실행에서 `flush_outbox()`를 실행한다. dry-run은 DB 복사본과 pending op 요약만 사용한다.
- `engine/domains.py`는 도메인 프로필의 입력을 `domain_feedback` LLM 작업으로 분석하고, `domain_state`, `domain_run`, `write_op`에 기록한다. 현재 일반 도메인 처리는 문서 단위 상태 키를 사용하므로 코멘트 단위 dedupe는 별도 식별이 필요하다.
- `writer.py`는 UNIQUE `write_op.op_key`, marker 기반 insert reconciliation, insert 성공 후 `bot_node` 등록을 제공한다. marker는 실제 Dynalist 노드 note의 첫 줄에 들어간다.
- `NodeIndex`와 `Classifier`는 문서 트리 및 bot subtree 판별에 재사용 가능하다.

## 입출력 인터페이스 및 데이터 구조

### 설정

- `Settings.domains`에 `devlog` 프로필을 등록한다. `config.toml`은 `enabled = true`, `file_ids = ["TZ9DVqlTaO5vYR825vdSkhq0"]`를 설정한다.
- `config.example.toml`에는 동일한 도메인 구조와 설명을 추가하되, 실제 개인 문서 ID 대신 placeholder를 사용한다.
- devlog는 일반 도메인 프로필 허용 목록 및 pipeline의 문서 수집에는 포함하되 기존 source 문서 요약 대상에서는 제외한다. 댓글 처리와 실행 로그 저장은 devlog 전용 흐름으로 다룬다.

### Dynalist 노드

- 날짜 노드: root 직계 자식, content `!(YYYY-MM-DD)`, kind `devlog_run_date`.
- 실행 노드: 날짜 노드의 자식, content `HH:mm:ss` (24시간제, Asia/Seoul), kind `devlog_run_log`. note는 고정된 실행 로그 marker 한 줄 뒤에 해당 실행 구간 로그 전체를 원문 그대로 보존한다. marker를 제외한 로그 부분의 줄·문자열을 잘라내거나 요약하지 않는다.
- 피드백 노드: 사용자 코멘트의 자식, kind `domain_feedback`. content는 짧은 요약과 `!(YYYY-MM-DD)`를 포함하고, note는 구체적인 피드백을 담는다. writer marker가 note 첫 줄에 추가되며 실제 피드백 본문은 그 뒤에 보존한다.
- 자동 날짜/실행/피드백 노드와 각 자동 노드의 하위 트리는 `bot_node` 등록 및 `Classifier`를 통해 사용자 입력에서 제외한다. kind는 용도를 식별할 수 있도록 고정하고, run 로그 kind와 `domain_feedback` kind를 서로 구분한다.

### 중복 키와 상태

- 실행 로그 op_key는 devlog file ID, 날짜, `HH:mm:ss` 실행 노드 식별 정보를 사용한 결정적 값으로 만든다. 날짜 노드 op_key는 file ID와 날짜를 사용한다. 날짜 노드가 이미 문서에 있으면 재사용하고, 없으면 시간 노드 op가 날짜 노드 op를 참조하도록 한다.
- 기존 outbox UNIQUE 제약과 marker reconciliation을 사용한다. 응답 유실 후 재실행하면 동일 marker의 노드를 reconcile하고 추가 insert를 하지 않는다.
- 코멘트 입력은 봇이 아닌 사용자 코멘트 노드의 content/note를 정규화해 해시한다. 코멘트 ID와 source hash를 포함한 결정적 op_key를 사용한다. 동일 코멘트·동일 hash의 완료 상태는 재호출하지 않는다. 기존 `domain_state(domain_id, file_id)`를 쓸 경우 코멘트 ID별 domain key로 구분하고, source hash가 바뀐 경우에만 재분석한다.
- 코멘트 피드백 marker는 도메인, 코멘트 node ID, source hash를 포함해 서로 다른 코멘트/버전 간 reconcile 오인을 방지한다. `bot_ref_id`는 SQLite 정수형이므로 Dynalist node ID를 그대로 담지 않는다. 코멘트 ID는 op_key/domain key로 식별한다.
- 피드백 노드가 성공적으로 등록된 후에는 `bot_node`를 기준으로 후속 실행에서 자동 생성 노드로 제외한다. 새 코멘트의 판정 정책은 실행 시점 문서에서 비봇 노드를 대상으로 하며, 초기 활성화에서 오래된 사용자 기록까지 무제한 분석하지 않도록 `run_date`/기존 domain 정책에 맞는 신규·변경 범위를 명시적으로 제한한다.

## 파일별 생성/수정 계획

### `src/tutorbot/config.py`

- `_default_domains()`에 `devlog` 프로필을 추가해 설정 검증에서 허용한다.
- 별도 도메인 설정 필드를 추가하지 않고 기존 `enabled`, `file_ids` 구조를 재사용한다.

### `src/tutorbot/cli.py`

- 실제 `cmd_run()`에서 최종 설정의 log path를 적용한 뒤 실행 구간의 시작 위치(byte offset 또는 동등한 안정적 경계)를 기억한다.
- 실행 요약까지 기록·flush한 후 시작 위치부터 현재 EOF까지의 신규 로그를 UTF-8로 읽어 pipeline/devlog 처리에 전달한다. 설정 로딩 이전의 다른 실행이나 기존 파일 내용은 포함하지 않는다.
- 로그 기록이 비어 있거나 파일이 없거나 읽기에 실패한 경우를 기록하고 실행을 조용히 중단하지 않도록 처리한다. 로그 본문을 콘솔 출력 또는 예외 메시지에 중복 노출하지 않는다.
- `--dry-run`에서는 devlog에 대한 Dynalist write op 생성·flush를 하지 않는다. 기존의 다른 dry-run 동작과 임시 DB 사용은 그대로 둔다.

### `src/tutorbot/engine/domains.py`

- 코멘트 단위 입력 추출/해시/LLM 처리를 추가한다. `Classifier.is_bot()`이 true인 노드와 그 subtree, 설정된 bot tag/marker가 있는 노드는 제외한다.
- 사용자 코멘트별로 기존 `domain_feedback` prompt와 `_validate_feedback()`를 재사용한다. 결과 content는 길이를 제한한 단일 줄 요약 + `!(run_date)`로 만들고, note에는 feedback 항목과 evidence를 보존한다.
- 코멘트마다 독립된 domain state 처리와 결정적 op_key/marker를 사용한다. 같은 source hash의 processed 상태라면 LLM 호출과 enqueue를 건너뛴다.
- 실행 로그를 쓸 때도 도메인 전용 helper를 두어 날짜 노드와 시간 노드를 outbox에 enqueue한다. 날짜 노드를 탐색할 때 날짜 content만으로 임의의 사용자가 작성한 노드를 봇 노드로 간주하지 말고, 기존 bot_node 등록 또는 고유 marker를 함께 확인한다.
- 다중 file ID 설정도 지원하며 각 문서별로 독립적인 op_key와 상태를 사용한다.

### `src/tutorbot/pipeline.py`

- 현재 활성 도메인 파일 ID 수집으로 devlog 문서를 읽는다.
- 일반 `process_domains()`에서 devlog를 제외하고, 전용 코멘트/실행로그 처리는 기존 outbox 처리 및 dry-run 경계와 연결한다.
- 실행 로그 캡처 텍스트와 실행 날짜/시간을 전용 처리에 전달한다. 로그 노드 쓰기 및 코멘트 피드백 쓰기는 실제 run의 outbox 흐름에 연결해 기존 중복 실행 lock과 보고 통계를 유지한다.
- run 시작 전 잔여 op flush는 유지한다. 실행 로그의 최종 텍스트가 완성되는 시점과 outbox 처리 순서를 정해 현재 run에서 생성된 로그 전체가 누락되지 않도록 한다.

### `src/tutorbot/dynalist/writer.py`

- 기존 enqueue, marker, crash-reconciliation, bot_node 등록 기능을 재사용한다.
- devlog 로그/피드백의 `bot_kind` 등록과 marker가 실제 재시도에서도 node kind에 맞게 보존되는지 확인한다. 기존 marker-only 매칭이 동일 parent 아래의 서로 다른 op를 구별할 수 있도록 각 marker에 날짜·시간 또는 comment ID/source hash를 포함한다.
- 변경이 필요한 경우에만 marker와 content/parent를 함께 검증하도록 `_reconcile_insert()`를 좁게 보강한다. 일반 outbox 동작이나 다른 도메인 작업의 중복 방지를 변경하지 않는다.

### `config.toml`, `config.example.toml`

- 실제 설정에 `[domains.devlog]`, `enabled = true`, `file_ids = ["TZ9DVqlTaO5vYR825vdSkhq0"]`를 추가한다.
- 예시 설정에는 `[domains.devlog]`와 placeholder ID를 추가한다.

### 테스트

- `tests/test_cli_logging.py`: 실행 구간 시작 offset, 기존 로그 제외, 신규 기록 전체 전달, 예외/빈 로그 처리, dry-run에서 devlog 미기록을 검증한다.
- `tests/test_domains.py`: devlog 설정 파싱, 날짜/시간 계층, note payload, 시간 형식, user comment별 Gemini `domain_feedback`, summary/date 및 구체 feedback note, bot subtree 제외, 동일 hash 재실행 skip을 검증한다.
- `tests/test_m2.py`: devlog insert outbox의 bot kind 등록, marker reconciliation 및 응답 유실 후 중복 노드 없음, 날짜 노드 재사용과 시간 노드 고유성을 검증한다.
- 필요하면 `tests/test_domains.py`의 pipeline 테스트를 확장해 devlog 문서가 읽히고 기존 study/work/life/career 처리에 영향을 주지 않는지 확인한다.

## 엣지 케이스 및 검증 기준 (Acceptance Criteria)

### 설정과 실행 경계

- `Settings.model_validate()`가 devlog를 수락하고 설정된 ID가 pipeline 읽기 목록에 정확히 한 번 포함된다.
- 동일 ID가 다른 도메인에도 설정돼 있으면 문서 read는 dedupe되고 devlog write routing은 devlog 설정에 따라 동작한다.
- 이전 로그 파일 내용은 해당 run 기록에 포함되지 않고, run 시작 경계 이후의 모든 로그 레코드는 누락·절단 없이 note에 포함된다.
- `run --dry-run` 및 실패한 설정 로딩은 devlog 문서를 수정하지 않는다. 실제 `run`만 Dynalist write를 수행한다.

### 트리와 payload

- 날짜 노드 content가 정확히 `!(YYYY-MM-DD)`이고 시간 노드가 그 날짜 노드의 자식이며 content는 `HH:mm:ss` 형식이다.
- 실행 시간 노드 note에 marker 이후 실행 중 새로 생성된 로그 전체가 포함된다.
- 날짜 노드가 이미 있으면 중복 날짜 노드를 만들지 않는다. 같은 날짜·시간의 재시도/재실행은 중복 시간 노드를 만들지 않는다.
- 코멘트 피드백 노드는 해당 사용자 코멘트 아래에만 생성되고, content는 개행 없는 짧은 요약과 실행 날짜를 포함하며, note는 구체 피드백/evidence를 보존한다.
- bot kind에 등록된 날짜·실행·피드백 노드와 그 subtree는 Gemini 입력에 들어가지 않는다. 사용자가 작성한 코멘트만 별도 입력으로 전달된다.

### 멱등성/복구

- 같은 코멘트 ID와 source hash를 재실행해도 추가 LLM 호출 또는 추가 피드백 op가 없다.
- 동일한 실행 로그 op_key가 다시 enqueue되면 새 op를 만들지 않는다.
- Dynalist 응답 유실 후 writer가 marker로 기존 날짜/시간/피드백 노드를 reconcile하고 중복 insert를 하지 않는다.
- 서로 다른 코멘트 ID 또는 실행 시각의 marker/op_key가 충돌하지 않는다.
- devlog write 실패는 기존 run 결과/오류 보고에 반영하며, 실패를 숨기거나 성공으로 표시하지 않는다.

### 테스트 실행 기준

- 우선: `pytest tests/test_cli_logging.py tests/test_domains.py tests/test_m2.py -q`
- 회귀 확인: `pytest -q`
- 프로젝트에 적용되는 lint를 수행했다면 해당 검사도 통과해야 한다.

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
