from dataclasses import replace
from types import SimpleNamespace

from tests.fakes import FakeDynalist, FakeLLM
from tests.test_feedback_foundations import task
from tests.test_m5 import setup_env
from tutorbot import cli, db
from tutorbot.config import DomainProfileCfg
from tutorbot.dynalist.client import DynalistClient
from tutorbot.dynalist.writer import enqueue_op
from tutorbot.feedback.flush_adapter import flush_feedback_safe
from tutorbot.feedback.models import FeedbackRulesCfg, OutputSpec
from tutorbot.feedback.service import run_feedback_command
from tutorbot.llm.client import LLMClient
from tutorbot.pipeline import RunResult, run_pipeline


def test_missing_parent_during_generation_fails_command(tmp_path):
    conn = db.connect(":memory:")
    db.migrate(conn)
    fake = FakeDynalist()
    fake.add_doc("doc")
    fake.add("doc", "root", "selection")
    fake.add_doc("out")
    parent = fake.add("out", "root", "destination")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    backend = FakeLLM()

    def generate(_):
        fake.docs["out"]["root"]["children"].remove(parent)
        del fake.docs["out"][parent]
        return {"summary": "example", "feedback": ["advice"], "evidence": []}

    backend.set("feedback_rule", generate)
    llm = LLMClient(backend=backend)
    spec = replace(task(), output=OutputSpec("under_parent", "out", parent))
    settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[spec]), db_path=":memory:",
                               timezone="UTC", llm=SimpleNamespace(daily_token_budget=10000))
    result = run_feedback_command(settings, conn=conn, dynalist=client, llm=llm,
                                  lock_path=tmp_path / "lock")
    assert result.exit_code == 1
    assert conn.execute("SELECT status FROM feedback_task_run").fetchone()[0] == "failed"
    assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == "failed"
    assert conn.execute("SELECT status FROM write_op WHERE bot_kind='feedback_rule'").fetchone()[0] == "skipped"
    assert not any(path == "doc/edit" for path, _ in fake.calls)
    conn.close()


def test_uncertain_write_is_not_reported_success(tmp_path):
    conn = db.connect(":memory:")
    db.migrate(conn)
    fake = FakeDynalist()
    fake.add_doc("doc")
    fake.add("doc", "root", "selection")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    backend = FakeLLM()

    def generate(_):
        fake.fail("doc/read", "NotFound")
        return {"summary": "example", "feedback": ["advice"], "evidence": []}

    backend.set("feedback_rule", generate)
    settings = SimpleNamespace(feedback_rules=FeedbackRulesCfg(tasks=[task()]), db_path=":memory:",
                               timezone="UTC", llm=SimpleNamespace(daily_token_budget=10000))
    result = run_feedback_command(settings, conn=conn, dynalist=client, llm=LLMClient(backend=backend),
                                  lock_path=tmp_path / "lock")
    assert result.exit_code == 1
    assert conn.execute("SELECT status FROM feedback_task_run").fetchone()[0] == "succeeded"
    assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == "pending_write"
    assert not any(path == "doc/edit" for path, _ in fake.calls)
    conn.close()


def test_unavailable_document_isolated_from_other_feedback_and_legacy_outbox():
    conn = db.connect(":memory:")
    db.migrate(conn)
    fake = FakeDynalist()
    for fid in ("bad", "good", "legacy"):
        fake.add_doc(fid)
    for fid in ("bad", "good"):
        enqueue_op(conn, op_key=f"fr:{fid}", file_id=fid, action="insert", parent_ref="node:root",
                   payload={"content": "synthetic"}, bot_kind="feedback_rule",
                   marker=f"tb:feedback_rule:{fid}")
        conn.execute("UPDATE write_op SET status='failed',attempts=1 WHERE file_id=?", (fid,))
    enqueue_op(conn, op_key="legacy", file_id="legacy", action="insert", parent_ref="node:root",
               payload={"content": "legacy"}, bot_kind="warning", marker="legacy-marker")
    enqueue_op(conn, op_key="same-file-legacy", file_id="bad", action="insert", parent_ref="node:root",
               payload={"content": "legacy"}, bot_kind="warning", marker="legacy-marker")
    fake.add("good", "root", "previous", "tb:feedback_rule:good")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    original = client.doc_read

    def read(fid):
        if fid == "bad":
            raise RuntimeError("private")
        return original(fid)

    client.doc_read = read
    report = flush_feedback_safe(conn, client)
    assert report.errors and report.reconciled == 1
    assert dict(conn.execute("SELECT op_key,status FROM write_op")) == {
        "fr:bad": "failed", "fr:good": "done", "legacy": "done", "same-file-legacy": "done"}
    assert fake.count_nodes("good") == 1 and fake.count_nodes("legacy") == 1
    assert fake.count_nodes("bad") == 1
    edits = [body for path, body in fake.calls if path == "doc/edit" and body["file_id"] == "bad"]
    assert len(edits) == 1 and edits[0]["changes"][0]["note"] == "legacy-marker"
    conn.close()


def test_read_failure_after_preflight_does_not_block_other_files():
    conn = db.connect(":memory:")
    db.migrate(conn)
    fake = FakeDynalist()
    fake.add_doc("bad")
    fake.add_doc("good")
    for fid in ("bad", "good"):
        enqueue_op(conn, op_key=f"fr:{fid}", file_id=fid, action="insert", parent_ref="node:root",
                   payload={"content": "synthetic"}, bot_kind="feedback_rule",
                   marker=f"tb:feedback_rule:{fid}")
        conn.execute("UPDATE write_op SET status='failed',attempts=1 WHERE file_id=?", (fid,))
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    original = client.doc_read
    reads = {"bad": 0}

    def read(fid):
        if fid == "bad":
            reads[fid] += 1
            if reads[fid] > 1:
                raise RuntimeError("private")
        return original(fid)

    client.doc_read = read
    report = flush_feedback_safe(conn, client)
    assert report.errors
    assert dict(conn.execute("SELECT file_id,status FROM write_op")) == {"bad": "failed", "good": "done"}
    assert fake.count_nodes("bad") == 0 and fake.count_nodes("good") == 1
    conn.close()


def test_incomplete_snapshot_does_not_prove_parent_deleted():
    conn = db.connect(":memory:")
    db.migrate(conn)
    fake = FakeDynalist()
    fake.add_doc("doc")
    enqueue_op(conn, op_key="fr:incomplete", file_id="doc", action="insert",
               parent_ref="node:missing", payload={"content": "synthetic"},
               bot_kind="feedback_rule", marker="tb:feedback_rule:incomplete")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    client.doc_read = lambda _: {"nodes": []}
    report = flush_feedback_safe(conn, client)
    assert report.errors
    assert conn.execute("SELECT status FROM write_op").fetchone()[0] == "pending"
    conn.close()


def test_domain_errors_are_sanitized_before_persistence(tmp_path, monkeypatch):
    fake, conn, backend, client, llm, settings = setup_env(tmp_path)
    secret = "PRIVATE_SECRET"
    document = "PRIVATE_DOCUMENT_ID"
    fake.add_doc(document)
    fake.add(document, "root", "#project synthetic")
    settings.domains["work"] = DomainProfileCfg(file_ids=[document], tags=["project"])
    original = llm.complete_json

    def complete(task_name, *args, **kwargs):
        if task_name == "domain_feedback":
            raise RuntimeError(secret)
        return original(task_name, *args, **kwargs)

    monkeypatch.setattr(llm, "complete_json", complete)
    result = run_pipeline(conn, settings, client, llm, run_date="2026-11-12", lock_path=tmp_path / "lock")
    row = conn.execute("SELECT error_summary FROM run_log WHERE id=?", (result.run_id,)).fetchone()
    assert result.status == "partial" and row[0] == "domain_processing_failed"
    assert secret not in row[0] and document not in row[0]
    assert secret not in str(result.errors) and document not in str(result.errors)
    conn.close()


def test_general_preview_consent_and_real_fake_backend(tmp_path, monkeypatch, capsys):
    fake, conn, backend, client, llm, settings = setup_env(tmp_path)
    conn.close()
    settings.logging = SimpleNamespace(log_file=tmp_path / "test.log")
    monkeypatch.setattr(cli, "_configure_logging", lambda _: tmp_path / "test.log")
    args = SimpleNamespace(dry_run=True, fake_llm=False, allow_external_llm=False,
                           date=None, no_feedback=False)
    monkeypatch.setattr(cli, "_client", lambda _: client)
    called = []

    def run(conn, settings, client, llm, **kwargs):
        called.append(llm.backend)
        return RunResult(1, "2026-11-12", "ok")

    monkeypatch.setattr(cli, "run_pipeline", run)
    assert cli.cmd_run(args, settings) == 2 and not called
    text = capsys.readouterr()
    assert "선택·참조" in text.out and "비용" in text.out and "--allow-external-llm" in text.err
    args.fake_llm = True
    assert cli.cmd_run(args, settings) == 0
    assert isinstance(called[-1], cli._DefaultFakeLLMBackend)
    args.fake_llm = False
    args.allow_external_llm = True
    settings.llm_api_key = lambda: "synthetic"
    assert cli.cmd_run(args, settings) == 0 and called[-1] is None
