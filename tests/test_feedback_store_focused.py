import logging
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tests.fakes import FakeDynalist
from tests.test_feedback_foundations import task
from tutorbot import db
from tutorbot.config import _parse_toml_bytes, _simple_toml_parse, load_settings
from tutorbot.dynalist.client import DynalistClient
from tutorbot.dynalist.writer import flush_outbox
from tutorbot.feedback import output, store
from tutorbot.feedback.models import NodeRef, RulesParseFailure, TargetSnapshot, UnitSpec
from tutorbot.feedback.persona import resolve_persona
from tutorbot.feedback.schedule import decide

NOW = "2026-11-24T00:00:00Z"


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


def insert_run(conn, cycle="2026-11-24", **extra):
    cols = dict(task_id="test", cycle_key=cycle, trigger_kind="scheduled", schedule="daily",
                started_at=NOW, status="started", config_hash="v1:config")
    cols.update(extra)
    return store.insert_run(conn, **cols)


def test_rules_replacement_ignores_task_errors_but_not_global_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("TUTORBOT_FEEDBACK_RULES_FILE", raising=False)
    config = tmp_path / "config.toml"
    rules = tmp_path / "rules.toml"
    rules.write_text('[[feedback_rules.tasks]]\nid="ok"\npurpose="ok"\nunit="per_node"\n'
                     'schedule="daily"\n[[feedback_rules.tasks.selection]]\nfile_id="doc"\n'
                     'parent_node_id="root"\nscope="descendants"\ntime="fixed_context"\n'
                     '[feedback_rules.tasks.output]\n', encoding="utf-8")
    for bad_tasks in ('tasks = "not an array"', 'tasks = [{id = "a", schedule = []}]',
                      "tasks = [" + ",".join("{}" for _ in range(51)) + "]"):
        config.write_text("[feedback_rules]\n" + bad_tasks, encoding="utf-8")
        cfg = load_settings(config, dotenv_path=tmp_path / "none", rules_file=rules).feedback_rules
        parsed = _parse_toml_bytes(config.read_bytes())
        if isinstance(parsed["feedback_rules"], RulesParseFailure):
            # The fallback cannot distinguish unsupported ignored task syntax
            # from unsupported global syntax; its failure remains global.
            assert [issue.code for issue in cfg.issues] == ["FR-G009"]
            assert not cfg.valid_tasks()
        else:
            assert [item.id for item in cfg.valid_tasks()] == ["ok"]
    config.write_text('[feedback_rules]\nenabled="bad"\ntasks="bad"\n', encoding="utf-8")
    cfg = load_settings(config, dotenv_path=tmp_path / "none", rules_file=rules).feedback_rules
    assert [issue.code for issue in cfg.issues] == ["FR-G004"]
    assert not cfg.valid_tasks()


@pytest.mark.parametrize("value,construct", [
    ('[{a=1}]', "inline table"), ('[1, {a=1}]', "inline table"),
    ("07:32:00", "datetime literal"), ("[07:32:00, 09:00:00]", "datetime literal"),
    ("2026-11-24T12:00:00Z", "datetime literal"),
])
def test_fallback_isolates_nested_unsupported(value, construct):
    result = _simple_toml_parse(f"[feedback_rules]\nx = {value}\n[llm]\nrequests_per_minute = 7\n")
    assert result["feedback_rules"] == RulesParseFailure(2, construct)
    assert result["llm"] == {"requests_per_minute": 7}


def test_rules_file_errors_log_and_persona_directory(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("TUTORBOT_FEEDBACK_RULES_FILE", raising=False)
    with caplog.at_level(logging.ERROR):
        cfg = load_settings(None, dotenv_path=tmp_path / "none", rules_file=tmp_path / "missing").feedback_rules
    assert cfg.issues[0].code == "FR-G007"
    assert "FR-G007" in caplog.text
    (tmp_path / "dir.md").mkdir()
    assert resolve_persona(replace(task(), base_dir=tmp_path, persona_path="dir.md")).error == "not_found"


def test_each_run_prioritizes_run_id():
    spec = task(schedule="each_run")
    now = datetime(2026, 11, 24, tzinfo=timezone.utc)
    assert decide(spec, now, lambda _: False, run_id=0).cycle_key == "each_run:0"
    assert decide(spec, now, lambda _: False, run_id=42).cycle_key == "each_run:42"
    assert decide(spec, now, lambda _: False).cycle_key.startswith("each_run:2026")


def test_migration_from_v2_preserves_existing_data(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "migrate.sqlite")
    all_migrations = db._migrations
    monkeypatch.setattr(db, "_migrations", lambda: [m for m in all_migrations() if m[0] <= 2])
    assert db.migrate(conn) == 2
    conn.execute("INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-24',?,'ok')", (NOW,))
    monkeypatch.setattr(db, "_migrations", all_migrations)
    assert db.migrate(conn) == db.migrate(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM run_log").fetchone()[0] == 1
    assert store.has_feedback_tables(conn)
    conn.close()


def test_approval_consumption_uniqueness_and_rollback(conn):
    approval = store.grant_approval(conn, "test", "p.md", "hash", "once", NOW)
    assert store.grant_approval(conn, "test", "p.md", "hash", "once", NOW) == approval
    rid = insert_run(conn, persona_hash="hash", consume_approval_id=approval)
    assert store.find_approval(conn, "test", "hash", "once") is None
    approval2 = store.grant_approval(conn, "test", "p.md", "hash", "once", NOW)
    assert insert_run(conn, persona_hash="hash", consume_approval_id=approval2) is None
    assert store.find_approval(conn, "test", "hash", "once")[0] == approval2
    with pytest.raises(ValueError):
        insert_run(conn, "other", persona_hash="wrong", consume_approval_id=approval2)
    assert not store.has_cycle(conn, "test", "other")
    assert conn.execute("SELECT consumed_by_run FROM feedback_persona_approval WHERE id=?", (approval,)).fetchone()[0] == rid
    assert store.revoke_approvals(conn, "test", NOW) == 1


def test_lock_ownership_stale_sweep_and_run_history(conn):
    rid = insert_run(conn)
    lock = store.try_lock(conn, "test", now_iso=NOW, stale_minutes=60, host="host", pid=os.getpid())
    assert lock.acquired
    assert store.heartbeat(conn, "test", lock.token, NOW, task_run_id=rid)
    assert not store.heartbeat(conn, "test", "wrong", NOW)
    store.release_lock(conn, "test", "wrong")
    assert not store.try_lock(conn, "test", now_iso=NOW, stale_minutes=60, host="host", pid=os.getpid()).acquired
    new = store.try_lock(conn, "test", now_iso="2026-11-24T02:00:00Z", stale_minutes=60, host="host", pid=os.getpid())
    assert new.acquired and new.stolen_run_ids == (rid,)
    assert conn.execute("SELECT status,error FROM feedback_task_run WHERE id=?", (rid,)).fetchone()[:] == ("failed", "interrupted")
    store.release_lock(conn, "test", new.token)
    second = insert_run(conn, "second", run_id=None)
    assert store.sweep_interrupted(conn, current_run_id=12, now_iso=NOW) == 1
    assert store.prev_started_at(conn, "test") == NOW
    assert [r["id"] for r in store.recent_runs(conn, "test", 2)] == [second, rid]


def test_outbox_flush_marks_finalize_and_late_failure(conn):
    fake = FakeDynalist()
    fake.add_doc("doc")
    parent = fake.add("doc", "root", "parent")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None, changes_per_minute=1e9)
    conn.execute("INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-24',?,'running')", (NOW,))
    run_log_id = conn.execute("SELECT id FROM run_log").fetchone()[0]
    rid = insert_run(conn, run_id=run_log_id)
    unit = UnitSpec("agg", (), (NodeRef("doc", parent),))
    args = dict(task=task(), task_run_id=rid, run_id=run_log_id, unit=unit,
                write_to=unit.write_to[0], out={"summary": "#goal !(date)", "feedback": ["ok"], "evidence": []},
                local_date="2026-11-24", bot_tag="tutorbot", truncated=False, over_target=False,
                snapshot=TargetSnapshot("hash", 100))
    oid = output.enqueue_result(conn, **args)
    assert output.enqueue_result(conn, **args) == oid
    assert store.load_bot_write_marks(conn, ["doc"])[("doc", parent)].effective_modified_ms == 100
    report = flush_outbox(conn, client, sleep=lambda _: None)
    assert report.done == 3
    assert store.sync_output_status(conn, now_iso=NOW).written == 1
    assert store.finalize_runs(conn, run_log_id, now_iso=NOW)["succeeded"] == 1
    row = conn.execute("SELECT * FROM feedback_output WHERE id=?", (oid,)).fetchone()
    assert row["written_at"] == NOW and row["result_node_id"]
    assert "＃goal ！(date)" in row["body_json"]
    assert len(fake.children("doc", parent)) == 1
    assert conn.execute("SELECT COUNT(*) FROM bot_node").fetchone()[0] == 3
    # A retryable output is allowed to finalize, then fail permanently later.
    unit2 = UnitSpec("agg2", (), (NodeRef("doc", parent),))
    args.update(unit=unit2)
    oid2 = output.enqueue_result(conn, **args)
    key = conn.execute("SELECT root_op_key FROM feedback_output WHERE id=?", (oid2,)).fetchone()[0]
    conn.execute("UPDATE write_op SET status='failed',attempts=5 WHERE op_key=?", (key,))
    assert store.sync_output_status(conn, now_iso=NOW).late_failed_run_ids == (rid,)
    assert conn.execute("SELECT error FROM feedback_task_run WHERE id=?", (rid,)).fetchone()[0] == "write_failed_later"


def test_outbox_unit_transaction_rolls_back(conn):
    rid = insert_run(conn)
    unit = UnitSpec("agg", (), (NodeRef("doc", "one"),))
    with pytest.raises(RuntimeError):
        with store._transaction(conn):
            output.enqueue_result(conn, task=task(), task_run_id=rid, run_id=None, unit=unit,
                                  write_to=unit.write_to[0], out={"summary": "ok", "feedback": [], "evidence": []},
                                  local_date="2026-11-24", bot_tag="tutorbot", truncated=False,
                                  over_target=False, snapshot=TargetSnapshot("h", 1))
            raise RuntimeError("simulate unit failure")
    assert conn.execute("SELECT COUNT(*) FROM feedback_output").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM write_op").fetchone()[0] == 0


def test_readonly_and_invalid_checks(tmp_path, conn):
    assert store.open_readonly(tmp_path / "missing") is None
    path = tmp_path / "test.sqlite"
    other = db.connect(path)
    db.migrate(other)
    other.close()
    readonly = store.open_readonly(path)
    assert store.has_feedback_tables(readonly)
    with pytest.raises(sqlite3.OperationalError):
        readonly.execute("DELETE FROM feedback_task_run")
    readonly.close()
    with pytest.raises(sqlite3.IntegrityError):
        insert_run(conn, trigger_kind="invalid")


def test_two_connections_compete_for_cycle(tmp_path):
    path = tmp_path / "shared.sqlite"
    first, second = db.connect(path), db.connect(path)
    db.migrate(first)
    assert insert_run(first, status="skipped", skip_reason="empty_selection") is not None
    assert insert_run(second, status="skipped", skip_reason="empty_selection") is None
    assert second.execute("SELECT COUNT(*) FROM feedback_task_run").fetchone()[0] == 1
    first.close()
    second.close()


def test_sync_prefix_is_literal_and_marks_are_latest_eligible(conn):
    rid = insert_run(conn)

    def add(key, status, node, own_hash):
        return store.insert_output(conn, task_run_id=rid, task_id="test", unit_ref=key,
                                   target_file_id="doc", target_node_id=node, root_op_key=key,
                                   status=status, body_json="{}", created_at=NOW,
                                   target_own_hash=own_hash, target_effective_modified_ms=10)

    first = add("a_b", "pending_write", "parent", "first")
    add("aXb", "pending_write", "other", "other")
    add("latest", "written", "parent", "latest")
    add("bad", "failed", "parent", "ignored")
    add("root", "written", "root", None)
    from tutorbot.dynalist.writer import enqueue_op
    for key in ("a_b", "aXb"):
        enqueue_op(conn, op_key=key, file_id="doc", action="insert", parent_ref="node:root")
    conn.execute("UPDATE write_op SET status='done',result_node_id='ok' WHERE op_key='a_b'")
    conn.execute("UPDATE write_op SET status='skipped' WHERE op_key='aXb'")
    marks = store.load_bot_write_marks(conn, ["doc"])
    assert marks[("doc", "parent")].own_hash == "latest"
    assert ("doc", "root") not in marks
    assert store.load_bot_write_marks(conn, ["other"]) == {}
    result = store.sync_output_status(conn, now_iso=NOW)
    assert (result.written, result.failed) == (1, 1)
    assert conn.execute("SELECT status FROM feedback_output WHERE id=?", (first,)).fetchone()[0] == "written"


def test_outbox_reconcile_and_missing_parent(conn):
    fake = FakeDynalist()
    fake.add_doc("doc")
    parent = fake.add("doc", "root", "parent")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None, changes_per_minute=1e9)
    rid = insert_run(conn)
    ref = NodeRef("doc", parent)
    unit = UnitSpec("agg", (), (ref,))
    kwargs = dict(task=task(), task_run_id=rid, run_id=None, unit=unit,
                  write_to=ref, out={"summary": "ok", "feedback": [], "evidence": []},
                  local_date="2026-11-24", bot_tag="tutorbot", truncated=False,
                  over_target=False, snapshot=TargetSnapshot("h", 1))
    oid = output.enqueue_result(conn, **kwargs)
    fake.crash_after_apply()
    flush_outbox(conn, client, sleep=lambda _: None)
    assert len(fake.children("doc", parent)) == 1
    result = flush_outbox(conn, client, sleep=lambda _: None)
    assert result.reconciled == 1
    assert len(fake.children("doc", parent)) == 1
    assert store.sync_output_status(conn, now_iso=NOW).written == 1
    assert conn.execute("SELECT status FROM feedback_output WHERE id=?", (oid,)).fetchone()[0] == "written"
    missing = NodeRef("doc", "deleted")
    unit2 = UnitSpec("missing", (), (missing,))
    kwargs.update(unit=unit2, write_to=missing)
    missing_id = output.enqueue_result(conn, **kwargs)
    flush_outbox(conn, client, sleep=lambda _: None)
    assert store.sync_output_status(conn, now_iso=NOW).failed == 1
    assert conn.execute("SELECT error FROM feedback_output WHERE id=?", (missing_id,)).fetchone()[0] == "write_failed"


def test_dead_pid_lock_takeover_and_sweep_lock_without_run_id(conn, monkeypatch):
    rid = insert_run(conn)
    lock = store.try_lock(conn, "test", now_iso=NOW, stale_minutes=60, host="host", pid=12345)
    assert store.heartbeat(conn, "test", lock.token, NOW, task_run_id=rid)
    def dead(_pid, _signal):
        raise ProcessLookupError
    monkeypatch.setattr(os, "kill", dead)
    acquired = store.try_lock(conn, "test", now_iso=NOW, stale_minutes=60, host="host", pid=12346)
    assert acquired.acquired and acquired.stolen_run_ids == (rid,)
    insert_run(conn, "next")
    assert store.sweep_interrupted(conn, current_run_id=9, now_iso=NOW) == 1
    assert conn.execute("SELECT COUNT(*) FROM feedback_task_lock").fetchone()[0] == 0
