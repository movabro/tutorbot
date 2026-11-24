CREATE TABLE feedback_success_anchor (
  task_id TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  initial_lower TEXT NOT NULL,
  PRIMARY KEY (task_id, config_hash)
);
