import json
import multiprocessing
from datetime import datetime, timezone
from types import SimpleNamespace

from tests.fakes import FakeDynalist
from tests.test_feedback_foundations import task
from tests.test_m5 import setup_env
from tutorbot import cli, db
from tutorbot.cli import main
from tutorbot.dynalist.client import DynalistClient
from tutorbot.feedback import commands, store
from tutorbot.feedback.models import FeedbackRulesCfg
from tutorbot.feedback.service import run_feedback_command
from tutorbot.pipeline import FileLock, run_pipeline


def _hold_lock(path, ready, release):
    with FileLock(path):
        ready.set()
        release.wait(10)


def test_pipeline_preview_then_real_does_not_consume_cycle(tmp_path):
    fake, conn, backend, client, llm, settings = setup_env(tmp_path)
    fake.add_doc("doc")
    fake.add("doc", "root", "safe example")
    settings.feedback_rules = FeedbackRulesCfg(tasks=[task()])
    backend.set("feedback_rule", {"summary": "example", "feedback": ["advice"], "evidence": []})
    before = "\n".join(conn.iterdump())
    old_conn, old_run_id, old_stats = llm.conn, llm.run_id, dict(llm.stats)
    preview = run_pipeline(conn, settings, client, llm, dry_run=True, lock_path=tmp_path / "lock")
    assert preview.stats["feedback"]["ran"] == 1
    assert "\n".join(conn.iterdump()) == before
    assert llm.conn is old_conn and llm.run_id == old_run_id
    assert llm.stats == old_stats
    assert not any(path == "doc/edit" for path, _ in fake.calls)
    actual = run_pipeline(conn, settings, client, llm, lock_path=tmp_path / "lock")
    assert actual.stats["feedback"]["ran"] == 1
    assert conn.execute("SELECT COUNT(*) FROM feedback_task_run").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM feedback_output WHERE status='written'").fetchone()[0] == 1
    assert any(path == "doc/edit" for path, _ in fake.calls)
    conn.close()


def test_allow_placeholders_cli(capsys, tmp_path):
    code = main(["-c", str(tmp_path / "absent.toml"), "feedback", "validate",
                 "--rules-file", "docs/templates/feedback-rules.example.toml",
                 "--base-dir", ".", "--allow-placeholders", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["summary"]["valid"] == 4 and data["summary"]["errors"] == 0
    assert data["summary"]["warnings"] > 0


def test_online_read_only_counts_and_missing_parent(tmp_path, monkeypatch, capsys):
    fake = FakeDynalist()
    fake.add_doc("doc")
    fake.add("doc", "root", "safe")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    monkeypatch.setattr(cli, "_client", lambda settings: client)
    settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[task()]),
                               db_path=tmp_path / "missing.sqlite3", tags=None)
    args = SimpleNamespace(feedback_cmd="validate", online=True, allow_placeholders=False, json=True)
    assert commands.dispatch(args, settings) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["tasks"][0]["online"]["selection_counts"] == [1]
    assert "첫 실행 하한" in data["history"]
    assert not settings.db_path.exists()
    assert not any(path == "doc/edit" for path, _ in fake.calls)

    settings.feedback_rules.tasks = [task(selection=[{"file_id": "doc", "parent_node_id": "missing",
                                                       "scope": "descendants", "time": "fixed_context"}])]
    assert commands.dispatch(args, settings) == 3
    assert json.loads(capsys.readouterr().out)["tasks"][0]["online"]["errors"]


def test_process_lock_contention_returns_75(tmp_path):
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    path = tmp_path / "lock"
    process = ctx.Process(target=_hold_lock, args=(path, ready, release))
    process.start()
    try:
        assert ready.wait(5)
        settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[task()]),
                                   db_path=tmp_path / "db.sqlite3")
        result = run_feedback_command(settings, dry_run=True, lock_path=path)
        assert result.exit_code == 75
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0


def test_online_errors_do_not_expose_exception(tmp_path, monkeypatch, capsys):
    class BrokenClient:
        def doc_read(self, fid):
            raise RuntimeError("PRIVATE_CONTENT")

    monkeypatch.setattr(cli, "_client", lambda settings: BrokenClient())
    settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[task()]),
                               db_path=tmp_path / "missing.sqlite3", tags=None)
    args = SimpleNamespace(feedback_cmd="validate", online=True, allow_placeholders=False, json=True)
    assert commands.dispatch(args, settings) == 3
    captured = capsys.readouterr()
    assert "PRIVATE_CONTENT" not in captured.out + captured.err


def test_online_uses_readonly_history_for_selection(tmp_path, monkeypatch, capsys):
    now = datetime(2026, 11, 24, 2, tzinfo=timezone.utc)
    last = datetime(2026, 11, 24, 1, tzinfo=timezone.utc)
    path = tmp_path / "history.sqlite3"
    conn = db.connect(path)
    db.migrate(conn)
    store.insert_run(conn, task_id="test", cycle_key="old", trigger_kind="scheduled",
                     schedule="daily", started_at=last.isoformat(), status="skipped", config_hash="v1:test")
    before = "\n".join(conn.iterdump())
    fake = FakeDynalist()
    fake.add_doc("doc")
    fake.add("doc", "root", "old", created=int(last.timestamp() * 1000) - 1000)
    fake.add("doc", "root", "new", created=int(last.timestamp() * 1000) + 1000)
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    monkeypatch.setattr(cli, "_client", lambda settings: client)
    monkeypatch.setattr(commands.clock, "now", lambda: now)
    spec = task(selection=[{"file_id": "doc", "parent_node_id": "root",
                            "scope": "descendants", "time": "created_since_last_run"}])
    settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[spec]), db_path=path, tags=None)
    args = SimpleNamespace(feedback_cmd="validate", online=True, allow_placeholders=False, json=True)
    assert commands.dispatch(args, settings) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["tasks"][0]["online"]["selection_counts"] == [1]
    assert data["history"] == "history available"
    assert "\n".join(conn.iterdump()) == before
    conn.close()
