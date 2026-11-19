# 일상·커리어 도메인의 최근 변경분 분석 명세

## 목표 및 범위

`life` 도메인과 `career` 도메인이 `life` 문서를 source로 읽을 때, 전체 문서 대신 `run_date` 기준 최근 24시간 이내에 생성되거나 수정된 사용자 노드와 그 해석에 필요한 최소 부모 문맥만 Gemini에 전달한다. `career`가 work/life source를 함께 읽는 경우 work source는 기존처럼 전체 사용자 기록을 유지하고, life source에만 증분 필터를 적용한다. `work` 자체의 입력은 기존처럼 전체 사용자 기록이다.

`personal_tasks`는 `_personal_tasks`의 상태·진행기록 파싱, DB 추적 및 분석 입력 구조를 그대로 유지하며 날짜 기반 노드 필터를 적용하지 않는다. 튜터봇 피드백 노드 및 그 하위 트리는 life 증분 입력과 컨텍스트 모두에서 제외한다. 다른 도메인/파이프라인 단계, Dynalist 쓰기·outbox 동작은 변경 범위가 아니다.

## 사전 조사: 현재 구조와 의존성

- `pipeline.run_pipeline`은 `r_date`를 결정하고, study 및 도메인 source 문서의 `doc_read` 결과를 `NodeIndex`로 변환해 `indices`에 보관한다. `process_domains(..., run_date=r_date)`로 이미 실행 날짜를 전달한다.
- `engine.domains.process_domains`는 `_profile_sources`로 source 목록을 정하고 `_source_content`에서 문서 텍스트를 생성한다. career의 source는 설정된 활성 source domain 파일들이고 target은 career 프로필 파일이다. 모든 일반 도메인은 현재 `index.text_of("root", skip=classifier.is_bot)`로 전체 기록을 취한다.
- `Node`는 Unix epoch 밀리초 `created`/`modified`, content/note 및 자식 ID를 보유한다. `NodeIndex`는 parent 관계, 순회, `text_of`를 제공한다. `Classifier.is_bot`은 DB의 `bot_node` registry를 근거로 봇 노드를 판별한다.
- 일반 도메인의 `source_hash`는 현재 구성된 `source_text` 전체의 SHA-256이며 target별 `domain_state` 중복 방지 키로 쓰인다. 빈 텍스트는 Gemini 호출 없이 warning/skip 처리한다.
- `_personal_tasks`는 별도 파서와 task별 hash를 통해 분석 대상 블록 및 개인 과업 상태를 관리한다. 이 흐름은 증분 텍스트 생성 경로와 분리되어 있다.
- 관련 회귀 테스트는 `tests/test_domains.py`; 파이프라인에서 날짜 전달과 source 문서 read 범위는 해당 파일의 pipeline 테스트로 확인한다. 테스트용 Dynalist 트리는 `tests/fakes.py`와 `NodeIndex`로 만든다.

## 입출력 인터페이스 및 데이터 구조

### 기간 산출

- 기간은 `run_date`의 Asia/Seoul 자정에서 시작한다. `cutoff = datetime.combine(date.fromisoformat(run_date), time.min, ZoneInfo("Asia/Seoul")) - timedelta(days=1)`.
- Dynalist의 created/modified epoch 밀리초를 Asia/Seoul-aware 시각으로 해석한다. 사용자 노드는 `created >= cutoff` 또는 `modified >= cutoff`이면 변경 노드다. cutoff와 정확히 같은 시각은 포함한다. `run_date=2026-11-20`의 cutoff는 `2026-11-19 00:00:00 Asia/Seoul`이다. 수집된 문서는 실행 시점 snapshot이므로 별도 상한은 두지 않는다.
- 누락/0 timestamp는 유효한 최근 변경으로 간주하지 않는다. 둘 다 cutoff보다 오래된 노드는 변경 대상이 아니다.

### life 증분 텍스트

- 필터는 `domain_id == "life"`에 적용한다. `career` 처리 중 source domain이 `life`인 파일에도 똑같이 적용한다. 이를 위해 career source 목록의 각 `file_id`가 활성화된 life 프로필에 속하는지 판정한다. 동일 파일이 life와 work 양쪽 설정에 중복되어 source로 들어오는 경우, life 성격의 개인정보를 보호하도록 life 증분 범위만 한 번 포함하며 work 전체 텍스트와 중복 병합하지 않는다.
- 최근 변경 노드는 `classifier.is_bot(node)`인 노드와 그 하위 subtree를 제외한 트리에서 판정한다. 이 제외는 생성 피드백 registry의 `kind`/bot 식별 정보에 기반하며, 분류된 봇 노드가 부모인 자식은 timestamp가 최근이어도 입력하지 않는다.
- 변경 노드 자체의 content/note 및 그 자손 subtree는 기존 `text_of`와 같은 들여쓰기·표현 규칙으로 포함한다. 변경 노드가 부모이고 그 아래에 오래된 자손이 있으면 해당 subtree가 변경 노드의 의미에 속하므로 포함한다. 중복/중첩 변경 노드는 가장 위쪽 변경 노드만 subtree rendering 시작점으로 삼아 중복 텍스트를 막는다.
- 변경 노드까지의 조상 중 봇 노드가 아닌 조상은 경로상의 content/note만 최소 context로 포함한다. 조상들의 다른 자식 가지는 포함하지 않는다. root는 synthetic node이므로 출력하지 않는다. 오래된 sibling/subtree 및 이전 1년치 기록은 포함하지 않는다.
- 구조상 최근 변경 노드가 하나도 없으면 텍스트는 빈 값이다. 이에 따라 해당 target의 Gemini 호출은 하지 않고 `domain_run` warning과 report skipped/warning을 남긴다. career의 life source가 변경 없음이어도 다른 source인 work에 기록이 있으면 work 텍스트만으로 기존 규칙대로 career 분석 가능하다. 모든 source를 합친 최종 텍스트가 비면 호출하지 않는다.

### 변경하지 않는 입력

- `work` 일반 도메인과 career의 work source는 기존 전체 문서 출력(`text_of("root", skip=classifier.is_bot)`)이다.
- `personal_tasks`는 `_personal_tasks`의 반환 텍스트, task hash, pending hash 반영, 경고 및 DB 상태 처리를 그대로 유지한다. 최근 변경 노드만 골라 task subtree를 자르는 동작은 금지한다.
- life 이외 source/domain 입력은 현재 동작을 유지한다.

### Hash와 로그

- 일반 도메인의 `source_hash` 및 `domain_state` 중복 방지는 기존 알고리즘/저장 구조를 유지하되, life 문서에서 만드는 `source_text`는 부모 문맥과 변경 노드의 증분 텍스트만으로 구성한다. 그 결과 해시는 전체 life 문서가 아니라 실제 Gemini에 넣는 하루 증분 텍스트의 SHA-256이다. career는 work 전체 텍스트와 life 증분 텍스트를 합친 실제 프롬프트 입력에 대해 기존 방식으로 hash를 계산한다.
- source가 여러 개면 문서 경계 표식(`## file_id`)도 기존대로 해시 및 프롬프트 입력에 포함한다.
- life source에 대해 `domain_id`, `source_id`, `target_id`, cutoff/기간 또는 변경 노드 수, 결과(`started`/`skipped`/`failed`/`succeeded`)를 기록한다. 로그는 ID·개수·기간만 남기며 사용자 텍스트, 프롬프트 전문, Gemini 결과, credential은 남기지 않는다. career 로그에서는 source별 집계가 구분되어야 한다.
- 변경 없음은 빈 입력 warning/skip으로 구별한다. career의 work source가 있어 career 분석을 수행했더라도 life 변경 없음 경고와 source별 변경 노드 수를 관측 가능하게 기록한다.

## 파일별 생성/수정 계획

| 파일 | 계획 |
|---|---|
| `src/tutorbot/engine/domains.py` | Seoul cutoff 계산 및 `Node.created`/`modified` 기반 life 증분 텍스트 helper 추가. 봇 노드/subtree 제외, top-level changed subtree 및 조상 경로만 rendering. `_source_content`에 도메인/source 역할 또는 증분 여부를 전달해 life에만 적용하고, personal_tasks 분기는 먼저 유지. career source별 life 판별 및 source별 변경 노드 수/기간 로그 추가. 빈 증분만 있는 target은 기존 warning/skip 방식으로 LLM 호출 없이 기록. source hash는 구성된 실제 증분 prompt text에 대해 계산. |
| `src/tutorbot/pipeline.py` | 변경 불필요: 이미 확정한 `r_date`를 도메인 처리에 전달하고 source 파일들을 indices에 읽는다. 테스트에서 날짜 전달 및 life source read가 보장되는지만 확인. |
| `src/tutorbot/dynalist/tree.py` | 가능하면 범용 NodeIndex API를 새로 늘리지 않고 domains helper에서 `parent_of`, `children`, `by_id` 사용. 구현상 트리 rendering의 중복/skip 방지가 복잡해지고 재사용이 명확해질 때만 최근 변경 subtree/경로 선택용 순수 helper를 추가하되, 기존 `text_of` 동작은 바꾸지 않는다. |
| `tests/test_domains.py` | cutoff 경계, created만/modified만 최근인 노드, 오래된 노드, 조상만 문맥화, sibling 제외, nested changed subtree 중복 방지, 봇 피드백 및 subtree 제외, 빈 life 입력 skip/no LLM, source hash가 증분 텍스트 기반임을 검증. career의 work 전체+life 증분 혼합과 source별 로그를 검증. work 전체 입력 및 personal_tasks 기존 구조 회귀 테스트 유지. |
| `tests/fakes.py` | 기존 `add(..., created=..., modified=...)` 및 `_tick` 인자로 충분한지 확인. 테스트 편의를 위한 확장만 필요할 때 수행하며 실제 동작 코드 변경은 하지 않는다. |
| `docs/SPEC.md` | 본 명세. |

## 엣지 케이스 및 검증 기준 (Acceptance Criteria)

1. `run_date=2026-11-20`에서 cutoff는 `2026-11-19 00:00 Asia/Seoul`이며 그 순간과 이후 created/modified 노드는 포함, 그 이전만 해당하는 노드는 제외된다.
2. created만 최근 또는 modified만 최근인 노드는 각각 포함된다. 둘 다 timestamp가 0/누락/기한 전이면 제외된다.
3. 변경 노드의 조상 경로 content/note는 포함하지만 조상의 unrelated sibling 가지는 포함하지 않는다. 부모가 변경 노드면 subtree를 한 번만 포함하고 중첩 변경 노드로 중복 출력하지 않는다.
4. 튜터봇 피드백 노드 및 그 하위 노드는 날짜와 무관하게 제외된다. 같은 문서의 다른 사용자 변경 노드는 정상 포함된다.
5. life에 최근 변경이 없으면 해당 source는 빈 증분으로 처리되고 Gemini를 호출하지 않으며 skipped와 warning을 남긴다. career의 work source에 텍스트가 있으면 work만으로 분석할 수 있고 life 변경 없음이 로그에서 식별된다.
6. work source 전체 기록과 personal_tasks의 기존 task/status/progress 구조는 유지된다. life 전용 날짜 필터가 다른 도메인 입력에 누출되지 않는다.
7. life의 저장 `source_hash`는 하루 증분 프롬프트 텍스트를 기준으로 한다. 오래된 비관련 기록만 수정/추가해도 hash와 Gemini 입력이 바뀌지 않으며 증분 입력이 바뀌면 hash가 바뀐다. career hash는 전체 work + life 증분 실제 입력을 반영한다.
8. 로그에는 life `source_id`, `target_id`, cutoff 또는 기간/변경 노드 수 및 결과가 표시되며 사용자 기록/LLM 결과 전문은 포함되지 않는다.
9. focused 검증: `pytest tests/test_domains.py`; 전체 회귀: `pytest`. Gemini 호출 여부는 fake backend 호출 수로 검증하고 외부 API 호출은 필요하지 않다.
