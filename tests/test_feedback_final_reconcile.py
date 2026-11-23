import pytest

from tests.fakes import FakeDynalist
from tests.test_feedback_foundations import task
from tests.test_feedback_store_focused import NOW, insert_run
from tutorbot import db
from tutorbot.dynalist.client import DynalistClient, UnknownOutcome
from tutorbot.dynalist.writer import flush_outbox
from tutorbot.feedback import output, store
from tutorbot.feedback.models import NodeRef, UnitSpec


@pytest.fixture
def setup():
    conn = db.connect(":memory:")
    db.migrate(conn)
    fake = FakeDynalist()
    fake.add_doc("doc")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None, changes_per_minute=1e9)
    ref = NodeRef("doc", "root")
    output.enqueue_result(conn, task=task(), task_run_id=insert_run(conn), run_id=None,
                          unit=UnitSpec("agg", (), (ref,)), write_to=ref,
                          out={"summary": "ok", "feedback": ["line"], "evidence": []},
                          local_date="2026-11-24", bot_tag="tutorbot", truncated=False,
                          over_target=False, snapshot=None)
    conn.execute("UPDATE write_op SET status='failed',attempts=4 WHERE bot_kind='feedback_rule'")
    yield conn, fake, client
    conn.close()


def test_fifth_response_lost_reconciles_parent_before_children(setup):
    conn, fake, client = setup
    fake.crash_after_apply()
    flush_outbox(conn, client)
    assert store.sync_output_status(conn, now_iso=NOW).pending == 1
    assert conn.execute("SELECT COUNT(*) FROM write_op WHERE status='skipped'").fetchone()[0] == 0
    flush_outbox(conn, client)
    assert store.sync_output_status(conn, now_iso=NOW).written == 1
    headers = fake.children("doc", "root")
    assert len(headers) == 1
    assert len(fake.children("doc", headers[0]["id"])) == 2
    assert conn.execute("SELECT attempts FROM write_op WHERE bot_kind='feedback_rule'").fetchone()[0] == 5


def test_final_reconciliation_read_failure_remains_pending(setup):
    conn, fake, client = setup
    fake.crash_after_apply()
    flush_outbox(conn, client)
    read = client.doc_read
    client.doc_read = lambda _: (_ for _ in ()).throw(RuntimeError("read unavailable"))
    flush_outbox(conn, client)
    assert store.sync_output_status(conn, now_iso=NOW).pending == 1
    assert conn.execute("SELECT COUNT(*) FROM write_op WHERE status='skipped'").fetchone()[0] == 0
    client.doc_read = read
    flush_outbox(conn, client)
    assert store.sync_output_status(conn, now_iso=NOW).written == 1
    assert len(fake.children("doc", "root")) == 1


def test_final_response_lost_without_apply_becomes_failed_after_read(setup):
    conn, fake, client = setup
    client.doc_edit = lambda *_: (_ for _ in ()).throw(UnknownOutcome("Timeout", "lost"))
    flush_outbox(conn, client)
    assert store.sync_output_status(conn, now_iso=NOW).pending == 1
    flush_outbox(conn, client)
    assert store.sync_output_status(conn, now_iso=NOW).failed == 1
    assert not fake.children("doc", "root")
    assert conn.execute("SELECT COUNT(*) FROM write_op WHERE status='skipped'").fetchone()[0] == 2


def test_fifth_response_lost_for_child_also_reconciles(setup):
    conn, fake, client = setup
    edit = client.doc_edit

    def lose_child_response(file_id, changes):
        if any(change.get("content", "").startswith("📝") for change in changes):
            fake.crash_after_apply()
        return edit(file_id, changes)

    client.doc_edit = lose_child_response
    conn.execute("UPDATE write_op SET attempts=4 WHERE bot_kind='feedback_rule_item'")
    flush_outbox(conn, client)
    assert store.sync_output_status(conn, now_iso=NOW).pending == 1
    client.doc_edit = edit
    flush_outbox(conn, client)
    assert store.sync_output_status(conn, now_iso=NOW).written == 1
    headers = fake.children("doc", "root")
    assert len(headers) == 1
    assert len(fake.children("doc", headers[0]["id"])) == 2
