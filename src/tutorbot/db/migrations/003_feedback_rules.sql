CREATE TABLE IF NOT EXISTS feedback_task_run (
  id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL,
  cycle_key TEXT NOT NULL,
  trigger_kind TEXT NOT NULL CHECK (trigger_kind IN ('scheduled','each_run','manual')),
  schedule TEXT NOT NULL,
  scheduled_at TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('started','skipped','failed','succeeded')),
  skip_reason TEXT,
  error TEXT,
  prev_started_at TEXT,
  window_lower TEXT,
  run_id INTEGER REFERENCES run_log(id),
  config_hash TEXT NOT NULL,
  persona_hash TEXT,
  selected_count INTEGER NOT NULL DEFAULT 0,
  units_total INTEGER NOT NULL DEFAULT 0,
  units_ok INTEGER NOT NULL DEFAULT 0,
  units_failed INTEGER NOT NULL DEFAULT 0,
  units_skipped_cap INTEGER NOT NULL DEFAULT 0,
  input_truncated_units INTEGER NOT NULL DEFAULT 0,
  input_over_target_units INTEGER NOT NULL DEFAULT 0,
  llm_calls INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  outputs_enqueued INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_feedback_task_run_cycle ON feedback_task_run(task_id, cycle_key);
CREATE INDEX IF NOT EXISTS ix_feedback_task_run_task ON feedback_task_run(task_id, started_at);
CREATE INDEX IF NOT EXISTS ix_feedback_task_run_run ON feedback_task_run(run_id);

CREATE TABLE IF NOT EXISTS feedback_task_lock (
  task_id TEXT PRIMARY KEY,
  owner_pid INTEGER NOT NULL,
  owner_host TEXT NOT NULL,
  owner_token TEXT NOT NULL,
  task_run_id INTEGER,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_output (
  id INTEGER PRIMARY KEY,
  task_run_id INTEGER NOT NULL REFERENCES feedback_task_run(id),
  task_id TEXT NOT NULL,
  unit_ref TEXT NOT NULL,
  target_file_id TEXT NOT NULL,
  target_node_id TEXT NOT NULL,
  root_op_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('pending_write','written','failed')),
  input_hash TEXT,
  truncated INTEGER NOT NULL DEFAULT 0,
  over_target INTEGER NOT NULL DEFAULT 0,
  body_json TEXT NOT NULL,
  target_own_hash TEXT,
  target_effective_modified_ms INTEGER,
  result_node_id TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  written_at TEXT,
  UNIQUE(task_run_id, unit_ref, target_file_id, target_node_id)
);
CREATE INDEX IF NOT EXISTS ix_feedback_output_status ON feedback_output(status);
CREATE INDEX IF NOT EXISTS ix_feedback_output_target ON feedback_output(target_file_id, target_node_id, id);

CREATE TABLE IF NOT EXISTS feedback_persona_approval (
  id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL,
  persona_path TEXT NOT NULL,
  persona_hash TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('standing','once')),
  granted_at TEXT NOT NULL,
  consumed_at TEXT,
  consumed_by_run INTEGER REFERENCES feedback_task_run(id),
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_feedback_persona_approval_task ON feedback_persona_approval(task_id, persona_hash);
