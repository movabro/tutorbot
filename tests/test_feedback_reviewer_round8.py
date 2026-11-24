import copy
import itertools
import logging
import multiprocessing
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.fakes import FakeDynalist
from tests.test_feedback_foundations import task
from tests.test_feedback_reviewer_round3 import _crash_worker
from tests.test_feedback_reviewer_round6 import NOW, _success_task
from tutorbot import db
from tutorbot.dynalist.client import DynalistClient
from tutorbot.feedback import commands, output, persona, service, store
from tutorbot.feedback.flush_adapter import flush_feedback_safe
from tutorbot.feedback.models import FeedbackRulesCfg, NodeRef, OutputSpec, UnitSpec
from tutorbot.pipeline import FileLock


@pytest.mark.parametrize("answers,change,approved", [
    (["bad", "y"], False, False),
    (["hash", "n"], False, False),
    (["hash", "y"], True, False),
    (["hash", "y"], False, True),
])
@pytest.mark.parametrize("entry", ["run", "approve"])
def test_private_manual_review(tmp_path, monkeypatch, capsys, answers, change, approved, entry):
    private = "PRIVATE PERSONA CONTENT"
    path = tmp_path / "secret-persona.md"
    path.write_text(private + "\r\n", encoding="utf-8")
    spec = replace(task(), base_dir=tmp_path, persona_path=path.name,
                   persona_approval="user_approved_before_use")
    p = persona.resolve_persona(spec)
    inputs = iter([p.hash if answer == "hash" else answer for answer in answers])

    def answer(prompt):
        value = next(inputs)
        if change and prompt.startswith("Approve"):
            path.write_text("changed", encoding="utf-8")
        return value

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    database = tmp_path / "test.sqlite"
    conn = db.connect(database)
    db.migrate(conn)
    if entry == "run":
        result = persona.approval_gate(conn, spec, p, interactive=True, dry_run=False)
        assert result.ok is approved
    else:
        settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[spec]), db_path=database)
        args = SimpleNamespace(feedback_cmd="approve", all=False, task=spec.id, revoke=False,
                               yes=False, hash=None)
        assert commands.dispatch(args, settings) == (0 if approved else 4)
    assert conn.execute("SELECT COUNT(*) FROM feedback_persona_approval").fetchone()[0] == int(approved)
    captured = capsys.readouterr()
    assert private not in captured.out + captured.err
    assert path.name not in captured.out + captured.err
    assert spec.id not in captured.out + captured.err
    conn.close()


@pytest.mark.parametrize("mode", ["yes", "non_tty", "eof_hash", "eof_confirm"])
def test_approval_rejects_automatic_or_incomplete_consent(tmp_path, monkeypatch, mode):
    path = tmp_path / "persona.md"
    path.write_text("example\n", encoding="utf-8")
    spec = replace(task(), base_dir=tmp_path, persona_path=path.name,
                   persona_approval="approve_each_run")
    digest = persona.resolve_persona(spec).hash
    database = tmp_path / "test.sqlite"
    conn = db.connect(database)
    db.migrate(conn)
    settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[spec]), db_path=database)
    args = SimpleNamespace(feedback_cmd="approve", all=False, task=spec.id, revoke=False,
                           yes=mode == "yes", hash=digest if mode == "yes" else None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: mode != "non_tty")
    answers = iter([digest] if mode == "eof_confirm" else [])

    def answer(_):
        value = next(answers, None)
        if value is None:
            raise EOFError
        return value

    monkeypatch.setattr("builtins.input", answer)
    assert commands.dispatch(args, settings) == 4
    assert conn.execute("SELECT COUNT(*) FROM feedback_persona_approval").fetchone()[0] == 0
    conn.close()


def test_global_lock_reports_fixed_message_to_log_and_stderr(tmp_path, capsys, caplog):
    spec = task()
    settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[spec]),
                               db_path=tmp_path / "db.sqlite")
    args = SimpleNamespace(feedback_cmd="run", force=False, task=None, fake_llm=True,
                           at=None, dry_run=True)
    with FileLock(tmp_path / "tutorbot.lock"), caplog.at_level(logging.WARNING):
        assert commands.dispatch(args, settings) == 75
    captured = capsys.readouterr()
    assert "tutorbot is already running" in captured.err
    assert "tutorbot is already running" not in captured.out
    assert any(r.levelno == logging.WARNING and r.message == "tutorbot is already running"
               for r in caplog.records)
    assert spec.id not in captured.err + caplog.text


@pytest.mark.parametrize("point", ["before", "after", "done"])
def test_killed_writer_recovery_controls_success_window(tmp_path, point):
    path = tmp_path / "outbox.sqlite"
    conn = db.connect(path)
    db.migrate(conn)
    spec = replace(_success_task(), unit="aggregate", domain="career",
                   output=OutputSpec("under_parent", "doc", "root", "career_note"))
    anchor = (NOW - timedelta(days=1)).isoformat()
    assert store.success_window_lower(conn, spec.id, spec.config_hash(), anchor) == anchor
    run_id = conn.execute("INSERT INTO run_log(run_date,started_at,status) VALUES(?,?,'running')",
                          (NOW.date().isoformat(), NOW.isoformat())).lastrowid
    task_run = store.insert_run(conn, task_id=spec.id, cycle_key="2026-11-24",
                                trigger_kind="scheduled", schedule="daily", started_at=NOW.isoformat(),
                                status="succeeded", config_hash=spec.config_hash(),
                                run_id=run_id, units_total=1, units_ok=1)
    ref = NodeRef("doc", "root")
    output.enqueue_result(conn, task=spec, task_run_id=task_run, run_id=run_id,
                          unit=UnitSpec("agg", (), (ref,)), write_to=ref,
                          out={"summary": "example", "feedback": ["advice"], "evidence": []},
                          local_date="2026-11-24", bot_tag="tutorbot", truncated=False,
                          over_target=False, snapshot=None)
    store.update_run(conn, task_run, outputs_enqueued=1)
    conn.execute("UPDATE write_op SET attempts=4,status='failed'")
    fake = FakeDynalist()
    fake.add_doc("doc")
    conn.close()
    ctx = multiprocessing.get_context("fork")
    with ctx.Manager() as manager:
        shared = manager.dict({"docs": fake.docs})
        ready = ctx.Event()
        worker = ctx.Process(target=_crash_worker, args=(path, shared, ready, point))
        worker.start()
        try:
            assert ready.wait(10)
            worker.kill()
            worker.join(10)
            assert not worker.is_alive()
            conn = db.connect(path)
            try:
                assert store.success_window_lower(conn, spec.id, spec.config_hash(), anchor) == anchor
                recovered = FakeDynalist()
                recovered.docs = copy.deepcopy(shared["docs"])
                recovered._ids = itertools.count(200)
                client = DynalistClient("test", transport=recovered, sleep=lambda _: None)
                flush_feedback_safe(conn, client)
                service.finalize_after_flush(conn, run_id, now_iso=NOW.isoformat())
                lower = store.success_window_lower(conn, spec.id, spec.config_hash(), anchor)
                if point == "before":
                    assert lower == anchor
                    assert len(recovered.children("doc", "root")) == 0
                    assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == "failed"
                else:
                    assert lower == NOW.isoformat()
                    assert len(recovered.children("doc", "root")) == 1
                    assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == "written"
            finally:
                conn.close()
        finally:
            if worker.is_alive():
                worker.kill()
                worker.join(10)
