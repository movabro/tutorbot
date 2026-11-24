from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.fakes import FakeDynalist, FakeLLM
from tests.test_feedback_foundations import task
from tests.test_m5 import setup_env
from tutorbot import cli, db
from tutorbot.config import DomainProfileCfg
from tutorbot.dynalist.client import DynalistClient
from tutorbot.feedback import commands
from tutorbot.feedback.models import FeedbackRulesCfg
from tutorbot.feedback.service import FeedbackCommandResult, FeedbackStageReport, run_feedback_command
from tutorbot.llm.client import LLMClient
from tutorbot.pipeline import RunResult, _DomainErrorConnection, run_pipeline


@pytest.mark.parametrize("failure", ["model", "source"])
def test_domain_errors_never_reach_sqlite(tmp_path, monkeypatch, failure):
    fake, conn, backend, client, llm, settings = setup_env(tmp_path)
    secret = "SYNTHETIC_PRIVATE_ERROR_74629"
    statements = []
    conn.set_trace_callback(statements.append)
    fake.add_doc("work")
    fake.add("work", "root", "#project example")
    settings.domains["work"] = DomainProfileCfg(file_ids=["work"], tags=["project"])
    settings.feedback_rules = FeedbackRulesCfg(tasks=[task()])
    if failure == "model":
        original = llm.complete_json

        def complete(name, *args, **kwargs):
            if name == "domain_feedback":
                raise RuntimeError(secret)
            return original(name, *args, **kwargs)

        monkeypatch.setattr(llm, "complete_json", complete)
    else:
        def source(*args, **kwargs):
            raise RuntimeError(secret)

        monkeypatch.setattr("tutorbot.engine.domains._source_content", source)
    result = run_pipeline(conn, settings, client, llm, run_date="2026-11-12",
                          lock_path=tmp_path / "lock")
    assert result.status == "partial"
    run = conn.execute("SELECT status,error FROM domain_run WHERE domain_id='work'").fetchone()
    assert tuple(run) == ("error", "domain_processing_failed")
    state = conn.execute("SELECT status,last_error FROM domain_state WHERE domain_id='work'").fetchone()
    if failure == "model":
        assert tuple(state) == ("error", "domain_processing_failed")
    else:
        assert state is None
    assert secret not in str(result.errors)
    assert all(secret not in statement for statement in statements)
    conn.close()
    for path in tmp_path.glob("tb.sqlite3*"):
        assert secret.encode() not in path.read_bytes()


def test_devlog_domain_failure_is_redacted_before_write(tmp_path, monkeypatch):
    fake, conn, backend, client, llm, settings = setup_env(tmp_path)
    secret = "SYNTHETIC_DEVLOG_PRIVATE_ERROR"
    statements = []
    conn.set_trace_callback(statements.append)
    fake.add_doc("devlog")
    now_ms = int(datetime(2026, 11, 12, 4, tzinfo=timezone.utc).timestamp() * 1000)
    fake.add("devlog", "root", "example", created=now_ms, modified=now_ms)
    settings.domains["devlog"] = DomainProfileCfg(file_ids=["devlog"])
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(secret)))
    result = run_pipeline(conn, settings, client, llm, run_date="2026-11-12",
                          lock_path=tmp_path / "lock")
    assert result.status == "partial"
    assert tuple(conn.execute("SELECT status,error FROM domain_run WHERE domain_id='devlog'").fetchone()) == (
        "error", "domain_processing_failed")
    assert tuple(conn.execute("SELECT status,last_error FROM domain_state WHERE domain_id LIKE 'devlog:%'").fetchone()) == (
        "error", "domain_processing_failed")
    assert all(secret not in statement for statement in statements)
    conn.close()
    for path in tmp_path.glob("tb.sqlite3*"):
        assert secret.encode() not in path.read_bytes()


def test_domain_success_and_redaction_contract(tmp_path):
    fake, conn, backend, client, llm, settings = setup_env(tmp_path)
    fake.add_doc("work")
    fake.add("work", "root", "#project example")
    settings.domains["work"] = DomainProfileCfg(file_ids=["work"], tags=["project"])
    backend.set("domain_feedback", {"summary": "example", "feedback": ["advice"], "evidence": []})
    result = run_pipeline(conn, settings, client, llm, run_date="2026-11-12",
                          lock_path=tmp_path / "lock")
    assert result.stats["domains"]["processed"] == 1
    assert conn.execute("SELECT status FROM domain_state WHERE domain_id='work'").fetchone()[0] == "processed"
    with pytest.raises(RuntimeError, match="unsupported domain error persistence"):
        _DomainErrorConnection(conn).execute(
            "INSERT INTO domain_run(run_id,domain_id,file_id,status,error,created_at)"
            " VALUES(?,?,?,'error',upper(?),?)", (1, "work", "work", "private", "now"))
    conn.close()


def test_domain_success_with_warning_is_preserved(tmp_path, monkeypatch):
    fake, conn, backend, client, llm, settings = setup_env(tmp_path)
    fake.add_doc("work")
    fake.add("work", "root", "#project example")
    settings.domains["work"] = DomainProfileCfg(file_ids=["work"], tags=["project"])
    backend.set("domain_feedback", {"summary": "example", "feedback": ["advice"], "evidence": []})
    monkeypatch.setattr("tutorbot.engine.domains._source_content",
                        lambda *a, **k: ("example", ["synthetic warning"], []))
    result = run_pipeline(conn, settings, client, llm, run_date="2026-11-12",
                          lock_path=tmp_path / "lock")
    assert result.stats["domains"]["processed"] == 1
    assert tuple(conn.execute("SELECT status,error FROM domain_run WHERE domain_id='work'").fetchone()) == (
        "warning", "synthetic warning")
    conn.close()


def test_unapproved_preview_warns_and_does_not_consume(tmp_path, capsys, caplog):
    conn = db.connect(":memory:")
    db.migrate(conn)
    fake = FakeDynalist()
    fake.add_doc("doc")
    fake.add("doc", "root", "example")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    backend = FakeLLM()
    backend.set("feedback_rule", {"summary": "example", "feedback": ["advice"], "evidence": []})
    (tmp_path / "persona.md").write_text("PRIVATE_PERSONA_EXAMPLE", encoding="utf-8")
    spec = replace(task(), base_dir=tmp_path, persona_path="persona.md",
                   persona_approval="user_approved_before_use")
    settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[spec]), db_path=":memory:",
                               timezone="UTC", llm=SimpleNamespace(daily_token_budget=10000))
    result = run_feedback_command(settings, dry_run=True, dynalist=client, llm=LLMClient(backend=backend),
                                  conn=conn, at=datetime(2026, 11, 24, tzinfo=timezone.utc),
                                  lock_path=tmp_path / "lock")
    assert result.exit_code == 0 and result.report.preview_unapproved == 1
    assert result.report.awaiting_approval == 0 and backend.calls
    assert "approval required (preview only)" in capsys.readouterr().out
    assert "FR-R004 preview=1" in caplog.text and "PRIVATE_PERSONA_EXAMPLE" not in caplog.text
    assert conn.execute("SELECT COUNT(*) FROM feedback_persona_approval").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM feedback_task_run").fetchone()[0] == 0
    assert not any(path == "doc/edit" for path, _ in fake.calls)
    conn.close()


@pytest.mark.parametrize("code,counts,expected", [
    (0, {"deferred_budget": 1}, "deferred_budget:1"),
    (3, {"not_started_docs": 1, "not_started_persona": 2, "awaiting_approval": 1},
     "not_started_docs:1,not_started_persona:2,awaiting_approval:1"),
    (1, {"failed": 1, "invalid": 2}, "failed:1,invalid:2"),
    (1, {"deferred_budget": 1}, "execution_failed:1,deferred_budget:1"),
    (4, {"awaiting_approval": 1}, "awaiting_approval:1"),
    (0, {}, "none"),
])
def test_command_counters_and_reasons_are_safe(monkeypatch, capsys, code, counts, expected):
    report = FeedbackStageReport(**counts, errors=["PRIVATE_ERROR"])
    monkeypatch.setattr(commands, "run_feedback_command",
                        lambda *a, **k: FeedbackCommandResult(code, report=report))
    settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[]), timezone="UTC")
    args = SimpleNamespace(feedback_cmd="run", force=False, task=None, fake_llm=True,
                           at=None, dry_run=True)
    assert commands.dispatch(args, settings) == code
    output = capsys.readouterr()
    assert f"feedback exit={code} reasons={expected}" in output.err
    assert '"not_started_docs":' in output.out and '"preview_unapproved":' in output.out
    assert "PRIVATE_ERROR" not in output.out + output.err


def test_general_run_shows_feedback_counters_without_private_stats(tmp_path, monkeypatch, capsys):
    fake, conn, backend, client, llm, settings = setup_env(tmp_path)
    settings.logging = SimpleNamespace(log_file=tmp_path / "test.log")
    monkeypatch.setattr(cli, "_configure_logging", lambda _: tmp_path / "test.log")
    monkeypatch.setattr(cli, "_client", lambda _: client)
    monkeypatch.setattr(cli, "run_pipeline", lambda *a, **k: RunResult(
        1, "2026-11-12", "ok", {"feedback": {"not_started_docs": 1, "private": "PRIVATE_VALUE"}}))
    args = SimpleNamespace(dry_run=True, fake_llm=True, date=None, no_feedback=False)
    assert cli._cmd_run(args, settings, conn) == 0
    output = capsys.readouterr().out
    assert '"not_started_docs": 1' in output and "PRIVATE_VALUE" not in output
    conn.close()
