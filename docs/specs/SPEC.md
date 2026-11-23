# feedback_rules v0.5 구현 기준

> 구현 기준 원문: [`spec_v0.5.md`](spec_v0.5.md). 이 문서는 코드베이스 조사 결과, 구현 의존 순서와 핵심 수용 조건을 요약한다. 원문과 충돌하면 원문이 우선한다.

## 목표 및 범위

`config.toml` 또는 선택적 규칙 파일에 선언된 `[[feedback_rules.tasks]]`를 독립적으로 로드·검증하고, 각 작업을 주기와 선택 규칙에 따라 실행해 LLM 피드백을 Dynalist 결과 노드로 기록한다. 예약 스케줄러는 만들지 않으며 기존 `tutorbot run` 호출 시점에 평가한다. 운영용 `feedback validate/list/run/approve/status` 명령도 추가한다.

기존 학습·도메인 기능과 `[domains.*]` 설정은 독립적으로 유지한다. 설정이 없거나 기능이 꺼져 있으면 기존 동작에 영향이 없어야 한다. 새 결과 쓰기는 기존 SQLite outbox와 writer의 marker/reconcile 흐름을 사용한다.

## 현재 구조와 의존성

- `config.py`는 `Settings.model_validate`에서 설정 dataclass를 조립하고, TOML 파싱은 `tomllib` → `tomli` → 제한된 `_simple_toml_parse` 순으로 처리한다. 현재 fallback은 표 배열·다중 행 배열을 지원하지 않는다.
- `cli.py`가 `load_settings`, DB 마이그레이션, LLM·Dynalist 클라이언트를 조립하며 `run_pipeline`을 호출한다. `_configured_file_ids`와 기존 `cmd_run` 흐름은 여러 테스트에서 사용된다.
- `pipeline.py`는 전역 `FileLock` 아래 문서를 `NodeIndex`로 읽고, 학습·도메인 단계를 실행한 다음 `flush_outbox`로 쓰기를 수행한다. feedback은 도메인 단계와 쓰기 flush 사이에 넣고, 쓰기 결과 상태는 flush 뒤 동기화해야 한다.
- `NodeIndex`는 Dynalist 트리·부모·형제 순서·epoch millisecond 시각을 제공한다. 기존 `Classifier.is_bot`은 부모 상속을 재귀로 계산하므로 새 피드백 경로에서는 반복식 판정을 구현하고 `classify.py`를 수정하지 않는다.
- `writer.enqueue_op`는 `op_key` 중복을 막고, `flush_outbox`는 이전 시도에서 marker로 insert를 reconcile하며 성공한 봇 노드를 `bot_node`에 등록한다. `LLMClient`는 JSON 검증·재시도·호출 기록을 제공하므로 그대로 재사용한다.
- 기존 회귀 기반은 `tests/test_domains.py`, `tests/test_m2.py`(outbox/reconcile), `tests/test_m3.py`~`test_m5.py`, `tests/fakes.py`다. `pyproject.toml`에는 pytest 외 lint 명령이 정의되어 있지 않다.

## 입출력 인터페이스 및 데이터 구조

### 입력·설정

- 입력: TOML `[feedback_rules]` 전역 옵션과 `[[feedback_rules.tasks]]`; 각 작업은 선택 소스, 참조 소스, 출력 위치, 주기, 단위, 페르소나 및 토큰 범위를 가진다.
- 소스 해석은 `NodeIndex`, registry 기반 `Classifier.is_direct_bot`, 봇 작성 시각 표지를 사용한다. 결과는 문서 순서의 선택 노드, 참조 텍스트, 토큰 적합 여부로 정규화한다.
- 설정 오류는 `ConfigIssue(code, severity, path, message, disables_task)`로 수집하고 작업 단위로 격리한다. 정규화 모델은 frozen dataclass를 사용하며 Python 3.10 호환을 유지한다.
- 규칙 파일 우선순위는 CLI `--rules-file` > `TUTORBOT_FEEDBACK_RULES_FILE` > `config.toml`. 페르소나 경로는 설정 파일 디렉터리를 기준으로 제한한다.

### 처리·저장

- 인터페이스: `run_feedback_stage(conn, settings, llm, indices, *, T, run_id, dry_run, ...)`, `build_units(...)`, `finalize_after_flush(...)`. 스케줄 판정은 주기 키와 `run/not_due/already_done/skip` 결과를 낸다.
- 시간 필터는 초 단위로 고정한 `T`와 `[하한, T)`를 사용한다. `created_since_last_run`/`modified_since_last_run`은 직전 실행 기록을 경계로 하며, 고정 길이 24h/7d 창은 실행 간격에 따라 누락·중복될 수 있다.
- `unit × placement` 네 조합은 설정된 선택 노드 또는 부모의 직접 자식에 결과를 추가한다. 사용자 노드 수정·삭제·이동은 금지한다.
- DB migration `003_feedback_rules.sql`은 `feedback_task_run`, `feedback_task_lock`, `feedback_output`, `feedback_persona_approval` 테이블을 추가한다. `(task_id, cycle_key)` 및 outbox `op_key`의 UNIQUE 보장을 유지한다.
- `feedback_output`은 검증된 LLM 출력, outbox 참조 및 target의 텍스트 hash/유효 수정 시각 snapshot을 보관한다. `write_op`은 기존 writer의 marker-first note와 crash reconcile을 재사용한다.

## 파일별 생성/수정 계획

### 신규

- `src/tutorbot/feedback/`: `models.py`, `loader.py`, `schedule.py`, `timefilter.py`, `sources.py`, `tokens.py`, `prompt.py`, `persona.py`, `output.py`, `store.py`, `service.py`, `commands.py`, `__init__.py`.
- `src/tutorbot/db/migrations/003_feedback_rules.sql`, `src/tutorbot/llm/prompts/feedback_rule.md`.
- `tests/data/feedback_rules_example.golden.json`, `tests/tools/gen_feedback_toml_golden.py`; `tests/test_feedback_{config,toml_fallback,tags,schedule,timefilter,sources,store,persona,runner,cli,pipeline}.py`.

### 수정

- `src/tutorbot/config.py`: 규칙 설정 모델·로더와 fallback TOML 표 배열/다중 행 배열 지원. 미지원 구문은 `feedback_rules` 범위에서만 `RulesParseFailure`로 격리.
- `src/tutorbot/parse/tags.py`: 새 본문+노트 태그 추출 및 정규화 헬퍼만 추가. 기존 `extract_tags`/`has_tag` 의미는 보존.
- `src/tutorbot/llm/schemas.py`: `FeedbackRuleOut` 검증 추가.
- `src/tutorbot/pipeline.py`: feedback 문서 추가 읽기, Stage 12b 및 flush 뒤 상태 확정, 통계·partial 반영. 기존 단계와 lock/flush 구조는 유지.
- `src/tutorbot/cli.py`: feedback 하위 명령, `run --no-feedback`, fake LLM 응답, stats 및 doctor 전용 문서 ID 헬퍼 추가.
- `config.example.toml`, `tests/fakes.py`: 예시와 테스트용 부모 modified 옵션만 보완.
- `docs/templates/feedback-rules.example.toml`, `docs/how_to_use.md`: 명세의 M6 운영 문서 갱신만 시행하고 템플릿을 바꾸면 golden도 같은 변경에서 갱신.

### 보존 제약

`engine/*`, `dynalist/*`(writer/tree 포함), `llm/client.py`, `llm/rate_limit.py`, `parse/classify.py`, `db/schema.sql`, 기존 마이그레이션, `pipeline.FileLock`, `cli._configured_file_ids`, `pyproject.toml`은 수정하지 않는다. `[domains.*]` 설정·실행과 기존 테스트 의미를 보존한다. 각 작업 단계 전후의 기존 사용자 변경을 덮어쓰거나 무관한 파일을 수정하지 않는다.

## 구현 의존 순서

1. **M1 설정 기반**: models/loader → fallback parser·golden → 설정 통합·태그 헬퍼 → 페르소나 파일 안전 로딩 → 오프라인 `validate`.
2. **M2 시간·저장**: DB migration/store → schedule/timefilter. 노드 트리나 service에 의존하지 않는 단위 테스트부터 완료.
3. **M3 소스**: 반복식 봇 판정, 선택·참조·컨텍스트 렌더링, 테스트 fake 확장 및 깊은 트리·작성 marker 보정 검증.
4. **M4 실행 핵심**: 토큰 추정·축약, prompt/schema/output → `run_feedback_stage`, `build_units`, outbox 적재 및 flush 후 확정.
5. **M5 통합·운영**: 승인 정책, commands/CLI, 단독 `feedback run`, pipeline Stage 2/12b/13b 통합, stats/doctor/`--no-feedback`.
6. **M6 문서·운영 검증**: 템플릿/사용 문서 갱신과 dry-run부터 실제 운영 리허설까지 수행.

각 마일스톤에서 해당 단계와 선행 단계의 테스트 및 전체 기존 스위트를 통과한 뒤 다음 단계로 진행한다.

## 엣지 케이스 및 검증 기준 (Acceptance Criteria)

- Python 3.10에서 `tomli` 없이 템플릿 전체를 fallback 파싱하고 golden JSON과 정확히 일치시킨다. 설정 오류는 격리하고 `[domains.*]`는 기존 검증을 유지한다.
- 주기별 경계, 시간대/DST, 윤년·월말, 이미 실행된 주기, 잘못된 연간 날짜·분기 anchor 및 `since_last_run` 경계가 spec과 일치한다. skip 결정은 gate 실패와 무관하게 실행 이력과 안전한 오류 로그를 남긴다.
- 태그/시간/scope 필터, references 비대상 처리, 봇 및 봇 하위 트리 제외, 깊이 3000 노드에서의 반복식 순회가 정확하고 재귀 오류가 없다.
- 네 가지 unit/placement 결과가 올바른 부모의 마지막 직접 자식이 된다. 빈 selection은 LLM·쓰기 없이 skipped로 기록되며 결과 노드는 다음 입력에서 제외된다.
- `input_token_range.max` 초과는 경고·표시 후 호출을 계속하고, 축약 후 안전 상한 초과만 해당 단위를 실패시킨다. 출력 검증/정제, 호출 한도, 개인정보 비노출을 검증한다.
- `op_key` 중복과 marker reconcile로 응답 유실 후에도 결과가 중복 생성되지 않는다. 대상 부모 삭제 등 쓰기 실패는 실행 결과에 반영되며 사용자 노드는 변경되지 않는다.
- approval, dry-run, CLI 종료 코드(0/1/2/3/4/75), 읽기 전용 명령의 DB 비생성·무마이그레이션, 기존 `run`의 부분 실패·종료 의미를 명세대로 검증한다.

### 검증 명령

```bash
pytest tests/test_feedback_*.py -q
pytest -q
python tests/tools/gen_feedback_toml_golden.py  # TOML 파서가 있는 환경에서 golden 생성/갱신 시
```

lint는 프로젝트 설정에 정의되어 있지 않다. 필요 시 운영 검증은 실제 Dynalist 쓰기 전에 `tutorbot feedback validate --online`, `tutorbot feedback run --dry-run --fake-llm` 순으로 한다.
