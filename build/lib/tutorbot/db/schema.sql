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
