import builtins
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.fakes import FakeDynalist, FakeLLM
from tests.test_feedback_foundations import task
from tests.test_m5 import setup_env
from tutorbot import db
from tutorbot.config import DomainProfileCfg, load_settings
from tutorbot.dynalist.client import DynalistClient
from tutorbot.dynalist.tree import NodeIndex
from tutorbot.feedback import store
from tutorbot.feedback.flush_adapter import flush_feedback_safe
from tutorbot.feedback.models import FeedbackRulesCfg
from tutorbot.feedback.service import finalize_after_flush, run_feedback_stage
from tutorbot.llm.client import LLMClient, LLMError
from tutorbot.pipeline import run_pipeline

NOW = datetime(2026, 11, 24, 1, tzinfo=timezone.utc)


def _environment(spec):
    conn = db.connect(":memory:")
    db.migrate(conn)
    fake = FakeDynalist()
    fake.add_doc("doc")
    node = fake.add("doc", "root", "example", created=int((NOW - timedelta(hours=1)).timestamp() * 1000))
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    backend = FakeLLM()
    backend.set("feedback_rule", {"summary": "example", "feedback": ["advice"], "evidence": []})
    llm = LLMClient(backend=backend, sleep=lambda _: None)
    settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[spec]))
    return conn, fake, node, client, backend, llm, settings


def _run(conn, client, settings, llm, now):
    run_id = conn.execute("INSERT INTO run_log(run_date,started_at,status) VALUES(?,?,'running')",
                          (now.date().isoformat(), now.isoformat())).lastrowid
    index = NodeIndex.from_doc_read("doc", client.doc_read("doc"))
    report = run_feedback_stage(conn, settings, llm, {"doc": index}, T=now,
                                run_id=run_id, dry_run=False)
    return run_id, report


def _success_task():
    return task(selection=[{"file_id": "doc", "parent_node_id": "root",
                            "scope": "descendants", "time": "created_since_last_success"}])


@pytest.mark.parametrize("legacy_failure", [False, True])
def test_response_lost_recovery_advances_success_bound_without_reanalysis(legacy_failure):
    spec = _success_task()
    conn, fake, node, client, backend, llm, settings = _environment(spec)
    try:
        run_id, report = _run(conn, client, settings, llm, NOW)
        assert report.ran == 1
        fake.crash_after_apply()
        flush_feedback_safe(conn, client)
        finalize_after_flush(conn, run_id, now_iso=NOW.isoformat())
        assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == "pending_write"
        assert conn.execute("SELECT status FROM feedback_task_run").fetchone()[0] == "succeeded"
        anchor = store.success_window_lower(conn, spec.id, spec.config_hash(), NOW.isoformat())
        assert datetime.fromisoformat(anchor) == NOW - timedelta(days=1)
        if legacy_failure:
            conn.execute("UPDATE feedback_task_run SET status='failed',error='write_unconfirmed'")
        flush_feedback_safe(conn, client)
        store.sync_output_status(conn, now_iso=(NOW + timedelta(minutes=1)).isoformat())
        assert tuple(conn.execute("SELECT status,error FROM feedback_task_run").fetchone()) == ("succeeded", None)
        assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == "written"
        assert store.success_window_lower(conn, spec.id, spec.config_hash(), anchor) == NOW.isoformat()
        assert len(fake.children("doc", node)) == 1
        _, next_report = _run(conn, client, settings, llm, NOW + timedelta(days=1))
        assert next_report.skipped == 1
        assert len(backend.calls) == 1
        assert len(fake.children("doc", node)) == 1
    finally:
        conn.close()


def test_confirmed_write_failure_remains_failed_and_does_not_advance():
    spec = _success_task()
    conn, fake, node, client, _backend, llm, settings = _environment(spec)
    try:
        run_id, _ = _run(conn, client, settings, llm, NOW)
        fake.docs["doc"]["root"]["children"].remove(node)
        del fake.docs["doc"][node]
        flush_feedback_safe(conn, client)
        assert finalize_after_flush(conn, run_id, now_iso=NOW.isoformat())["failed"] == 1
        store.sync_output_status(conn, now_iso=NOW.isoformat())
        assert tuple(conn.execute("SELECT status,error FROM feedback_task_run").fetchone()) == (
            "failed", "write_failed")
        assert datetime.fromisoformat(store.success_window_lower(
            conn, spec.id, spec.config_hash(), NOW.isoformat())) == NOW - timedelta(days=1)
    finally:
        conn.close()


@pytest.mark.parametrize("legacy_failure", [False, True])
def test_unconfirmed_write_that_later_fails_permanently(legacy_failure):
    spec = _success_task()
    conn, fake, node, client, _backend, llm, settings = _environment(spec)
    try:
        run_id, _ = _run(conn, client, settings, llm, NOW)
        fake.fail("doc/read", "NotFound")
        flush_feedback_safe(conn, client)
        finalize_after_flush(conn, run_id, now_iso=NOW.isoformat())
        assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == "pending_write"
        if legacy_failure:
            conn.execute("UPDATE feedback_task_run SET status='failed',error='write_unconfirmed'")
        fake.docs["doc"]["root"]["children"].remove(node)
        del fake.docs["doc"][node]
        flush_feedback_safe(conn, client)
        store.sync_output_status(conn, now_iso=NOW.isoformat())
        status, error = conn.execute("SELECT status,error FROM feedback_task_run").fetchone()
        assert status == "failed"
        assert error == ("write_failed" if legacy_failure else "write_failed_later")
        assert datetime.fromisoformat(store.success_window_lower(
            conn, spec.id, spec.config_hash(), NOW.isoformat())) == NOW - timedelta(days=1)
    finally:
        conn.close()


@pytest.mark.parametrize("scenario,code,reason", [
    ("approval", "FR-R004", "approval_required"),
    ("token", "FR-R006", "daily_token_budget"),
    ("calls", "FR-R006", "call_limit"),
    ("llm", "FR-R021", "llm_error"),
])
def test_safe_gate_budget_and_llm_reason_logs(tmp_path, caplog, scenario, code, reason):
    secret = "PRIVATE_EXAMPLE_DO_NOT_LOG"
    spec = task()
    if scenario == "approval":
        (tmp_path / "persona.md").write_text(secret)
        spec = replace(spec, base_dir=tmp_path, persona_path="persona.md",
                       persona_approval="user_approved_before_use")
    conn, fake, _node, client, backend, llm, settings = _environment(spec)
    try:
        if scenario == "calls":
            fake.add("doc", "root", secret)
            settings.feedback_rules.max_llm_calls_per_run = 1
        if scenario == "llm":
            backend.set("feedback_rule", LLMError(secret))
        run_id = conn.execute("INSERT INTO run_log(run_date,started_at,status) VALUES(?,?,'running')",
                              (NOW.date().isoformat(), NOW.isoformat())).lastrowid
        index = NodeIndex.from_doc_read("doc", client.doc_read("doc"))
        if scenario == "token":
            llm.stats["prompt_tokens"] = 10
        report = run_feedback_stage(conn, settings, llm, {"doc": index}, T=NOW, run_id=run_id,
                                    dry_run=False, token_budget=10 if scenario == "token" else None)
        assert code in caplog.text and reason in caplog.text
        assert secret not in caplog.text
        assert (report.failed if scenario == "llm" else
                report.awaiting_approval if scenario == "approval" else report.deferred_budget) == 1
    finally:
        conn.close()


def test_fallback_error_keeps_legacy_pipeline_running(tmp_path, monkeypatch, caplog):
    config = tmp_path / "config.toml"
    config.write_text('[feedback_rules]\ntasks = [{id = "invalid"}]\n')
    original_import = builtins.__import__

    def fallback(name, *args, **kwargs):
        if name in ("tomllib", "tomli"):
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fallback)
    parsed = load_settings(config, dotenv_path=tmp_path / "none")
    assert [issue.code for issue in parsed.feedback_rules.issues] == ["FR-G009"]
    fake, conn, backend, client, llm, settings = setup_env(tmp_path)
    try:
        settings.feedback_rules = parsed.feedback_rules
        fake.add_doc("work")
        fake.add("work", "root", "#project example")
        settings.domains["work"] = DomainProfileCfg(file_ids=["work"], tags=["project"])
        backend.set("domain_feedback", {"summary": "example", "feedback": ["advice"], "evidence": []})
        result = run_pipeline(conn, settings, client, llm, run_date="2026-11-12",
                              dry_run=True, lock_path=tmp_path / "lock")
        assert result.status == "partial"
        assert "feedback" not in result.stats
        assert result.stats["domains"]["processed"] == 1
        assert all(name in result.stats for name in ("goals", "dumps", "study", "weekly", "domains"))
        assert caplog.text.count("FR-G009") == 1
        assert not any(name == "feedback_rule" for name, _ in backend.calls)
    finally:
        conn.close()
