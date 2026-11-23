import builtins
import hashlib
import json
from dataclasses import replace

import pytest

from tests.fakes import FakeDynalist
from tests.test_feedback_foundations import task
from tests.test_feedback_store_focused import NOW, insert_run
from tutorbot import db
from tutorbot.config import load_settings
from tutorbot.dynalist.client import DynalistClient, RetryableError
from tutorbot.dynalist.tree import Node
from tutorbot.dynalist.writer import enqueue_op, flush_outbox
from tutorbot.feedback import output, store
from tutorbot.feedback.models import (
    BotWriteMark,
    ContextLine,
    NodeRef,
    OutputSpec,
    TargetBlock,
    TargetSnapshot,
    UnitSpec,
)
from tutorbot.feedback.sources import SelectedNode
from tutorbot.feedback.timefilter import effective_modified_ms, own_text_hash


@pytest.mark.parametrize("applied", [True, False])
def test_process_death_on_fifth_send_recovers_without_resending(tmp_path, applied):
    path = tmp_path / "outbox.db"
    conn = db.connect(path)
    db.migrate(conn)
    fake = FakeDynalist()
    fake.add_doc("doc")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None, changes_per_minute=1e9)
    dest = NodeRef("doc", "root")
    output.enqueue_result(conn, task=task(), task_run_id=insert_run(conn), run_id=None,
                          unit=UnitSpec("agg", (), (dest,)), write_to=dest,
                          out={"summary": "ok", "feedback": ["line"], "evidence": []},
                          local_date="2026-11-24", bot_tag="tutorbot", truncated=False,
                          over_target=False, snapshot=None)
    conn.execute("UPDATE write_op SET status='failed',attempts=4 WHERE bot_kind='feedback_rule'")
    edit = client.doc_edit

    def die(file_id, changes):
        state = conn.execute("SELECT status,attempts,last_error FROM write_op WHERE bot_kind='feedback_rule'").fetchone()
        assert tuple(state) == ("failed", 5, "unknown_outcome_reconcile")
        if applied:
            edit(file_id, changes)
        raise SystemExit("process terminated")

    client.doc_edit = die
    with pytest.raises(SystemExit):
        flush_outbox(conn, client)
    conn.close()
    conn = db.connect(path)
    client.doc_edit = edit
    flush_outbox(conn, client)
    result = store.sync_output_status(conn, now_iso=NOW)
    headers = fake.children("doc", "root")
    if applied:
        assert result.written == 1
        assert len(headers) == 1
        assert len(fake.children("doc", headers[0]["id"])) == 2
    else:
        assert result.failed == 1
        assert not headers
        assert conn.execute("SELECT COUNT(*) FROM write_op WHERE status='skipped'").fetchone()[0] == 2
    assert conn.execute("SELECT attempts FROM write_op WHERE bot_kind='feedback_rule'").fetchone()[0] == 5
    conn.close()


def test_fifth_definite_failure_and_legacy_behavior():
    conn = db.connect(":memory:")
    db.migrate(conn)
    fake = FakeDynalist()
    fake.add_doc("doc")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None, changes_per_minute=1e9)
    enqueue_op(conn, op_key="legacy", file_id="doc", action="insert", parent_ref="node:root",
               marker="legacy-marker", bot_kind="concept")
    conn.execute("UPDATE write_op SET attempts=4 WHERE op_key='legacy'")

    def fail(*_):
        row = conn.execute("SELECT attempts,last_error FROM write_op").fetchone()
        assert tuple(row) == (5, None)
        raise RetryableError("injected", "failed")
    client.doc_edit = fail
    flush_outbox(conn, client)
    assert tuple(conn.execute("SELECT status,attempts FROM write_op").fetchone()) == ("failed", 5)
    conn.close()


def test_selected_target_excerpt_uses_selection_not_destination_and_sanitizes():
    conn = db.connect(":memory:")
    db.migrate(conn)
    selected = SelectedNode(NodeRef("source", "n1"), 0, "self")
    dest = NodeRef("output", "n2")
    unit = UnitSpec("source.n1", (selected,), (dest,))
    spec = replace(task(), output=OutputSpec("under_parent", "output", "n2"))
    block = TargetBlock(selected, (ContextLine(selected.ref, 0, "#goal\n!(date) " + "x" * 50, None, 1),), 1)
    args = {"conn": conn, "task": spec, "task_run_id": insert_run(conn), "run_id": None,
            "unit": unit, "write_to": dest, "out": {"summary": "ok", "feedback": [], "evidence": []},
            "local_date": "2026-11-24", "bot_tag": "tutorbot", "truncated": False,
            "over_target": False, "snapshot": None}
    wrong = replace(block, selected=replace(selected, ref=dest))
    with pytest.raises(ValueError, match="does not match"):
        output.enqueue_result(**args, selected_target=wrong)
    output.enqueue_result(**args, selected_target=block)
    line = json.loads(conn.execute("SELECT payload_json FROM write_op WHERE op_key LIKE '%:line:0'").fetchone()[0])["content"]
    assert line == "🎯 대상: [source/n1] " + ("＃goal ！(date) " + "x" * 50)[:40]
    assert conn.execute("SELECT COUNT(*) FROM feedback_output").fetchone()[0] == 1
    conn.close()


def test_v2_hash_boundary_and_conservative_v1_migration():
    first = Node("n", content="a\nb", note="c", modified=3000)
    second = Node("n", content="a", note="b\nc", modified=3000)
    old = "v1:" + hashlib.sha256(b"a\nb\nc").hexdigest()
    assert own_text_hash(first).startswith("v2:")
    assert own_text_hash(first) != own_text_hash(second)
    assert effective_modified_ms(first, BotWriteMark(old, 1000)) == 3000
    assert effective_modified_ms(second, BotWriteMark(old, 1000)) == 3000
    new_mark = BotWriteMark(own_text_hash(first), 1000)
    assert effective_modified_ms(first, new_mark) == 1000
    assert effective_modified_ms(second, new_mark) == 3000
    assert own_text_hash(Node("n", content="e\u0301")) == own_text_hash(Node("n", content="é"))


def test_v1_persisted_mark_replaced_by_new_output():
    conn = db.connect(":memory:")
    db.migrate(conn)
    node = Node("n", content="a\nb", note="c", modified=3000)
    old = "v1:" + hashlib.sha256(b"a\nb\nc").hexdigest()
    rid = insert_run(conn)
    dest = NodeRef("doc", "n")
    args = {"conn": conn, "task": task(), "task_run_id": rid, "run_id": None,
            "unit": UnitSpec("agg", (), (dest,)), "write_to": dest,
            "out": {"summary": "ok", "feedback": [], "evidence": []},
            "local_date": "2026-11-24", "bot_tag": "tutorbot", "truncated": False,
            "over_target": False}
    output.enqueue_result(**args, snapshot=TargetSnapshot(old, 1000))
    mark = store.load_bot_write_marks(conn, ["doc"])[("doc", "n")]
    assert effective_modified_ms(node, mark) == 3000
    args["unit"] = UnitSpec("new", (), (dest,))
    output.enqueue_result(**args, snapshot=TargetSnapshot(own_text_hash(node), 3000))
    mark = store.load_bot_write_marks(conn, ["doc"])[("doc", "n")]
    node.modified = 4000
    assert mark.own_hash.startswith("v2:")
    assert effective_modified_ms(node, mark) == 3000
    conn.close()


def test_external_rules_and_fallback_unsupported_are_global(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    rules = tmp_path / "rules.toml"
    config.write_text('[feedback_rules]\ntasks = [{id = "bad"}]\n[llm]\nrequests_per_minute = 7\n')
    rules.write_text('[[feedback_rules.tasks]]\nid="ok"\npurpose="ok"\nunit="per_node"\n'
                     'schedule="daily"\n[[feedback_rules.tasks.selection]]\nfile_id="doc"\n'
                     'parent_node_id="root"\nscope="descendants"\ntime="fixed_context"\n'
                     '[feedback_rules.tasks.output]\n')
    monkeypatch.delenv("TUTORBOT_FEEDBACK_RULES_FILE", raising=False)
    original_import = builtins.__import__

    def no_toml(name, *args, **kwargs):
        if name in ("tomllib", "tomli"):
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_toml)
    settings = load_settings(config, dotenv_path=tmp_path / "none", rules_file=rules)
    assert [issue.code for issue in settings.feedback_rules.issues] == ["FR-G009"]
    assert not settings.feedback_rules.valid_tasks()
    assert settings.llm.requests_per_minute == 7
    config.write_text('[feedback_rules]\nenabled = true\n')
    rules.write_text('[feedback_rules]\ntasks = [{id = "bad"}]\n')
    settings = load_settings(config, dotenv_path=tmp_path / "none", rules_file=rules)
    assert [issue.code for issue in settings.feedback_rules.issues] == ["FR-G007"]
    assert not settings.feedback_rules.valid_tasks()
