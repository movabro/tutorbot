import logging
import os
from datetime import timedelta

import pytest

from tests.test_feedback_foundations import task
from tests.test_feedback_reviewer_round6 import NOW, _environment, _run, _success_task
from tests.test_m5 import setup_env
from tutorbot.feedback import service, store
from tutorbot.feedback.models import FeedbackRulesCfg, LockResult
from tutorbot.pipeline import run_pipeline

SECRET = "PRIVATE_PAYLOAD_DO_NOT_LOG"


@pytest.mark.parametrize("reason,code,level", [
    ("missing", "FR-R022", logging.WARNING),
    ("permanent", "FR-R025", logging.ERROR),
])
def test_output_failure_reason_logged_once_on_transition(caplog, reason, code, level):
    conn, _, _, client, _, llm, settings = _environment(task())
    try:
        run_id, _ = _run(conn, client, settings, llm, NOW)
        key = conn.execute("SELECT root_op_key FROM feedback_output").fetchone()[0]
        if reason == "missing":
            conn.execute("UPDATE write_op SET status='skipped',last_error=? WHERE op_key=?",
                         (f"parent {SECRET} not in document", key))
        else:
            conn.execute("UPDATE write_op SET status='failed',attempts=5,last_error=? WHERE op_key=?",
                         (SECRET, key))
        with caplog.at_level(logging.INFO):
            assert store.sync_output_status(conn, now_iso=NOW.isoformat()).failed == 1
            assert store.sync_output_status(conn, now_iso=NOW.isoformat()).failed == 0
        matching = [r for r in caplog.records if code in r.message]
        assert len(matching) == 1 and matching[0].levelno == level
        assert "count=1" in matching[0].message and SECRET not in caplog.text
        assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == "failed"
        assert store.finalize_runs(conn, run_id, now_iso=NOW.isoformat())["failed"] == 1
    finally:
        conn.close()


def test_unconfirmed_is_not_permanent_failure(caplog):
    conn, _, _, client, _, llm, settings = _environment(task())
    try:
        run_id, _ = _run(conn, client, settings, llm, NOW)
        conn.execute("UPDATE write_op SET status='failed',attempts=5,last_error='unknown_outcome_reconcile' "
                     "WHERE op_key=(SELECT root_op_key FROM feedback_output)")
        with caplog.at_level(logging.INFO):
            assert store.sync_output_status(conn, now_iso=NOW.isoformat()).pending == 1
            assert store.finalize_runs(conn, run_id, now_iso=NOW.isoformat())["succeeded"] == 1
        assert "feedback_output_unconfirmed count=1" in caplog.text
        assert "FR-R022" not in caplog.text and "FR-R025" not in caplog.text
        assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == "pending_write"
    finally:
        conn.close()


def test_interrupted_sweep_and_stale_lock_log_only_transitions(caplog):
    conn, _, _, client, _, llm, settings = _environment(task())
    try:
        run_id, _ = _run(conn, client, settings, llm, NOW)
        with caplog.at_level(logging.INFO):
            assert store.sweep_interrupted(conn, current_run_id=run_id + 1, now_iso=NOW.isoformat()) == 1
            assert store.sweep_interrupted(conn, current_run_id=run_id + 1, now_iso=NOW.isoformat()) == 0
        assert caplog.text.count("FR-R026") == 1
        later = NOW + timedelta(days=1)
        run_id, _ = _run(conn, client, settings, llm, later)
        row = conn.execute("SELECT id FROM feedback_task_run WHERE run_id=?", (run_id,)).fetchone()[0]
        lock = store.try_lock(conn, "test", now_iso=later.isoformat(), stale_minutes=60, host="host", pid=os.getpid())
        store.heartbeat(conn, "test", lock.token, later.isoformat(), task_run_id=row)
        with caplog.at_level(logging.INFO):
            store.try_lock(conn, "test", now_iso=(later + timedelta(hours=2)).isoformat(),
                           stale_minutes=60, host="host", pid=os.getpid())
        assert caplog.text.count("FR-R026") == 2
        assert conn.execute("SELECT error FROM feedback_task_run WHERE id=?", (row,)).fetchone()[0] == "interrupted"
    finally:
        conn.close()


def test_busy_lock_info_log(caplog, monkeypatch):
    conn, _, _, client, _, llm, settings = _environment(task())
    monkeypatch.setattr(store, "try_lock", lambda *args, **kwargs: LockResult(False))
    try:
        with caplog.at_level(logging.INFO):
            _, report = _run(conn, client, settings, llm, NOW)
        assert report.already_running == 1
        assert any(r.levelno == logging.INFO and "FR-R005" in r.message for r in caplog.records)
    finally:
        conn.close()


@pytest.mark.parametrize("success", [False, True])
def test_cap_counts_skips_or_defers_without_leaking(caplog, success):
    conn, fake, _, client, backend, llm, settings = _environment(_success_task() if success else task())
    try:
        fake.add("doc", "root", SECRET, created=int((NOW - timedelta(hours=1)).timestamp() * 1000))
        settings.feedback_rules.max_units_per_task = 1
        with caplog.at_level(logging.INFO):
            _, report = _run(conn, client, settings, llm, NOW)
        if success:
            assert report.deferred_budget == 1 and not backend.calls
            assert "reason=success_window_cap count=1" in caplog.text
            assert "FR-R024" not in caplog.text
            assert conn.execute("SELECT COUNT(*) FROM feedback_task_run").fetchone()[0] == 0
        else:
            assert report.ran == 1 and len(backend.calls) == 1
            assert "code=FR-R024 count=1" in caplog.text
            assert conn.execute("SELECT units_skipped_cap FROM feedback_task_run").fetchone()[0] == 1
        assert SECRET not in caplog.text
    finally:
        conn.close()


def test_pipeline_stage_exception_is_partial_and_safe(tmp_path, monkeypatch, caplog):
    _, conn, _, client, llm, settings = setup_env(tmp_path)
    settings.feedback_rules = FeedbackRulesCfg(tasks=[task()])

    def fail(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(service, "run_feedback_stage", fail)
    try:
        with caplog.at_level(logging.INFO):
            result = run_pipeline(conn, settings, client, llm, run_date="2026-11-12",
                                  dry_run=True, lock_path=tmp_path / "lock")
        assert result.status == "partial"
        assert any(r.levelno == logging.ERROR and r.message == "feedback_stage_failed"
                   for r in caplog.records)
        assert SECRET not in caplog.text
    finally:
        conn.close()
