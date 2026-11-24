import copy
import itertools
import logging
import multiprocessing
import os
import signal
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.fakes import FakeDynalist
from tests.test_feedback_foundations import task
from tutorbot import cli, db
from tutorbot.dynalist.client import DynalistClient
from tutorbot.dynalist.writer import enqueue_op
from tutorbot.feedback import commands, persona
from tutorbot.feedback.flush_adapter import flush_feedback_safe
from tutorbot.feedback.models import FeedbackRulesCfg
from tutorbot.pipeline import FileLock, save_devlog_run_log


def _outbox(conn, attempts, *, parent="root", kind="feedback_rule"):
    enqueue_op(conn, op_key="fr:test", file_id="doc", action="insert", parent_ref=f"node:{parent}",
               payload={"content": "synthetic"}, bot_kind=kind, marker=f"tb:{kind}:test")
    conn.execute("UPDATE write_op SET attempts=?,status='failed' WHERE op_key='fr:test'", (attempts,))


@pytest.mark.parametrize("attempts", [1, 4, 5])
def test_writer_pre_reconcile_rejects_duplicate(attempts):
    conn = db.connect(":memory:")
    db.migrate(conn)
    _outbox(conn, attempts)
    if attempts == 5:
        conn.execute("UPDATE write_op SET last_error='unknown_outcome_reconcile'")
    fake = FakeDynalist()
    fake.add_doc("doc")
    fake.add("doc", "root", "a", "tb:feedback_rule:test")
    fake.add("doc", "root", "b", "tb:feedback_rule:test")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    report = flush_feedback_safe(conn, client)
    assert report.errors
    assert conn.execute("SELECT status FROM write_op").fetchone()[0] == "failed"
    assert not any(path == "doc/edit" for path, _ in fake.calls)
    conn.close()


def test_preflight_read_failure_never_uses_stale_index():
    from tutorbot.dynalist.tree import NodeIndex

    conn = db.connect(":memory:")
    db.migrate(conn)
    _outbox(conn, 1)
    fake = FakeDynalist()
    fake.add_doc("doc")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    stale = NodeIndex.from_doc_read("doc", client.doc_read("doc"))
    fake.add("doc", "root", "already written", "tb:feedback_rule:test")
    fake.fail("doc/read", "NotFound")
    assert flush_feedback_safe(conn, client, indices={"doc": stale}).errors
    assert fake.count_nodes("doc") == 1
    assert conn.execute("SELECT status FROM write_op").fetchone()[0] == "failed"
    assert flush_feedback_safe(conn, client).reconciled == 1
    assert fake.count_nodes("doc") == 1
    conn.close()


def test_devlog_obeys_global_lock(tmp_path):
    conn = db.connect(":memory:")
    db.migrate(conn)
    settings = SimpleNamespace(db_path=tmp_path / "live.db",
                               domains={"devlog": SimpleNamespace(enabled=True, file_ids=["doc"])})
    fake = FakeDynalist()
    fake.add_doc("doc")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    with FileLock(tmp_path / "tutorbot.lock"), pytest.raises(RuntimeError):
        save_devlog_run_log(conn, settings, client, run_id=1, run_date="2026-01-01",
                            run_time="00:00:00", log_text="synthetic")
    assert not fake.calls
    conn.close()


def test_devlog_flush_does_not_resend_uncertain_feedback(tmp_path):
    conn = db.connect(":memory:")
    db.migrate(conn)
    run_id = conn.execute("INSERT INTO run_log(run_date,started_at,status) "
                          "VALUES('2026-01-01','2026-01-01','running')").lastrowid
    _outbox(conn, 1)
    fake = FakeDynalist()
    fake.add_doc("doc")
    fake.add_doc("devlog")
    fake.add("doc", "root", "already written", "tb:feedback_rule:test")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    original = client.doc_read

    def broken(fid):
        if fid == "doc":
            raise RuntimeError("private read failure")
        return original(fid)

    client.doc_read = broken
    settings = SimpleNamespace(db_path=tmp_path / "live.db",
                               domains={"devlog": SimpleNamespace(enabled=True, file_ids=["devlog"])})
    report = save_devlog_run_log(conn, settings, client, run_id=run_id, run_date="2026-01-01",
                                 run_time="00:00:00", log_text="synthetic")
    assert report.errors and not any(path == "doc/edit" and body["file_id"] == "doc"
                                     for path, body in fake.calls)
    assert any(path == "doc/edit" and body["file_id"] == "devlog" for path, body in fake.calls)
    assert fake.count_nodes("doc") == 1
    client.doc_read = original
    assert flush_feedback_safe(conn, client).reconciled == 1
    assert fake.count_nodes("doc") == 1
    conn.close()


def test_run_preview_backs_up_live_wal_without_files(tmp_path, monkeypatch):
    path = tmp_path / "live.db"
    source = db.connect(path)
    source.execute("PRAGMA wal_autocheckpoint=0")
    source.execute("CREATE TABLE synthetic (v TEXT)")
    source.execute("INSERT INTO synthetic VALUES ('wal-only')")
    assert path.with_name(path.name + "-wal").exists()
    settings = SimpleNamespace(db_path=path)
    monkeypatch.setattr(cli, "_cmd_run", lambda args, settings, conn:
                        conn.execute("SELECT v FROM synthetic").fetchone()[0])
    assert cli.cmd_run(SimpleNamespace(dry_run=True, fake_llm=True), settings) == "wal-only"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live.db", "live.db-shm", "live.db-wal"]
    source.close()


def test_real_preview_requires_consent_before_call(monkeypatch, capsys):
    cfg = FeedbackRulesCfg(tasks=[task()])
    settings = SimpleNamespace(feedback_rules=cfg, timezone="UTC")
    args = SimpleNamespace(feedback_cmd="run", force=False, task=None, fake_llm=False,
                           at=None, dry_run=True, allow_external_llm=False)
    monkeypatch.setattr(commands, "run_feedback_command", lambda *a, **k: pytest.fail("must not call"))
    assert commands.dispatch(args, settings) == 2
    out = capsys.readouterr().out
    assert "선택·참조" in out and "비용" in out and "상한" in out


def test_doctor_and_approval_do_not_print_private_values(tmp_path, monkeypatch, capsys):
    fake = FakeDynalist()
    fake.add_doc("private-document")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    monkeypatch.setattr(cli, "_client", lambda _: client)
    settings = SimpleNamespace(domains={"work": SimpleNamespace(enabled=True, file_ids=["private-document"])})
    assert cli.cmd_doctor(SimpleNamespace(write_test=False), settings) == 0
    assert "private-document" not in capsys.readouterr().out

    (tmp_path / "private-persona.md").write_text("private persona contents", encoding="utf-8")
    spec = replace(task(), base_dir=tmp_path, persona_path="private-persona.md",
                   persona_approval="user_approved_before_use")
    conn = db.connect(":memory:")
    db.migrate(conn)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert not persona.approval_gate(conn, spec, persona.resolve_persona(spec),
                                     interactive=True, dry_run=False).ok
    text = capsys.readouterr().out
    assert "private persona contents" not in text and "private-persona.md" not in text
    conn.close()


def test_error_paths_and_component_logs_are_redacted(tmp_path, monkeypatch, capsys):
    class Broken:
        def file_list(self):
            raise RuntimeError("PRIVATE_EXCEPTION /private/path")

    monkeypatch.setattr(cli, "_client", lambda _: Broken())
    assert cli.cmd_doctor(SimpleNamespace(write_test=False), SimpleNamespace(domains={})) == 1
    captured = capsys.readouterr()
    assert "PRIVATE_EXCEPTION" not in captured.err and "/private/path" not in captured.err
    record = logging.LogRecord("tutorbot.parse.classify", logging.WARNING, __file__, 1,
                               "node %s", ("PRIVATE_ID",), None)
    assert cli._SafeLogFilter().filter(record)
    assert "PRIVATE_ID" not in record.getMessage()
    args = ["-c", str(tmp_path / "absent.toml"), "feedback", "validate",
            "--rules-file", str(tmp_path / "PRIVATE_PATH.toml")]
    assert cli.main(args) == 3
    captured = capsys.readouterr()
    assert "PRIVATE_PATH" not in captured.out + captured.err


def test_status_ignores_invalid_feedback_config(tmp_path, capsys):
    path = tmp_path / "history.sqlite3"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    config = tmp_path / "bad.toml"
    config.write_text(f'db_path = "{path}"\n[feedback_rules]\ntasks = [broken\n', encoding="utf-8")
    assert cli.main(["-c", str(config), "feedback", "status", "--json"]) == 0
    assert capsys.readouterr().out.strip() == "[]"


def test_online_placeholder_warning_does_not_read_placeholder(tmp_path, monkeypatch):
    from tutorbot.feedback.loader import parse_feedback_rules

    raw = {"id": "test", "purpose": "test", "unit": "per_node", "schedule": "daily",
           "selection": [{"file_id": "<document>", "parent_node_id": "<parent>",
                          "scope": "descendants", "time": "fixed_context"}], "output": {}}
    spec = parse_feedback_rules({"tasks": [raw]}, base_dir=tmp_path, allow_placeholders=True).tasks[0]
    monkeypatch.setattr(cli, "_client", lambda _: SimpleNamespace(
        doc_read=lambda _: pytest.fail("placeholder sent to network")))
    settings = SimpleNamespace(db_path=tmp_path / "absent", tags=None)
    result, _ = commands._online_validate(settings, [spec], allow_placeholders=True)
    assert not result[spec.index]["errors"]
    assert result[spec.index]["warnings"]


def test_online_success_filter_on_old_db_uses_temporary_migration(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute("DROP TABLE feedback_success_anchor")
    conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    conn.close()
    fake = FakeDynalist()
    fake.add_doc("doc")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    monkeypatch.setattr(cli, "_client", lambda _: client)
    spec = task(selection=[{"file_id": "doc", "parent_node_id": "root",
                            "scope": "descendants", "time": "created_since_last_success"}])
    result, available = commands._online_validate(SimpleNamespace(db_path=path, tags=None), [spec])
    assert available and not result[spec.index]["errors"]
    conn = db.connect(path)
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='feedback_success_anchor'").fetchone()
    conn.close()


def test_prompt_discloses_reference_and_target_omissions():
    from tutorbot.feedback.models import (
        ContextLine,
        NodeRef,
        ReferenceBlock,
        TargetBlock,
    )
    from tutorbot.feedback.prompt import build_messages
    from tutorbot.feedback.sources import SelectedNode

    spec = replace(task(), unit="aggregate", input_token_range=(1, 500))
    selected = [SelectedNode(NodeRef("doc", f"n{i}"), 0, "self") for i in range(3)]
    targets = [TargetBlock(item, (ContextLine(item.ref, 0, "x" * 800, None, 200),), 200)
               for item in selected]
    lines = tuple(ContextLine(NodeRef("doc", f"r{i}"), 0, "r" * 800, None, 200) for i in range(5))
    refs = [ReferenceBlock(0, spec.selection[0], lines, 1000)]
    messages, fit = build_messages(spec, None, None, targets, refs,
                                   datetime(2026, 1, 1, tzinfo=timezone.utc), 10000)
    text = messages[1]["content"]
    assert fit.truncated
    assert "[참조 1: 앞 0/5개 노드만 포함]" in text
    assert "생략된 대상:" in text


def _crash_worker(db_path, shared, ready, point):
    from tutorbot.dynalist import writer

    conn = db.connect(db_path)
    fake = FakeDynalist()
    fake.docs = copy.deepcopy(shared["docs"])
    fake._ids = itertools.count(100)
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    original_edit = client.doc_edit

    def pause():
        ready.set()
        os.kill(os.getpid(), signal.SIGSTOP)

    def edit(fid, changes):
        if point == "before":
            pause()
        result = original_edit(fid, changes)
        shared["docs"] = copy.deepcopy(fake.docs)
        if point == "after":
            pause()
        return result

    client.doc_edit = edit
    original_done = writer._apply_domain_done

    def done(*args):
        if point == "done":
            pause()
        return original_done(*args)

    writer._apply_domain_done = done
    flush_feedback_safe(conn, client)
    conn.close()


@pytest.mark.parametrize("point", ["before", "after", "done"])
@pytest.mark.parametrize("kind", ["feedback_rule", "feedback_rule_item"])
def test_kill_fifth_attempt_recovers_without_duplicate(tmp_path, point, kind):
    path = tmp_path / "outbox.sqlite3"
    conn = db.connect(path)
    db.migrate(conn)
    fake = FakeDynalist()
    fake.add_doc("doc")
    parent = fake.add("doc", "root", "parent") if kind == "feedback_rule_item" else "root"
    _outbox(conn, 4, parent=parent, kind=kind)
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
            recovered = FakeDynalist()
            recovered.docs = copy.deepcopy(shared["docs"])
            recovered._ids = itertools.count(200)
            client = DynalistClient("test", transport=recovered, sleep=lambda _: None)
            conn = db.connect(path)
            flush_feedback_safe(conn, client)
            row = conn.execute("SELECT status,last_error FROM write_op").fetchone()
            if point == "before":
                assert tuple(row) == ("failed", "write_failed")
                assert len(recovered.children("doc", parent)) == 0
            else:
                assert row["status"] == "done"
                assert len(recovered.children("doc", parent)) == 1
            conn.close()
        finally:
            if worker.is_alive():
                worker.kill()
                worker.join(10)


def _cycle_worker(path, barrier, results):
    from tutorbot.feedback import store

    conn = db.connect(path)
    barrier.wait(10)
    rid = store.insert_run(conn, task_id="test", cycle_key="2026-01-01", trigger_kind="scheduled",
                           schedule="daily", started_at="2026-01-01T00:00:00+00:00",
                           status="started", config_hash="v1:test")
    results.put(rid is not None)
    conn.close()


def test_two_processes_contend_for_same_cycle(tmp_path):
    path = tmp_path / "cycles.sqlite3"
    conn = db.connect(path)
    db.migrate(conn)
    ctx = multiprocessing.get_context("fork")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    workers = [ctx.Process(target=_cycle_worker, args=(path, barrier, results)) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        outcomes = [results.get(timeout=10) for _ in workers]
        assert sorted(outcomes) == [False, True]
        assert conn.execute("SELECT COUNT(*) FROM feedback_task_run").fetchone()[0] == 1
    finally:
        for worker in workers:
            worker.join(10)
            if worker.is_alive():
                worker.kill()
                worker.join(10)
        conn.close()


def _task_lock_worker(path, barrier, release, results):
    from tutorbot.feedback import store

    conn = db.connect(path)
    barrier.wait(10)
    lock = store.try_lock(conn, "test", now_iso="2026-01-01T00:00:00+00:00",
                          stale_minutes=60, host="synthetic", pid=os.getpid())
    results.put(lock.acquired)
    release.wait(10)
    if lock.acquired:
        store.release_lock(conn, "test", lock.token)
    conn.close()


def test_two_processes_contend_for_same_task_lock(tmp_path):
    path = tmp_path / "locks.sqlite3"
    conn = db.connect(path)
    db.migrate(conn)
    ctx = multiprocessing.get_context("fork")
    barrier, release, results = ctx.Barrier(2), ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=_task_lock_worker, args=(path, barrier, release, results))
               for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        assert sorted(results.get(timeout=10) for _ in workers) == [False, True]
    finally:
        release.set()
        for worker in workers:
            worker.join(10)
            if worker.is_alive():
                worker.kill()
                worker.join(10)
        conn.close()
