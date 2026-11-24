import sqlite3

import pytest

from tests.fakes import FakeDynalist
from tests.test_feedback_foundations import task
from tests.test_feedback_store_focused import NOW, insert_run
from tutorbot import db
from tutorbot.dynalist.client import DynalistClient, RetryableError
from tutorbot.dynalist.writer import enqueue_op, flush_outbox
from tutorbot.feedback import output, store
from tutorbot.feedback.models import NodeRef, UnitSpec


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


def enqueue(conn, rid, file_id="doc"):
    ref = NodeRef(file_id, "root")
    return output.enqueue_result(
        conn, task=task(), task_run_id=rid, run_id=None,
        unit=UnitSpec("agg", (), (ref,)), write_to=ref,
        out={"summary": "ok", "feedback": ["line"], "evidence": []},
        local_date="2026-11-24", bot_tag="tutorbot", truncated=False,
        over_target=False, snapshot=None,
    )


@pytest.mark.parametrize("failures,expected", [(1, "written"), (5, "failed")])
def test_parent_retry_or_exhaustion(conn, failures, expected):
    fake = FakeDynalist()
    fake.add_doc("doc")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None, changes_per_minute=1e9)
    original = client.doc_edit
    calls = []

    def fail_then_write(file_id, changes):
        calls.append(1)
        if len(calls) <= failures:
            raise RetryableError("injected", "retry")
        return original(file_id, changes)

    client.doc_edit = fail_then_write
    enqueue(conn, insert_run(conn))
    flush_outbox(conn, client, sleep=lambda _: None)
    states = conn.execute("SELECT status,attempts FROM write_op ORDER BY seq").fetchall()
    assert states[0][:] == (("done", failures + 1) if failures < 5 else ("failed", 5))
    assert all(row[0] == ("done" if failures < 5 else "skipped") for row in states[1:])
    store.sync_output_status(conn, now_iso=NOW)
    assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == expected


def test_unknown_outcome_remains_pending_then_reconciles_children(conn):
    fake = FakeDynalist()
    fake.add_doc("doc")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None, changes_per_minute=1e9)
    enqueue(conn, insert_run(conn))
    fake.crash_after_apply()
    flush_outbox(conn, client, sleep=lambda _: None)
    assert store.sync_output_status(conn, now_iso=NOW).pending == 1
    assert conn.execute("SELECT COUNT(*) FROM write_op WHERE status='skipped'").fetchone()[0] == 0
    flush_outbox(conn, client, sleep=lambda _: None)
    assert store.sync_output_status(conn, now_iso=NOW).written == 1
    assert len(fake.children("doc", "root")) == 1
    assert len(fake.children("doc", fake.children("doc", "root")[0]["id"])) == 2


def test_previously_failed_parent_waits_and_legacy_dependency_behavior_is_unchanged(conn):
    fake = FakeDynalist()
    fake.add_doc("doc")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None, changes_per_minute=1e9)
    enqueue(conn, insert_run(conn))
    conn.execute("UPDATE write_op SET status='failed',attempts=1 WHERE bot_kind='feedback_rule'")
    enqueue_op(conn, op_key="old-parent", file_id="doc", action="insert", parent_ref="node:root")
    enqueue_op(conn, op_key="old-child", file_id="doc", action="insert", parent_ref="op:old-parent")
    conn.execute("UPDATE write_op SET status='failed',attempts=1 WHERE op_key='old-parent'")
    assert store.sync_output_status(conn, now_iso=NOW).pending == 1
    flush_outbox(conn, client, sleep=lambda _: None)
    assert store.sync_output_status(conn, now_iso=NOW).written == 1
    assert conn.execute("SELECT status FROM write_op WHERE op_key='old-child'").fetchone()[0] == "skipped"


def test_same_node_id_in_different_documents(conn):
    fake = FakeDynalist()
    for file_id in ("one", "two"):
        fake.add_doc(file_id)
    client = DynalistClient("test", transport=fake, sleep=lambda _: None, changes_per_minute=1e9)
    rid = insert_run(conn)
    assert enqueue(conn, rid, "one") != enqueue(conn, rid, "two")
    roots = conn.execute("SELECT root_op_key FROM feedback_output ORDER BY id").fetchall()
    assert roots[0][0] != roots[1][0]
    markers = conn.execute("SELECT marker FROM write_op WHERE bot_kind='feedback_rule' ORDER BY seq").fetchall()
    assert markers[0][0] != markers[1][0]
    fake.crash_after_apply()
    flush_outbox(conn, client, sleep=lambda _: None)
    flush_outbox(conn, client, sleep=lambda _: None)
    assert store.sync_output_status(conn, now_iso=NOW).written == 2
    for file_id in ("one", "two"):
        headers = fake.children(file_id, "root")
        assert len(headers) == 1
        assert len(fake.children(file_id, headers[0]["id"])) == 2


def test_pending_ops_query_uses_key_index(conn):
    prefix = "key:line:"
    plan = conn.execute("EXPLAIN QUERY PLAN " + store._OUTPUT_OPS_SQL,
                        ("key", prefix, "key:line;", len(prefix), prefix)).fetchall()
    details = [row[3] for row in plan]
    assert sum("SEARCH write_op USING INDEX" in detail for detail in details) == 2
    assert not any("SCAN write_op" in detail for detail in details)


def v2_connection(monkeypatch):
    conn = db.connect(":memory:")
    migrations = db._migrations
    monkeypatch.setattr(db, "_migrations", lambda: [m for m in migrations() if m[0] <= 2])
    assert db.migrate(conn) == 2
    monkeypatch.setattr(db, "_migrations", migrations)
    return conn


def test_migration_version_failure_rolls_back_and_retries(monkeypatch):
    conn = v2_connection(monkeypatch)
    conn.execute("CREATE TRIGGER reject_version BEFORE UPDATE ON meta "
                 "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.migrate(conn)
    assert not conn.in_transaction
    assert db._current_version(conn) == 2
    assert not store.has_feedback_tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'feedback_%'").fetchone()[0] == 0
    conn.execute("DROP TRIGGER reject_version")
    assert db.migrate(conn) == 4
    assert store.has_feedback_tables(conn)
    conn.close()


def test_migration_recovers_existing_003_objects_without_losing_data(monkeypatch):
    conn = v2_connection(monkeypatch)
    script = (db.HERE / "migrations/003_feedback_rules.sql").read_text(encoding="utf-8")
    # Old 003 was committed before the version bump, without IF NOT EXISTS.
    conn.executescript(script.replace(" IF NOT EXISTS", ""))
    rid = insert_run(conn)
    conn.execute("DROP INDEX ix_feedback_output_target")
    conn.execute("DROP TABLE feedback_persona_approval")
    assert db.migrate(conn) == 4
    assert conn.execute("SELECT task_id FROM feedback_task_run WHERE id=?", (rid,)).fetchone()[0] == "test"
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='ix_feedback_output_target'").fetchone()
    assert store.has_feedback_tables(conn)
    conn.close()


def test_v1_ddl_failure_rolls_back(monkeypatch, tmp_path):
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE partial (id INTEGER);"
        "INSERT INTO missing_table VALUES (1);", encoding="utf-8"
    )
    monkeypatch.setattr(db, "HERE", tmp_path)
    conn = db.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn)
    assert not conn.in_transaction
    assert db._current_version(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='partial'").fetchone()[0] == 0
    conn.close()


def test_v2_version_failure_rolls_back_ddl_and_retries(monkeypatch):
    conn = db.connect(":memory:")
    migrations = db._migrations
    monkeypatch.setattr(db, "_migrations", list)
    assert db.migrate(conn) == 1
    conn.execute("CREATE TRIGGER reject_version BEFORE UPDATE ON meta "
                 "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    monkeypatch.setattr(db, "_migrations", lambda: [m for m in migrations() if m[0] == 2])
    with pytest.raises(sqlite3.IntegrityError):
        db.migrate(conn)
    assert db._current_version(conn) == 1
    assert not conn.in_transaction
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='domain_state'").fetchone() is None
    conn.execute("DROP TRIGGER reject_version")
    assert db.migrate(conn) == 2
    conn.close()


def test_migration_rejects_incompatible_existing_table_without_changes(monkeypatch):
    conn = v2_connection(monkeypatch)
    conn.execute("CREATE TABLE feedback_output (id INTEGER PRIMARY KEY, extra TEXT)")
    conn.execute("INSERT INTO feedback_output VALUES (1,'keep')")
    with pytest.raises(sqlite3.DatabaseError, match="incompatible|no such column"):
        db.migrate(conn)
    assert db._current_version(conn) == 2
    assert conn.execute("SELECT extra FROM feedback_output").fetchone()[0] == "keep"
    assert not conn.in_transaction
    conn.close()
