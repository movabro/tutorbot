# 개인교사봇 기획서 v0.2 (상세 기술 명세)

> v0.1 초안을 보완한 문서입니다. 이 문서만 보고 `@worker`가 구현을 시작할 수 있는 수준을 목표로 합니다.
> 규범적 표현: **MUST**(필수) / **SHOULD**(권장) / **MAY**(선택).
> 각 장에 있는 "v0.1 대비 변경"은 초안에서 무엇이 바뀌었는지 표시합니다.

---

## 0. 요약 (v0.1 대비 핵심 변경)

| # | 항목 | v0.2 결정 |
|---|------|-----------|
| 1 | 변경 감지 | 문서 단위 "변경 없음 → 종료" 분기를 **폐기**. 매 배치마다 문서를 1회 읽고(`doc/read`) **학습 단위(노드 하위 트리)별 해시**로 변경을 판정 |
| 2 | 봇 노드 식별 | `#tutorbot` 태그는 **최상위 봇 노드에만** 붙임. 실제 식별은 DB의 `bot_node` 등록부 + 태그 + note 마커 순으로 판정 |
| 3 | 퀴즈 식별자 | 화면의 `[Q123]`은 **`quiz_attempt.id`(출제 인스턴스)**. 노드와 attempt의 매핑은 DB의 `quiz_attempt.node_id`가 정본(正本) |
| 4 | 쓰기 신뢰성 | **Outbox 패턴**(`write_op` 테이블) + 결정적 `op_key` + note 마커로 재시도 시 중복 쓰기 방지 |
| 5 | 노드 키 | Dynalist `node_id`는 **문서 안에서만 유일**함. 모든 노드 참조는 `(file_id, node_id)` 복합 키 |
| 6 | LLM 호출 | 공부 1건당 **매핑 + 피드백 + 카드 생성을 한 번의 호출로 합침**. 개념 목록은 압축 인덱스로 전달하고, 개념이 많으면 검색으로 후보를 줄임 |
| 7 | 개념 상태 | LLM은 상태를 **제안**만 하고, 실제 전이는 엔진의 규칙(§7.4)으로 결정. ⭐ 숙달은 SRS 근거로만 부여 |

---

## 1. 목적 · 전제 (v0.1 유지)

- 기능: ① 진도 체크(커리큘럼 대비 갭) ② 공부 기록 피드백 ③ SRS 복습 퀴즈
- 전제: 사용자 1명, 입력과 출력은 모두 Dynalist, 하루 1회 배치, 웹 UI 없음
- 비목표(MVP 제외): 실시간 반응, 텔레그램/웹 응답, 다중 사용자, 커리큘럼 재생성 명령, 로컬 LLM

---

## 2. 결정 사항 (v0.1 §8에 대한 권장 결정안)

### D1. 퀴즈 응답 방식 → **(A) Dynalist 안에서 응답**
- 사용자는 퀴즈 항목 노드 **아래에 자식 노드로** 답을 적음. 봇이 쓰지 않은 자식이면 모두 답으로 간주하고, 여러 줄이면 하위 트리 전체를 이어 붙임.
- 답이 `?`, `모름`, `pass`, `패스` 중 하나와 정확히 같으면(대소문자·공백 무시) **LLM을 호출하지 않고 0점** 처리.
- **미응답 처리**:
  - 미응답 attempt는 `open` 상태로 남고 **다시 출제하지 않음**(노드도 그대로 둠).
  - 미응답 attempt는 그날의 출제 한도 N에 포함됨. 그래서 쌓인 퀴즈가 무한정 늘지 않음.
  - 출제 후 `quiz_expire_days`(기본 3일)가 지나도 답이 없으면 `expired` 처리. 노드 제목 끝에 `⌛`를 붙이고 `checked=true`로 바꿈. **SRS에는 반영하지 않음**(카드의 due는 그대로이므로 나중에 새 attempt로 다시 나올 수 있음).
  - 같은 카드가 연속 3회 만료되면 due를 +7일 미룸(무시하는 카드가 계속 나오는 것을 막음).
- 이유: 입출력 채널을 하나로 유지하면 구현할 부분이 가장 적음. 하루 1회 배치이므로 실시간 채널은 필요 없음.

### D2. 수정된 공부 노드 → **다시 분석하고 기존 피드백 노드를 덮어씀**
- 해시가 바뀌면 다시 분석함. 단, 마지막으로 처리한 원문과 비교한 정규화 텍스트 유사도(`difflib.SequenceMatcher.ratio`)가 **0.95 이상이면 사소한 수정**으로 보고 해시만 갱신함(LLM 호출 없음).
- 다시 분석한 결과는 **같은 피드백 노드를 수정(edit)**해서 반영함. 새 노드는 만들지 않음. 이전 피드백은 DB의 `feedback` 테이블에 버전으로 남김.
- 카드는 `(entry, concept)` 조합 중 **아직 카드가 없는 개념만** 새로 만듦.
- 정착(settle) 규칙: 하위 트리의 최대 `modified`가 현재로부터 `settle_minutes`(기본 60분) 안이면 이번 배치에서는 처리하지 않고 미룸. 작성 중인 노드에 피드백이 달리는 것을 막음.
- 이유: "추가"를 고르면 피드백 노드가 계속 늘어나고, "무시"를 고르면 내용을 보강한 공부가 반영되지 않음.

### D3. 날짜 구조 → **둘 다 지원하고 우선순위를 고정**
1. 공부 노드 **자체**(content나 note)에 있는 `!(YYYY-MM-DD…)`
2. **가장 가까운 조상 노드** 중 날짜가 있는 노드. 조상 노드의 날짜는 `!(YYYY-MM-DD…)` 또는 content 시작 부분의 `YYYY-MM-DD` / `YYYY.MM.DD` / `YYYY/MM/DD`로 인식
3. 노드의 `created`(ms epoch)를 설정 시간대(기본 `Asia/Seoul`)의 날짜로 변환한 값
- `study_entry.date_source`에 `self|ancestor|created` 중 어디서 날짜를 얻었는지 기록함.
- v0.1은 부모 날짜를 우선했지만, 노드 자체에 적힌 날짜가 더 구체적이므로 1순위로 올림.

### D4. 목표 개수 → **스키마는 다중 목표, 운영 규칙은 단순하게**
- 각 `#목표` 노드에는 **주제 태그 1개가 필수**임(예: `#목표 #rust Rust 백엔드`). 태그가 없으면 목표 아래에 경고 노드를 쓰고 처리하지 않음.
- 활성 목표가 **1개**이면: 주제 태그가 없는 `#공부`/`#지식덤프`도 자동으로 그 목표에 연결함.
- 활성 목표가 **2개 이상**이면: 주제 태그가 **필수**임. 태그가 없는 노드에는 `🤖 주제 태그를 붙여주세요: #rust / #ml #tutorbot` 경고 자식을 한 번 쓰고 `status=skipped_untagged`로 둠. 태그가 붙으면 해시가 바뀌므로 다음 배치에서 처리됨.
- **LLM으로 목표를 추정하지 않음**(v0.1의 "태그가 없으면 LLM이 추정"은 폐기). 결정적으로 동작하고 비용이 없음.

### D5. 커리큘럼 수정 권한 → **봇이 소유하고, 사용자의 삭제만 반영**
- 커리큘럼 트리는 봇 노드임. 사용자가 이름을 바꾸거나 이동하면 다음 상태 갱신 때 봇이 덮어씀(이동은 그대로 두고 content만 덮어씀).
- 사용자가 **개념 노드를 삭제**하면 `concept.user_deleted=1`로 표시하고 진도 계산과 퀴즈 대상에서 제외함. "이 개념은 범위 밖"이라는 의사 표시로 해석함.
- 커리큘럼 **루트 컨테이너**가 통째로 사라지면 DB를 기준으로 트리 전체를 다시 만듦(`user_deleted`인 개념은 제외).
- 사용자가 개념을 추가하는 기능과 `#재생성` 명령은 v0.3으로 미룸.

### D6. LLM → **클라우드 API + 공급자 추상화 + 2단계 모델**
- `LLMClient.complete_json(task, messages, schema) -> dict` 인터페이스 하나로 감쌈. 공급자(Gemini/OpenAI/Anthropic 등)와 모델명은 설정 파일에서 지정함.
- `model_smart`: 커리큘럼 생성, 덤프 평가, 공부 분석(피드백)에 사용. `model_fast`: 채점, 주간 리포트 문장에 사용.
- JSON 스키마 강제(structured output) 기능을 지원하면 사용함. 응답은 반드시 pydantic으로 검증함.
- 로컬 LLM은 한국어 품질과 JSON 준수율 때문에 MVP에서 제외함. 추상화 계층이 있으므로 나중에 추가할 수 있음.

### D7. 배치 시각 · 퀴즈 개수 · 주기
| 항목 | 기본값 | 비고 |
|------|--------|------|
| 실행 | 매일 **04:00 Asia/Seoul**, systemd timer `Persistent=true` | 서버가 꺼져 실행을 놓치면 켜진 뒤 1회 실행 |
| `quiz_per_day` | **5** (목표별) | 미응답 attempt 포함 |
| `max_new_cards_per_day` | **2** (목표별) | 복습 카드를 우선 출제 |
| `quiz_expire_days` | 3 | D1 참고 |
| 주간 리포트 | `run_date`가 **월요일**인 배치에서 지난 ISO 주(월~일) 작성 | 월요일 실행을 놓치면 다음 실행 때 보충 |
| `min_entry_chars` | 20 | 태그를 뺀 본문이 이보다 짧으면 `too_short`로 두고 처리하지 않음 |
- 수동 실행: `tutorbot run [--dry-run] [--date YYYY-MM-DD]`. 같은 날 다시 실행해도 결과가 같아야 함(멱등, §8).

---

## 3. Dynalist API 연동 명세

### 3.1 사용할 엔드포인트
모든 요청은 `POST https://dynalist.io/api/v1/<path>` 형식이고 JSON 본문에 `"token"`을 넣음. 응답의 `_code`가 `"Ok"`가 아니면 오류로 처리함(대소문자를 무시하고 비교할 것).

| 엔드포인트 | 용도 | 요청 | 응답에서 쓰는 필드 |
|---|---|---|---|
| `file/list` | 설치 시 문서 확인(`tutorbot doctor`) | – | `files[].id,title,type` |
| `doc/read` | 매 배치 전체 트리 읽기 | `file_id` | `version`, `nodes[]` |
| `doc/check_for_updates` | **사용 안 함**(최적화용으로만 남겨둠, §6.1 참고) | `file_ids` | `versions{}` |
| `doc/edit` | 노드 쓰기 | `file_id`, `changes[]` | `new_node_ids[]` (insert 순서대로) |

> `file/edit`은 문서·폴더 단위 조작용이므로 **쓰지 않음**. 노드를 다루는 API는 `doc/edit`임.

**node 객체 필드**(`doc/read`): `id`, `content`, `note`, `created`(ms), `modified`(ms), `children`(id 배열, 순서 있음), `checked`, `checkbox`, `collapsed`, `heading`, `color`. 응답의 `nodes`는 **평평한 배열**이고 루트 노드의 id는 `"root"`임. 트리는 `children`을 따라가며 구성함.

**`doc/edit`의 change 종류**:
- `{"action":"insert","parent_id":…,"index":-1|0..,"content":…,"note":…,"checked":bool}`. `index:-1`은 맨 뒤에 넣음.
- `{"action":"edit","node_id":…,"content":…,"note":…,"checked":bool}`. 보낸 필드만 바뀜.
- `{"action":"move","node_id":…,"parent_id":…,"index":…}`
- `{"action":"delete","node_id":…}`
- ⚠️ 필드명(`parent_id` vs `parent`)은 M0에서 실제 API로 확인하고 `client.py` 한 곳에 상수로 둘 것.

### 3.2 Rate limit 대응
공식 문서 기준(대략치)은 다음과 같음. M0에서 다시 확인함.
- `doc/read`: 분당 30회, 버스트 100
- `doc/edit`: 요청 분당 약 60회, 버스트 약 50. **change 개수** 기준 분당 240개, 버스트 500

구현 규칙:
- 클라이언트 쪽 토큰 버킷을 둠(엔드포인트별 요청 수 + edit change 수). 기본값은 보수적으로 **change 분당 200개, 요청 1개당 change 최대 100개**.
- `_code == "TooManyRequests"` 또는 HTTP 429/5xx이면 지수 백오프(2, 4, 8, 16, 32초, 최대 5회)로 재시도. `InvalidToken`, `NotFound`, `NodeNotFound` 계열은 재시도하지 않음.
- 커리큘럼처럼 큰 트리는 **깊이별로 나눠서** 넣음. 부모의 `new_node_ids`를 받아야 자식을 넣을 수 있기 때문임(§8.3 의존성 해석).

### 3.3 노드 분류 규칙 (Parser)
태그 정규식: `(?<![\w#])#([^\s#!@()\[\],.;:?]+)`. content에서만 찾고 note는 보지 않음. 태그 이름은 설정에서 바꿀 수 있음.

| 분류 | 판정 |
|---|---|
| **봇 노드** | ① `bot_node`에 `(file_id,node_id)`가 등록됨, 또는 ② content에 `#tutorbot`이 있음, 또는 ③ note 첫 줄이 `tb:`로 시작함. 그리고 **봇 노드의 자손은 모두 봇 소유**임. 예외: 퀴즈 항목 노드의 봇이 아닌 자식은 "답" 영역임(§3.5) |
| 목표 | 봇 노드가 아니고 content에 `#목표` |
| 지식덤프 | 봇 노드가 아니고 content에 `#지식덤프` |
| 공부 | 봇 노드가 아니고 content에 `#공부`. 공부 노드 안에 또 `#공부`가 있으면 **가장 바깥 노드가 학습 단위**이고 안쪽 태그는 무시함(warning 로그) |
| 주제 태그 | 위 예약 태그와 `#tutorbot`을 제외한 태그 중 **활성 목표의 topic_tag와 일치하는 첫 번째 태그** |

> ⚠️ **M0 검증 항목**: Dynalist가 한글 태그(`#공부`)를 태그로 인식하는지 확인. 인식하지 않더라도 파서는 텍스트 정규식으로 찾으므로 기능은 동작하지만, Dynalist 안에서 태그 필터를 쓸 수 없음. 그럴 경우 설정으로 `#study` 같은 ASCII 별칭을 함께 허용함(`tags.study = ["공부","study"]`).

### 3.4 봇이 쓰는 노드의 형식과 위치

모든 **최상위 봇 노드**: content는 `🤖 <제목> #tutorbot`, note 첫 줄은 마커 `tb:<kind>:<ref_id>`. 하위 봇 노드에는 태그와 마커를 붙이지 않고 `bot_node`에만 등록함(화면을 깔끔하게 유지하기 위함).

| kind | 위치 | 형식 |
|---|---|---|
| `curriculum_root` | 목표 노드의 자식, index 0 | `🤖 커리큘럼 (진도 42%) #tutorbot` |
| `concept` | curriculum_root 아래, 최대 깊이 3 | 잎 노드: `[⬜] 소유권`, `[🟡]`, `[✅]`, `[⭐]`. 중간 노드: `📘 메모리 관리 (3/7)` |
| `progress_report` | 목표의 자식, index 1 | `🤖 진도 리포트 (2026-11-12) #tutorbot`. 자식: 갭 요약(≤5줄), `➡️ 다음 추천:` 개념 ≤5개 |
| `quiz_root` | 목표의 자식, index 2 | `🤖 복습 퀴즈 #tutorbot` |
| `quiz_day` | quiz_root의 자식, index 0(최신이 위) | `📅 2026-11-12` |
| `quiz_item` | quiz_day의 자식 | `[Q123] 질문 텍스트`. 123은 `quiz_attempt.id` |
| `grade` | quiz_item의 마지막 자식 | `🤖 3/5 · 코멘트 · 모범답안: …`. 채점 후 quiz_item은 `checked=true` |
| `feedback` | 공부 노드의 마지막 자식(index -1) | 헤더 `🤖 피드백 (2026-11-12) #tutorbot`. 자식 줄은 최대 5개: `✏️ 교정:`, `🔑 빠진 핵심:`, `➡️ 다음:`, `🧩 매핑: 개념A, 개념B` |
| `weekly_root` / `weekly_report` | 목표의 자식 / weekly_root의 자식(index 0) | `🤖 주간 리포트 #tutorbot` / `📊 2026-W45 …` |
| `warning` | 해당 사용자 노드의 마지막 자식 | `🤖 ⚠️ … #tutorbot` (같은 사유로는 1회만) |

- **인프라 컨테이너**(`curriculum_root`, `quiz_root`, `weekly_root`, `progress_report`)가 사라지면 **다시 만듦**.
- **콘텐츠 노드**(`feedback`, `concept`, `quiz_item`)를 사용자가 지우면 **존중함**. 다시 만들지 않고 DB에 `deleted_at`을 기록함. 단, 해당 공부 노드의 내용이 다시 바뀌어 재분석되면 피드백을 새로 만듦.

### 3.5 퀴즈 답 수집
- `open` 상태인 `quiz_attempt`마다 `(file_id, node_id)`로 노드를 찾음.
  - 노드가 없으면 `status=orphaned`(SRS에 반영하지 않음).
  - 봇이 쓰지 않은 자식의 하위 트리 텍스트를 이어 붙인 것이 답임. 비어 있으면 미응답.
- 텍스트의 `[Q123]`은 사람이 읽기 위한 표시이자 DB 복구용 보조 정보임. **정본은 DB 매핑**임.
- **중복 채점 방지**: `graded`/`expired`/`orphaned` 상태인 attempt는 수집 대상이 아님. 채점 뒤에 사용자가 답을 고쳐도 다시 채점하지 않음(MVP).

---

## 4. 하위 트리 해시 (`content_hash`)

### 4.1 정규화 함수 `norm(s)`
1. `None`은 `""`로 바꿈 → 유니코드 **NFC** 정규화
2. 줄 단위로 나눠 각 줄의 앞뒤 공백 제거. 연속 공백·탭은 공백 1개로 바꿈
3. 빈 줄 제거, `\n`으로 다시 합침
- 서식 기호(`**`, 링크 문법)와 태그는 **남김**. 태그를 붙이는 것 자체가 의미 있는 변경이기 때문임(D4).

### 4.2 직렬화 (결정적)
```
serialize(node, depth):
    if is_bot_owned(node): return ""          # 봇 노드와 그 하위 트리 전체 제외
    line = f"{depth}\x1f{norm(node.content)}\x1f{norm(node.note)}\x1e"
    return line + "".join(serialize(child, depth+1) for child in node.children_in_order)

content_hash = "v1:" + sha256(serialize(unit_root, 0).encode("utf-8")).hexdigest()
```
- **포함**: content, note, 자식의 **순서와 깊이**(구조를 바꾸면 변경으로 봄)
- **제외**: `checked`, `collapsed`, `color`, `heading`, `modified`, 봇 노드의 하위 트리 전체
- 학습 단위를 **이동**하는 것(부모가 바뀌는 것)은 해시에 영향이 없음. 날짜만 다시 계산함.
- 해시 알고리즘이 바뀔 때는 접두어 `v1:`을 올림. 저장된 해시의 버전이 다르면 **재분석하지 않고 조용히 다시 계산해 저장**함(마이그레이션 규칙).

### 4.3 적용 대상
| 단위 | 해시 범위 | 저장 위치 |
|---|---|---|
| 목표 | 목표 노드 + 사용자 자식(봇 컨테이너는 제외됨) | `goal.source_hash` |
| 지식덤프 | 덤프 노드 하위 트리 | `knowledge_dump.content_hash` / `processed_hash` |
| 공부 | 공부 노드 하위 트리 | `study_entry.content_hash` / `processed_hash` |
| 퀴즈 답 | 퀴즈 항목의 사용자 자식들 | `quiz_attempt.answer_hash` |

`content_hash`는 마지막으로 **본** 해시이고 `processed_hash`는 마지막으로 **처리에 성공한** 해시임. 처리 필요 여부는 `content_hash != processed_hash`로 판정함.

---

## 5. 데이터 모델 (SQLite DDL)

> `PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;` 사용. 시각은 UTC ISO8601 텍스트, 날짜(`*_date`)는 현지 날짜 `YYYY-MM-DD`.
> 스키마 버전은 `meta` 테이블에 두고, 마이그레이션은 `db/migrations/NNN_*.sql`을 순서대로 적용함.

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- schema_version, hash_version

CREATE TABLE goal (
  id            INTEGER PRIMARY KEY,
  file_id       TEXT NOT NULL,
  node_id       TEXT NOT NULL,
  title         TEXT NOT NULL,
  topic_tag     TEXT NOT NULL UNIQUE,         -- 예: 'rust' (# 제외, 소문자)
  level         TEXT,                          -- 목표 수준(자유 텍스트)
  deadline      TEXT,                          -- YYYY-MM-DD 또는 NULL
  status        TEXT NOT NULL DEFAULT 'pending_curriculum',
                -- pending_curriculum | active | error | failed | archived(노드 삭제됨)
  source_hash   TEXT,
  retry_count   INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(file_id, node_id)
);

CREATE TABLE concept (
  id            INTEGER PRIMARY KEY,
  goal_id       INTEGER NOT NULL REFERENCES goal(id),
  parent_id     INTEGER REFERENCES concept(id),
  name          TEXT NOT NULL,
  description   TEXT,                          -- ≤ 80자
  depth         INTEGER NOT NULL,              -- 1..3
  sort_order    INTEGER NOT NULL,
  is_leaf       INTEGER NOT NULL,
  state         TEXT NOT NULL DEFAULT 'unknown',  -- unknown|partial|understood|mastered (잎만 의미 있음)
  state_reason  TEXT,                          -- 마지막 전이의 근거 요약
  node_id       TEXT,                          -- 봇이 쓴 Dynalist 노드(file_id는 goal에서)
  rendered_text TEXT,                          -- 마지막으로 쓴 content (바뀐 것만 edit하기 위함)
  user_deleted  INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE concept_prereq (concept_id INTEGER REFERENCES concept(id), prereq_id INTEGER REFERENCES concept(id),
  PRIMARY KEY (concept_id, prereq_id));

CREATE TABLE concept_state_event (
  id INTEGER PRIMARY KEY, concept_id INTEGER NOT NULL REFERENCES concept(id),
  old_state TEXT NOT NULL, new_state TEXT NOT NULL,
  source TEXT NOT NULL,              -- dump | study | quiz | srs
  ref_id INTEGER,                    -- dump/entry/attempt id
  reason TEXT, run_id INTEGER REFERENCES run_log(id), created_at TEXT NOT NULL
);

CREATE TABLE knowledge_dump (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goal(id),
  file_id TEXT NOT NULL, node_id TEXT NOT NULL,
  raw_text TEXT NOT NULL, content_hash TEXT NOT NULL, processed_hash TEXT,
  status TEXT NOT NULL,              -- pending|processed|error|failed|skipped_untagged|deleted
  retry_count INTEGER NOT NULL DEFAULT 0, last_error TEXT, processed_at TEXT,
  UNIQUE(file_id, node_id)
);

CREATE TABLE study_entry (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goal(id),
  file_id TEXT NOT NULL, node_id TEXT NOT NULL,
  entry_date TEXT NOT NULL, date_source TEXT NOT NULL,     -- self|ancestor|created
  raw_text TEXT NOT NULL,                  -- 현재 본 원문(하위 트리 들여쓰기 텍스트, 봇 노드 제외)
  processed_text TEXT,                     -- 마지막으로 처리한 원문(D2 유사도 비교용)
  content_hash TEXT NOT NULL, processed_hash TEXT,
  max_modified INTEGER NOT NULL,           -- 하위 트리 modified 최댓값(ms), settle 판정용
  status TEXT NOT NULL,
    -- pending|processed|error|failed|deleted|skipped_untagged|too_short|deferred
  retry_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  first_seen_at TEXT NOT NULL, processed_at TEXT,
  UNIQUE(file_id, node_id)
);

CREATE TABLE entry_concept (
  entry_id INTEGER REFERENCES study_entry(id), concept_id INTEGER REFERENCES concept(id),
  coverage TEXT NOT NULL,                  -- mention|partial|full
  PRIMARY KEY(entry_id, concept_id)
);

CREATE TABLE feedback (
  id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES study_entry(id),
  based_on_hash TEXT NOT NULL, version INTEGER NOT NULL,   -- 같은 entry에서 1,2,3…
  body_json TEXT NOT NULL,                 -- LLM 출력 원본(검증 후)
  node_id TEXT,                            -- 헤더 노드. 버전이 바뀌어도 같은 노드를 재사용
  created_at TEXT NOT NULL,
  UNIQUE(entry_id, based_on_hash)
);

CREATE TABLE quiz_card (
  id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES goal(id),
  concept_id INTEGER NOT NULL REFERENCES concept(id),
  source_entry_id INTEGER NOT NULL REFERENCES study_entry(id),  -- NULL이면 SQLite UNIQUE가 동작하지 않으므로 NOT NULL
  question TEXT NOT NULL, answer_key TEXT NOT NULL,
  ease REAL NOT NULL DEFAULT 2.5, interval_days INTEGER NOT NULL DEFAULT 0,
  repetitions INTEGER NOT NULL DEFAULT 0, lapses INTEGER NOT NULL DEFAULT 0,
  due_date TEXT NOT NULL,                  -- 새 카드는 생성일 + 1
  consecutive_expired INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',   -- active|suspended
  created_at TEXT NOT NULL,
  UNIQUE(source_entry_id, concept_id, question)
);

CREATE TABLE quiz_attempt (
  id INTEGER PRIMARY KEY,                  -- 화면의 [Q<id>]
  card_id INTEGER NOT NULL REFERENCES quiz_card(id),
  goal_id INTEGER NOT NULL REFERENCES goal(id),
  issued_date TEXT NOT NULL, expire_date TEXT NOT NULL,
  file_id TEXT NOT NULL, node_id TEXT,     -- 쓰기가 끝나기 전에는 NULL
  status TEXT NOT NULL,                    -- pending_write|open|graded|expired|orphaned
  answer_text TEXT, answer_hash TEXT,
  score INTEGER, grade_comment TEXT, graded_at TEXT,
  run_id INTEGER REFERENCES run_log(id)
);
CREATE UNIQUE INDEX ux_attempt_card_day ON quiz_attempt(card_id, issued_date);

CREATE TABLE bot_node (
  file_id TEXT NOT NULL, node_id TEXT NOT NULL,
  kind TEXT NOT NULL,                      -- §3.4의 kind
  ref_id INTEGER,                          -- goal/concept/feedback/attempt id 등
  op_key TEXT,                             -- 이 노드를 만든 write_op
  created_at TEXT NOT NULL, deleted_at TEXT,
  PRIMARY KEY(file_id, node_id)
);
CREATE INDEX ix_bot_node_kind ON bot_node(kind, ref_id);

CREATE TABLE write_op (                    -- Outbox
  id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES run_log(id),
  op_key TEXT NOT NULL UNIQUE,             -- 결정적 키 (§8.2)
  file_id TEXT NOT NULL,
  action TEXT NOT NULL,                    -- insert|edit|delete
  parent_ref TEXT,                         -- insert: 'node:<id>' 또는 'op:<op_key>'
  target_ref TEXT,                         -- edit/delete: 'node:<id>' 또는 'op:<op_key>'
  idx INTEGER,                             -- insert index
  payload_json TEXT NOT NULL,              -- content/note/checked
  bot_kind TEXT, bot_ref_id INTEGER,       -- 성공하면 bot_node에 등록할 정보
  marker TEXT,                             -- 최상위 봇 노드의 note 마커 (tb:kind:id)
  seq INTEGER NOT NULL,                    -- 같은 run 안에서의 실행 순서
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|done|failed|skipped
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  result_node_id TEXT, created_at TEXT NOT NULL, done_at TEXT
);

CREATE TABLE weekly_report (
  id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES goal(id),
  iso_week TEXT NOT NULL, body_json TEXT NOT NULL, node_id TEXT,
  created_at TEXT NOT NULL, UNIQUE(goal_id, iso_week)
);

CREATE TABLE learner_profile (             -- 토큰 절약용 롤링 요약 (§9)
  goal_id INTEGER PRIMARY KEY REFERENCES goal(id),
  summary TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE run_log (
  id INTEGER PRIMARY KEY, run_date TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL,                    -- running|ok|partial|failed
  stats_json TEXT,                         -- 단계별 처리 건수
  error_summary TEXT,
  llm_calls INTEGER DEFAULT 0, llm_prompt_tokens INTEGER DEFAULT 0, llm_completion_tokens INTEGER DEFAULT 0,
  dynalist_reads INTEGER DEFAULT 0, dynalist_changes INTEGER DEFAULT 0
);

CREATE TABLE llm_call (
  id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES run_log(id),
  task TEXT NOT NULL, model TEXT NOT NULL, ref TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms INTEGER,
  ok INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL
);
```

**v0.1 대비 보완 사항**
- `quiz_attempt`에 `node_id`, `status`, `expire_date`, `answer_hash`를 추가함. `[Q123]` = attempt id로 확정함.
- `knowledge_dump` 테이블을 새로 만듦. 덤프의 해시가 저장되는 곳임.
- `concept.node_id`와 `rendered_text`를 추가함. 상태 아이콘을 바꿀 때 바뀐 노드만 `edit`하기 위함.
- 선수개념은 `concept_prereq` 테이블로 정규화함. 상태 변경 이력은 `concept_state_event`에 남김.
- `bot_node`, `write_op`, `llm_call`, `learner_profile`, `weekly_report`를 새로 만듦.

---

## 6. 일일 배치 흐름 (v0.2)

### 6.1 "변경 없으면 퀴즈 출제만" 모순의 해결
- 퀴즈를 출제하면 문서가 바뀌고, 어제 퀴즈의 답도 받아야 하므로 **문서 버전으로 단락(short-circuit)하지 않음**.
- 대신 **항상** `doc/read`를 1회 함(설정된 문서마다 1회, rate limit에 비해 미미함). 변경 판정은 단위별 해시로 함(§4).
- 봇이 쓴 노드는 해시에서 빠지므로 **봇 자신의 쓰기가 다음 배치의 "변경"으로 잡히지 않음**.

### 6.2 단계
```
0. 잠금      : 파일 잠금(`data/tutorbot.lock`, fcntl). 이미 실행 중이면 종료.
               run_log 행 생성(status=running). run_date = 현지 날짜(--date로 덮어쓸 수 있음).
1. Outbox 정리: 이전 실행에서 남은 write_op(pending/failed-retryable)부터 실행 (§8)
2. 읽기      : 문서마다 doc/read → NodeIndex(id→node, parent map) 구성
3. 등록부 조정: bot_node 중 문서에 없는 노드는 deleted_at 기록.
               태그나 마커가 있는데 등록되지 않은 봇 노드는 마커로 ref를 찾아 등록(복구)
4. 파싱      : 목표·덤프·공부·퀴즈 답 수집, 해시 계산, DB upsert.
               사라진 사용자 노드는 status=deleted(데이터는 보존)
5. 목표      : 새 목표 → 커리큘럼 LLM → concept 저장 → 커리큘럼 트리 write_op 생성
               (목표 원문만 바뀌면 title/level/deadline만 갱신하고 커리큘럼은 재생성하지 않음)
6. 덤프      : processed_hash와 다른 덤프 → 평가 LLM → 상태 이벤트 적용
7. 공부      : 처리 대상(§6.3) → 분석 LLM(매핑+피드백+카드) → DB + feedback write_op
8. 채점      : 답이 있는 open attempt → (빠른 0점 처리 | 채점 LLM 배치) → SRS 갱신,
               상태 하향 → grade write_op + quiz_item checked
9. 만료      : expire_date < run_date인 open attempt → expired, 노드에 ⌛ 표시 + checked
10. 출제     : (goal, run_date)로 이미 출제했으면 건너뜀. 아니면 카드를 선택(§7.3)하고
               attempt(pending_write) 생성 → quiz_day/quiz_item write_op
11. 진도 갱신: 각 concept의 렌더링 결과가 rendered_text와 다르면 edit op.
               curriculum_root 제목의 진도율과 progress_report 갱신(내용이 바뀐 경우만)
12. 주간     : 월요일이거나 보충이 필요하면 weekly_report 생성 → write_op
13. 쓰기     : write_op 실행 (§8.3)
14. 종료     : run_log 마무리(ok | partial(일부 단위 실패) | failed), 잠금 해제
```
- 단계 5~12는 **단위마다 독립적**임. 한 단위가 실패해도 다른 단위는 계속 처리함.
- LLM 호출은 DB 트랜잭션 **밖에서** 함. 결과를 받은 뒤 "DB 갱신 + write_op 생성"을 **하나의 트랜잭션으로 커밋**함.
- `--dry-run`: 2~12를 수행하되 LLM은 실제로 호출하고 DB는 임시 복사본을 쓰며, 13 대신 write_op 목록을 출력함. `--dry-run --fake-llm`은 비용 없이 실행함.

### 6.3 공부 노드 처리 대상 조건
`status`가 pending/error이고 `content_hash != processed_hash`이며, 다음을 모두 만족해야 함.
- 목표 연결 성공(D4). 실패하면 `skipped_untagged`와 경고 1회
- 태그를 뺀 본문 길이 ≥ `min_entry_chars`. 실패하면 `too_short`(경고 없음)
- `now - max_modified ≥ settle_minutes`. 실패하면 `deferred`
- 이전에 처리한 원문이 있고 유사도 ≥ 0.95이면: `processed_hash = content_hash`로 갱신하고 끝냄(D2)
- 하루 처리 상한 `max_entries_per_run`(기본 20). 넘치는 노드는 다음 배치로 미룸(비용 폭주 방지)

### 6.4 실패 · 재시도 정책
| 실패 | 처리 |
|---|---|
| LLM 호출 오류/타임아웃 | 같은 실행 안에서 최대 2회 재시도(백오프 2s, 8s) |
| LLM JSON 검증 실패 | 오류 메시지를 덧붙여 **수정 요청 1회** → 그래도 실패하면 단위 실패 |
| 단위 실패 | `status=error`, `retry_count+=1`, `processed_hash`는 그대로 두어 다음 배치에서 다시 시도. `retry_count ≥ 3`이면 `failed`로 두고 **원문이 바뀔 때까지** 건너뜀(해시가 바뀌면 retry_count=0) |
| Dynalist 읽기 실패 | 해당 문서는 이번 배치에서 건너뜀(쓰기도 하지 않음). 모든 문서가 실패하면 run=failed |
| `doc/edit` 실패 | §8.3 |
| 커리큘럼 생성 실패 | goal.status=error. 3회 실패하면 `failed`와 목표 아래 경고 노드 |
| 예산 초과 | `daily_token_budget`(기본 300k)을 넘으면 남은 LLM 단위는 다음 배치로 미룸. 채점과 출제를 공부 분석보다 먼저 보장하도록 **단계 8·10을 7보다 먼저** 실행하는 옵션(`priority_quiz=true`, 기본값)을 둠 |

> 기본 실행 순서는 `priority_quiz=true` 기준으로 **0→1→2→3→4→5→8→9→10→6→7→11→12→13→14**임. 새 공부에서 만든 카드는 due가 내일이라 오늘 출제와 관계가 없으므로 순서를 바꿔도 문제없음.

---

## 7. 도메인 규칙

### 7.1 진도율
- 대상: `is_leaf=1 AND user_deleted=0`인 개념
- 가중치: unknown 0, partial 0.5, understood 1, mastered 1
- 진도율 = Σ가중치 / 잎 개수 × 100 (반올림 정수). 중간 노드는 `(understood 이상인 잎 수)/(잎 수)`로 표시

### 7.2 SRS (SM-2 변형)
```
입력 q ∈ 0..5
if q < 3: repetitions = 0; interval = 1; lapses += 1
else:
    repetitions += 1
    interval = 1 if repetitions == 1 else 6 if repetitions == 2 else round(interval * ease)
ease = max(1.3, ease + (0.1 - (5-q)*(0.08 + (5-q)*0.02)))
due_date = run_date + interval
consecutive_expired = 0
```

### 7.3 카드 선택 (목표별)
1. `slots = quiz_per_day - (이 목표의 open attempt 수)`. 0 이하이면 출제하지 않음(quiz_day 노드도 만들지 않음).
2. 복습 후보: `status=active`이고 `due_date ≤ run_date`이고 open attempt가 없고 개념이 `user_deleted=0`인 카드. `due_date` 오름차순, 같으면 `lapses` 내림차순.
3. 새 카드(`repetitions=0 AND` 출제 이력 없음)는 최대 `max_new_cards_per_day`장. 복습 카드를 먼저 채우고 남은 칸을 새 카드로 채움.
4. 같은 개념은 하루에 1장만.

### 7.4 개념 상태 전이 규칙 (엔진이 결정)
| 근거 | 허용 전이 |
|---|---|
| dump | LLM 제안(`partial`/`understood`)으로 **올리기만** 가능. 단, 다른 근거(study/quiz) 이벤트가 **없는** 개념은 덤프 재평가로 내릴 수도 있음 |
| study | `coverage=partial` → 최대 partial, `coverage=full` + LLM 제안 understood → understood. **올리기만** 가능 |
| quiz (q ≤ 2) | understood/mastered → partial로 **내림** |
| srs | 카드 `interval_days ≥ 21` 그리고 마지막 q ≥ 4 → mastered (해당 개념의 카드 중 하나라도 만족하면) |
- 모든 전이는 `concept_state_event`에 기록하고, 개념 노드는 단계 11에서 갱신함.

---

## 8. 쓰기 신뢰성: Outbox · 멱등성

### 8.1 원칙
- DB가 진실의 원천임. Dynalist 쓰기는 **DB에 커밋된 write_op을 실행하는 것**뿐임.
- 모든 write_op은 결정적인 `op_key`를 가짐. 같은 op_key는 한 번만 생성됨(UNIQUE). 그래서 같은 날 배치를 다시 돌려도 중복 쓰기가 생기지 않음.

### 8.2 op_key 규칙
| 작업 | op_key |
|---|---|
| 커리큘럼 루트 | `curr_root:{goal_id}:{gen}`. gen은 루트를 다시 만든 횟수 |
| 개념 노드 삽입 | `concept:{concept_id}:{gen}` |
| 개념 표시 갱신 | `concept_edit:{concept_id}:{sha1(rendered)[:8]}` |
| 피드백 헤더 생성 | `fb:{entry_id}` (최초 1회) |
| 피드백 갱신 | `fb_edit:{entry_id}:{based_on_hash[:12]}` (헤더 edit + 이전 자식 delete + 새 자식 insert). 지울 자식 id는 이번 배치의 `NodeIndex.children_of(feedback.node_id)`에서 얻음 |
| 퀴즈 날짜 노드 | `quiz_day:{goal_id}:{run_date}` |
| 퀴즈 항목 | `quiz_item:{attempt_id}` |
| 채점 | `grade:{attempt_id}`, `grade_check:{attempt_id}` |
| 만료 | `expire:{attempt_id}` |
| 경고 | `warn:{kind}:{file_id}:{node_id}:{reason}` |
| 주간 | `weekly:{goal_id}:{iso_week}` |

### 8.3 실행기 (Writer)
1. 실행 대상: `status IN (pending, failed)`이고 `attempts < 5`인 op. `seq` 순서대로 처리.
2. **의존성 해석**: `parent_ref/target_ref = 'op:<k>'`이면 해당 op의 `result_node_id`로 바꿈. 아직 없으면 다음 웨이브로 미룸.
3. 웨이브마다 문서별로 change를 모아 ≤100개씩 `doc/edit`. 응답의 `new_node_ids`를 insert 순서대로 op에 매핑. 성공하면 같은 트랜잭션에서 `write_op.done` + `bot_node` 등록 + 도메인 행(`concept.node_id`, `quiz_attempt.node_id/status=open`, `feedback.node_id` 등) 갱신.
4. **실패 처리**:
   - 재시도 가능한 오류(rate limit, 5xx, 네트워크): 백오프 후 재시도
   - 부모 노드가 사라짐(`NodeNotFound` 등): 해당 op와 그에 의존하는 op를 `skipped`로 표시하고 원인을 기록(예: 사용자가 공부 노드를 막 지운 경우)
   - 요청 결과를 알 수 없음(타임아웃 등): **그 문서를 다시 읽어 조정(reconcile)한 뒤** 재시도
5. **조정(reconcile)**: insert를 다시 시도하기 전에 부모 아래에서 다음을 찾음.
   - 마커가 있는 op: note 첫 줄이 `marker`와 같은 자식
   - 마커가 없는 하위 op: content가 payload와 **정확히 같고** `bot_node`에 등록되지 않은 자식
   - 찾으면 **새로 넣지 않고 그 노드를 채택**(done 처리). 이 방식이면 `doc/edit`이 원자적인지와 관계없이 중복이 생기지 않음.
6. 한 번에 모든 쓰기가 성공하지 않아도 됨. 남은 op은 다음 배치의 단계 1에서 다시 실행됨.

### 8.4 동시 편집
- 사용자가 배치 중에 편집하는 경우는 04:00 실행이므로 드묾. 그래도 다음을 지킴.
  - 봇은 **사용자 노드를 수정하거나 삭제하지 않음**(insert만 함). edit/delete는 봇 노드에만 함.
  - 그래서 사용자 데이터를 잃을 위험은 없음. 최악의 경우는 op이 `skipped`되는 것뿐임.

---

## 9. LLM 작업 명세 · 토큰 최적화

### 9.1 공통
- 시스템 프롬프트(작업별 고정) → **고정 접두부**(목표 정보 + 개념 인덱스) → 가변부(입력) 순서로 구성함. 공급자의 프롬프트 캐싱을 활용하기 위함.
- 출력은 JSON만 허용. pydantic 모델로 검증. 출력에 나온 `concept_id`가 입력 후보에 없으면 그 항목만 버리고 경고 로그를 남김.
- 프롬프트 파일은 `src/tutorbot/llm/prompts/<task>.md`에 둠(코드와 분리). 버전 문자열을 `llm_call.task`에 함께 기록함.

### 9.2 작업별 입출력 스키마

**T1 커리큘럼 생성** (`model_smart`)
- 입력: 목표 하위 트리 텍스트, run_date
- 출력:
```json
{"goal":{"title":"str","level":"str|null","deadline":"YYYY-MM-DD|null"},
 "concepts":[{"key":"c1","parent_key":null,"name":"str","description":"≤80자",
              "prereq_keys":["c0"],"order":1}]}
```
- 제약: 깊이 ≤3, 잎 30~100개, 이름은 25자 이하. 같은 부모 아래에서 이름 중복 금지. `key` 참조 무결성은 검증 단계에서 확인함.

**T2 지식덤프 평가** (`model_smart`)
- 입력: 개념 인덱스(§9.3), 덤프 원문
- 출력(근거가 있는 개념만):
```json
{"assessments":[{"concept_id":12,"state":"partial|understood","evidence":"≤60자"}],
 "gap_summary":["≤5줄"],"next_concepts":[12,15,18]}
```

**T3 공부 분석 = 매핑 + 피드백 + 카드** (`model_smart`, 단위 1건당 1회)
- 입력: 개념 인덱스(또는 후보 상위 K), learner_profile 요약, 매핑된 개념의 현재 상태, 공부 원문(최대 `entry_max_chars`=6000자, 넘으면 앞뒤를 보존하고 가운데를 생략), 이 공부 노드의 이전 피드백(재분석일 때만)
- 출력:
```json
{"concepts":[{"concept_id":12,"coverage":"mention|partial|full","state_suggestion":"partial|understood|null"}],
 "feedback":{"corrections":["≤2개"],"missing":["≤2개"],"next":["≤1개"]},
 "cards":[{"concept_id":12,"question":"≤150자","answer_key":"≤300자"}],
 "off_curriculum":"str|null"}
```
- 피드백 줄 수(corrections + missing + next)는 **합계 최대 4줄**이고 여기에 매핑 줄 1줄을 더해 **5줄 이내**. 4줄을 넘으면 코드가 corrections → next → missing 순서로 우선 남기고 나머지를 잘라냄. 각 줄은 120자 이하. 교정할 것이 없으면 빈 배열.
- 카드: 최대 3장. `coverage`가 partial/full인 개념만 대상이고 이미 카드가 있는 `(entry, concept)`는 제외(입력으로 제외 목록을 줌).

**T4 채점** (`model_fast`, 목표별 배치 1회, 최대 20문항씩)
- 입력: `[{"attempt_id","question","answer_key","user_answer"}]`
- 출력: `[{"attempt_id":123,"score":0-5,"comment":"≤80자"}]`. 모든 attempt_id가 정확히 한 번씩 있어야 함. 빠진 문항은 그 문항만 다음 배치에서 다시 채점.
- 채점 기준표(0: 무응답/완전 오답 … 5: 완전·정확)를 프롬프트에 고정. temperature 0.

**T5 주간 리포트** (`model_fast`)
- 입력: **SQL로 집계한 통계만** 넣음(공부 건수, 상태 전이 수, 퀴즈 정답률, 약점 개념 상위 5개, 다음 추천 후보). 원문은 넣지 않음.
- 출력: `{"summary":"≤3줄","weak_points":["≤3"],"next_week":["≤3"]}`. 숫자(진도율 등)는 **코드가 렌더링**하고 LLM은 문장만 씀.

**T6 학습자 프로필 갱신** (`model_fast`, 주 1회 주간 리포트와 함께)
- 입력: 기존 summary + 지난주 피드백의 corrections/missing 모음
- 출력: `{"summary":"≤600자"}` → `learner_profile`

### 9.3 토큰 최적화 전략
1. **압축 개념 인덱스**: 한 줄에 `id|상위>개념|상태기호` 형식(예: `12|메모리>소유권|🟡`). 설명과 선수개념은 넣지 않음. 개념 100개 기준 약 1.5~2k 토큰.
2. **후보 축소(개념 > `index_full_threshold`=150개일 때)**:
   - 1차: 공부 원문과 개념 이름·설명의 **임베딩 코사인 유사도 상위 K=25** (임베딩은 `concept.embedding` BLOB 컬럼에 캐시하고 개념이 생성될 때 1회 계산. 이 컬럼은 M5 마이그레이션에서 추가하며 §5 DDL에는 없음). 임베딩을 쓸 수 없으면 BM25 키워드 매칭으로 대체.
   - 2차: 선택된 개념의 부모와 형제 개념을 일부 추가해 맥락을 보존.
   - MVP(개념 ≤100개)에서는 전체 인덱스를 쓰고, 이 경로는 M5에서 구현해도 됨. 인터페이스(`ConceptRetriever.candidates(text) -> list[Concept]`)는 처음부터 둠.
3. **고정 접두부 캐싱**: 같은 목표의 공부를 연속으로 처리할 때 시스템 프롬프트와 개념 인덱스가 동일한 접두부가 되도록 순서를 고정함.
4. **이력을 요약으로 대체**: 과거 공부 원문이나 피드백 전체를 넣지 않음. `learner_profile`(≤600자)과 매핑된 개념의 `state_reason`만 넣음.
5. **입력 상한**: 공부 원문은 6000자, 덤프는 12000자로 자름(넘치는 경우 덤프는 최상위 자식 단위로 나눠 여러 번 평가하고 결과를 max 병합).
6. **LLM이 필요 없는 일은 코드로**: 빠른 0점, 사소한 수정 감지, 진도율, SRS, 주간 숫자.
7. **계측**: 모든 호출의 토큰 수를 `llm_call`에 기록함. `tutorbot stats`로 일별·작업별 사용량을 확인함. `daily_token_budget`으로 상한을 둠.
- 예상치: 하루 공부 3건 기준 T3 약 3×5k + T4 약 2k + 기타 ≈ **2만~3만 토큰/일**.

---

## 10. 설정 (`config.toml`)
```toml
timezone = "Asia/Seoul"
db_path = "data/tutorbot.sqlite3"

[dynalist]
token_env = "DYNALIST_TOKEN"          # 토큰은 환경변수로만 받음. 파일에 쓰지 않음
file_ids = ["<doc id>"]               # MVP는 1개를 권장, 여러 개도 지원

[tags]
goal  = ["목표", "goal"]
dump  = ["지식덤프", "dump"]
study = ["공부", "study"]
bot   = "tutorbot"

[quiz]
per_day = 5
max_new_per_day = 2
expire_days = 3
skip_answers = ["?", "모름", "pass", "패스"]

[processing]
min_entry_chars = 20
settle_minutes = 60
similarity_skip = 0.95
max_entries_per_run = 20
entry_max_chars = 6000
priority_quiz = true

[llm]
provider = "gemini"                   # gemini | openai | anthropic
api_key_env = "LLM_API_KEY"
model_smart = "<설정>"
model_fast = "<설정>"
daily_token_budget = 300000
index_full_threshold = 150
```

---

## 11. 코드 구조 (worker용)
```
tutor_ws/
  pyproject.toml                # requires-python ">=3.10" (현재 머신은 3.10.12), deps: httpx, pydantic>=2, tomli; python_version<'3.11', 공급자 SDK(선택)
  config.example.toml
  src/tutorbot/
    cli.py                      # run | init-db | doctor | stats   (argparse 또는 typer)
    config.py                   # pydantic Settings
    clock.py                    # now()/run_date 주입 (테스트용)
    db/ schema.sql, migrations/, repo.py
    dynalist/ client.py         # HTTP + 토큰 버킷 + 재시도 + 오류 분류
              tree.py           # NodeIndex: by_id, parent_of, iter_subtree, text_of
              writer.py         # Outbox 실행기 + reconcile
    parse/ tags.py, dates.py, hashing.py, classify.py
    llm/ client.py (공급자 추상화), schemas.py (pydantic), prompts/*.md, retriever.py
    engine/ goals.py, dumps.py, study.py, quiz.py (선택·채점·SRS), state.py (§7.4), progress.py, weekly.py
    render.py                   # 봇 노드 텍스트 렌더링 (§3.4 형식은 여기 한 곳에만)
    pipeline.py                 # §6.2 오케스트레이션
  tests/
    fixtures/doc_read_*.json
    fakes.py                    # FakeDynalist(read/edit 의미 재현, 장애 주입), FakeLLM(작업별 고정 응답)
  deploy/ tutorbot.service, tutorbot.timer
```

---

## 12. 테스트 · 수용 기준

| 영역 | 필수 테스트 |
|---|---|
| hashing | 같은 트리는 같은 해시. 봇 자식을 추가해도 해시 불변. checked/collapsed를 바꿔도 불변. 자식 순서를 바꾸면 변경. note를 고치면 변경. NFC/공백 정규화 |
| dates | D3의 우선순위 3가지 + 시간대 경계(UTC 15:30 = KST 다음 날 00:30) |
| classify | 중첩 `#공부`, 봇 하위 트리 제외, 퀴즈 답 영역, 한글/ASCII 태그 별칭 |
| SRS | §7.2 공식의 표 기반 테스트, ease 하한 1.3 |
| 선택 | 미응답이 한도를 차지함, 새 카드 상한, 같은 개념은 하루 1장 |
| **멱등성** | FakeDynalist로 파이프라인을 2회 연속 실행 → **두 번째 실행의 Dynalist change는 0개**(또는 진도 표시 변경만) |
| **장애 주입** | (a) insert 성공 직후 프로세스가 죽음 → 재실행해도 중복 노드 0개 (b) TooManyRequests 2회 후 성공 (c) 쓰기 전에 부모 삭제 → op skipped, 배치는 partial |
| LLM | 잘못된 JSON → 수정 요청 1회 → 실패하면 단위 error. 다른 단위는 처리됨 |
| E2E | 픽스처 문서: 목표 1, 덤프 1, 공부 2, 퀴즈 답 → 1일차 실행 → 2일차(답 작성 시뮬레이션) 실행. 예상 트리 스냅샷과 비교 |

---

## 13. 리스크 · 대응 (보완)
| 리스크 | 대응 |
|---|---|
| Dynalist API 필드명이나 제약이 문서와 다름 | M0에서 실제 토큰으로 스모크 테스트. 필드명은 client.py 상수로 격리 |
| 한글 태그를 인식하지 못함 | 파서는 정규식으로 동작함. ASCII 별칭 지원(§3.3) |
| LLM이 개념 id를 잘못 만들어냄(환각) | 후보 집합에 없는 id는 버림. 카드와 상태 이벤트도 같은 검증을 거침 |
| 피드백 품질이 낮음 | 프롬프트를 파일로 분리하고 버전을 기록. dry-run으로 품질을 미리 확인 |
| 비용 폭주 | 단위 상한, 유사도 스킵, 일일 예산, 계측 |
| DB 손실 | 매일 실행 전 `data/backup/tutorbot-YYYYMMDD.sqlite3`로 SQLite 백업(7일 보관). note 마커와 `[Q id]`로 일부 복구 가능 |
| 사용자가 봇 노드를 고침 | 봇 노드는 덮어씀(문서 규약에 명시). 사용자 노드는 봇이 절대 수정하지 않음 |

---

## 14. 구현 마일스톤 (수용 기준 포함)

| M | 범위 | 완료 기준 |
|---|---|---|
| **M0** | 프로젝트 골격, config, Dynalist client(read/edit, 토큰 버킷, 재시도), `tutorbot doctor` | 실제 문서를 읽어 노드 수를 출력. 테스트용 노드 1개 insert→edit→delete 성공. **§3.1 필드명, insert `index:-1`이 맨 뒤 삽입으로 동작하는지(안 되면 `len(children)` 사용), 한글 태그 검증 결과를 보드에 보고** |
| **M1** | DB 스키마/마이그레이션, NodeIndex, 태그/날짜/분류/해시, 파싱 단계(4) | 픽스처 기반 단위 테스트 통과. `tutorbot run --dry-run --fake-llm`이 수집 결과를 출력 |
| **M2** | Outbox writer + reconcile, render.py, 목표→커리큘럼(T1), 진도 갱신(단계 11) | 멱등성 테스트와 장애 주입 (a)(b)(c) 통과. 실제 문서에 커리큘럼 생성 |
| **M3** | 덤프 평가(T2), 공부 분석(T3), 피드백 쓰기/갱신, 상태 규칙 §7.4 | 수정된 공부 노드의 피드백이 같은 노드에서 갱신됨. 사소한 수정은 스킵 |
| **M4** | 카드·선택·출제·채점(T4)·만료·SRS | 2일 E2E 시나리오 통과. 같은 날 재실행해도 퀴즈가 중복되지 않음 |
| **M5** | 주간 리포트(T5/T6), 예산/stats, systemd 배포, 백업, (선택) 검색 기반 후보 축소 | timer로 하루 실행 성공, run_log/llm_call 집계 확인 |

---

## 15. 사용자 확인이 필요한 항목 (기본값으로 진행 가능)
1. Dynalist 구성: **전용 문서 1개**에 목표·덤프·공부를 모두 두는 것을 권장. 기존 일기 문서 여러 개를 써야 하는지?
2. LLM 공급자와 API 키: 어떤 공급자의 키를 가지고 있는지?(기본 설계는 공급자 중립)
3. 피드백 위치: 공부 노드의 **마지막 자식**(기본) vs 첫 자식
4. 커리큘럼 규모: 잎 30~100개가 적당한지(분야가 넓으면 상향)
