from types import SimpleNamespace

from tests.fakes import FakeDynalist
from tutorbot import db, ingest
from tutorbot.dynalist.tree import NodeIndex
from tutorbot.parse.classify import DEFAULT_TAGS

F = "d"
S = SimpleNamespace(tags=DEFAULT_TAGS, timezone="Asia/Seoul",
                    processing=SimpleNamespace(min_entry_chars=20))


def run(conn, fk):
    return ingest.ingest_index(conn, NodeIndex.from_doc_read(F, fk._doc_read({"file_id": F})), S)


def setup():
    fk = FakeDynalist(); fk.add_doc(F)
    conn = db.connect(":memory:"); db.migrate(conn)
    return fk, conn


def test_single_goal_links_untagged_entries_and_is_idempotent():
    fk, conn = setup()
    fk.add(F, "root", "#목표 #rust Rust 백엔드 개발")
    d = fk.add(F, "root", "2026-03-01")
    e = fk.add(F, d, "#공부 소유권과 빌림에 대해서 오늘 공부했다")
    fk.add(F, "root", "#공부 짧음")
    r = run(conn, fk)
    assert (r.goals_new, r.entries_new) == (1, 2)
    rows = {x["node_id"]: x for x in conn.execute("SELECT * FROM study_entry")}
    assert rows[e]["entry_date"] == "2026-03-01" and rows[e]["date_source"] == "ancestor"
    assert rows[e]["status"] == "pending" and rows[e]["goal_id"] == 1
    assert sorted(x["status"] for x in rows.values()) == ["pending", "too_short"]
    r2 = run(conn, fk)
    assert (r2.goals_new, r2.entries_new, r2.entries_changed) == (0, 0, 0)


def test_two_goals_require_topic_tag_then_resolve():
    fk, conn = setup()
    fk.add(F, "root", "#목표 #rust Rust")
    fk.add(F, "root", "#목표 #ml ML")
    e = fk.add(F, "root", "#공부 충분히 긴 공부 내용입니다 여러 줄 아님")
    r = run(conn, fk)
    assert conn.execute("SELECT status FROM study_entry").fetchone()[0] == "skipped_untagged"
    assert r.warnings
    fk.docs[F][e]["content"] = "#공부 #ml 충분히 긴 공부 내용입니다 여러 줄 아님"
    r = run(conn, fk)
    row = conn.execute("SELECT status, goal_id FROM study_entry").fetchone()
    assert row["status"] == "pending" and row["goal_id"] == 2 and r.entries_changed == 1


def test_modified_entry_resets_and_bot_child_ignored_and_deleted():
    fk, conn = setup()
    fk.add(F, "root", "#목표 #rust Rust")
    e = fk.add(F, "root", "#공부 충분히 긴 공부 내용입니다 아아아아")
    run(conn, fk)
    conn.execute("UPDATE study_entry SET processed_hash=content_hash, status='processed', retry_count=2")
    fk.add(F, e, "🤖 피드백 #tutorbot", "tb:feedback:1")
    assert run(conn, fk).entries_changed == 0
    fk.add(F, e, "추가 내용")
    assert run(conn, fk).entries_changed == 1
    row = conn.execute("SELECT * FROM study_entry").fetchone()
    assert row["status"] == "pending" and row["retry_count"] == 0
    assert row["content_hash"] != row["processed_hash"]
    fk.docs[F]["root"]["children"].remove(e)
    assert run(conn, fk).entries_deleted == 1


def test_goal_without_topic_warns():
    fk, conn = setup()
    fk.add(F, "root", "#목표 그냥 목표")
    r = run(conn, fk)
    assert r.warnings and conn.execute("SELECT count(*) FROM goal").fetchone()[0] == 0
