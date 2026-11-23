CREATE TABLE domain_state (
  domain_id       TEXT NOT NULL,
  file_id         TEXT NOT NULL,
  source_hash     TEXT NOT NULL,
  feedback_json   TEXT,
  status          TEXT NOT NULL,
  last_error      TEXT,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY(domain_id, file_id)
);

CREATE TABLE domain_run (
  id              INTEGER PRIMARY KEY,
  run_id          INTEGER REFERENCES run_log(id),
  domain_id       TEXT NOT NULL,
  file_id         TEXT NOT NULL,
  status          TEXT NOT NULL,
  source_hash     TEXT,
  error           TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX ix_domain_run_domain ON domain_run(domain_id, file_id, created_at);

CREATE TABLE personal_task (
  file_id         TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  status          TEXT,
  status_transition INTEGER NOT NULL DEFAULT 0,
  content_hash    TEXT NOT NULL,
  analyzed_hash   TEXT,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY(file_id, node_id)
);

CREATE TABLE personal_task_progress (
  file_id         TEXT NOT NULL,
  task_node_id    TEXT NOT NULL,
  record_node_id  TEXT NOT NULL,
  date_text       TEXT,
  entry_date      TEXT,
  subtree_hash    TEXT NOT NULL,
  detail_paths_json TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY(file_id, task_node_id, record_node_id)
);

CREATE TABLE completion_event (
  id              INTEGER PRIMARY KEY,
  file_id         TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  transition_id   INTEGER NOT NULL,
  observed_at     TEXT NOT NULL,
  completed_at    TEXT,
  initial_discovery INTEGER NOT NULL DEFAULT 0,
  category_path_json TEXT NOT NULL,
  source_hash     TEXT NOT NULL,
  UNIQUE(file_id, node_id, transition_id)
);
