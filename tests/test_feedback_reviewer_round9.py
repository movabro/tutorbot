import multiprocessing
import os
import signal
import tracemalloc
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.fakes import FakeDynalist, FakeLLM
from tests.test_feedback_foundations import task
from tests.test_feedback_reviewer_round6 import NOW, _environment, _success_task
from tests.test_m5 import make_settings
from tutorbot import clock, db
from tutorbot.dynalist.client import DynalistClient
from tutorbot.dynalist.tree import NodeIndex
from tutorbot.feedback import prompt, service, store
from tutorbot.feedback.models import (
    ConfigIssue,
    ContextLine,
    FeedbackRulesCfg,
    NodeRef,
    ReferenceBlock,
    ScheduleSpec,
    TargetBlock,
)
from tutorbot.feedback.sources import SelectedNode
from tutorbot.feedback.tokens import estimate_tokens
from tutorbot.llm.client import LLMClient
from tutorbot.pipeline import FileLock, run_pipeline


def _hold_locks(path, ready, release):
    with FileLock(path.parent / "tutorbot.lock"):
        conn = db.connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            ready.set()
            release.wait(15)
            conn.execute("ROLLBACK")
        finally:
            conn.close()


@pytest.mark.parametrize("dry_run", [False, True])
def test_global_lock_precedes_real_database_lock_and_migration(tmp_path, dry_run):
    path = tmp_path / "db.sqlite"
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute("DROP TABLE feedback_success_anchor")
    conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    conn.close()
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    worker = ctx.Process(target=_hold_locks, args=(path, ready, release))
    worker.start()
    try:
        assert ready.wait(10)
        settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[task()]), db_path=path)
        result = service.run_feedback_command(settings, dry_run=dry_run)
        assert result.exit_code == 75
        conn = db.connect(path)
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
        conn.close()
    finally:
        release.set()
        worker.join(10)
        if worker.is_alive():
            worker.kill()
            worker.join(10)
    assert worker.exitcode == 0


@pytest.mark.parametrize("scenario", ["skip", "docs", "persona", "over", "large", "unit", "config"])
def test_service_logs_and_errors_do_not_expose_identifiers(tmp_path, caplog, monkeypatch, scenario):
    secret = "PRIVATE_TASK_AND_PATH_XYZ"
    spec = replace(task(), id=secret)
    if scenario == "skip":
        spec = replace(spec, schedule=ScheduleSpec("quarterly", quarter_anchor_invalid=secret))
    if scenario == "persona":
        spec = replace(spec, base_dir=tmp_path, persona_path="missing", persona_approval="always_allowed")
    conn, _fake, _node, client, _backend, llm, settings = _environment(spec)
    try:
        if scenario == "config":
            settings.feedback_rules.issues.append(ConfigIssue("FR-G001", "error", secret, secret))
        if scenario in ("over", "large", "unit"):
            if scenario == "large":
                def build(*args):
                    raise prompt.InputTooLarge(secret)
            else:
                def build(*args):
                    return [], prompt.FitResult(False, scenario == "over", False, 0)
            monkeypatch.setattr(prompt, "build_messages", build)
            if scenario == "unit":
                def fail(*args, **kwargs):
                    raise ValueError(secret)
                monkeypatch.setattr(llm, "complete_json", fail)
        run_id = conn.execute("INSERT INTO run_log(run_date,started_at,status) VALUES(?,?,'running')",
                              (NOW.date().isoformat(), NOW.isoformat())).lastrowid
        indices = {} if scenario == "docs" else {"doc": NodeIndex.from_doc_read("doc", client.doc_read("doc"))}
        report = service.run_feedback_stage(conn, settings, llm, indices, T=NOW, run_id=run_id, dry_run=False)
        assert secret not in caplog.text
        assert secret not in str(report.errors)
        assert caplog.records
    finally:
        conn.close()


def _crash_started(path, settings, fake, ready, after_one):
    clock.set_now(NOW)
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    backend = FakeLLM()
    backend.set("feedback_rule", {"summary": "example", "feedback": ["advice"], "evidence": []})
    llm = LLMClient(backend=backend, sleep=lambda _: None)
    original = llm.complete_json
    calls = 0

    def pause(*args, **kwargs):
        nonlocal calls
        calls += 1
        if after_one and calls == 1:
            return original(*args, **kwargs)
        ready.set()
        os.kill(os.getpid(), signal.SIGSTOP)

    llm.complete_json = pause
    service.run_feedback_command(settings, dynalist=client, llm=llm)


@pytest.mark.parametrize("entry", ["pipeline", "feedback"])
@pytest.mark.parametrize("after_one", [False, True])
def test_killed_started_run_is_interrupted_on_both_entrypoints(tmp_path, entry, after_one):
    settings = make_settings(tmp_path)
    settings.domains = {}
    spec = _success_task()
    settings.feedback_rules = FeedbackRulesCfg(tasks=[spec])
    path = settings.db_path
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    fake = FakeDynalist()
    fake.add_doc("doc")
    fake.add("doc", "root", "synthetic", created=int((NOW - timedelta(hours=1)).timestamp() * 1000))
    if after_one:
        fake.add("doc", "root", "synthetic 2", created=int((NOW - timedelta(hours=1)).timestamp() * 1000))
    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    worker = ctx.Process(target=_crash_started, args=(path, settings, fake, ready, after_one))
    worker.start()
    try:
        assert ready.wait(10)
        conn = db.connect(path)
        assert conn.execute("SELECT status FROM feedback_task_run").fetchone()[0] == "started"
        assert conn.execute("SELECT count(*) FROM feedback_output").fetchone()[0] == int(after_one)
        conn.close()
        worker.kill()
        worker.join(10)
        assert worker.exitcode == -signal.SIGKILL
        clock.set_now(NOW)
        client = DynalistClient("test", transport=fake, sleep=lambda _: None)
        llm = LLMClient(backend=FakeLLM(), sleep=lambda _: None)
        conn = db.connect(path)
        try:
            anchor = (NOW - timedelta(days=1)).isoformat()
            assert store.success_window_lower(conn, spec.id, spec.config_hash(), anchor) == anchor
            if entry == "pipeline":
                result = run_pipeline(conn, settings, client, llm, run_date=NOW.date().isoformat())
                assert result.status == "ok"
            else:
                result = service.run_feedback_command(settings, dynalist=client, llm=llm, conn=conn)
                assert result.exit_code == 0
            assert tuple(conn.execute("SELECT status,error FROM feedback_task_run").fetchone()) == (
                "failed", "interrupted")
            assert conn.execute("SELECT count(*) FROM feedback_task_lock").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM feedback_task_run").fetchone()[0] == 1
            if after_one:
                assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == "written"
                assert conn.execute("SELECT count(*) FROM write_op WHERE status!='done'").fetchone()[0] == 0
            assert store.success_window_lower(conn, spec.id, spec.config_hash(), anchor) == anchor
        finally:
            conn.close()
    finally:
        clock.set_now(None)
        if worker.is_alive():
            worker.kill()
            worker.join(10)


def test_deep_reference_is_counted_before_allocation():
    spec = replace(task(), input_token_range=(1, 2000))
    selected = SelectedNode(NodeRef("doc", "target"), 0, "self")
    targets = [TargetBlock(selected, (ContextLine(selected.ref, 0, "target", None, 1),), 1)]
    lines = tuple(ContextLine(NodeRef("doc", str(i)), i, "line\nmore", "note\nmore", 1)
                  for i in range(5000))
    refs = [ReferenceBlock(0, spec.selection[0], lines, 5000)]
    tracemalloc.start()
    try:
        messages, fit = prompt.build_messages(spec, None, None, targets, refs, NOW, 8000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    assert fit.truncated and not fit.over_target
    assert fit.tokens == estimate_tokens("".join(m["content"] for m in messages))
    text = messages[1]["content"]
    assert "[참조 1: 앞 " in text and "/5000개 노드만 포함]" in text
    included = sum(f"[doc/{i}]" in text for i in range(5000))
    assert 0 < included < 5000
    assert f"[doc/{included - 1}]" in text and f"[doc/{included}]" not in text


def test_unavoidable_deep_target_rejected_without_allocating_indentation():
    spec = replace(task(), input_token_range=(1, 2000))
    selected = SelectedNode(NodeRef("doc", "target"), 0, "self")
    targets = [TargetBlock(selected, (ContextLine(selected.ref, 5_000_000, "x", None, 1),), 1)]
    tracemalloc.start()
    try:
        with pytest.raises(prompt.InputTooLarge):
            prompt.build_messages(spec, None, None, targets, [], NOW, 8000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
