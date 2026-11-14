"""M2 tests: Outbox writer + reconcile, render.py, T1 curriculum, progress update,
idempotency (2 consecutive runs -> 0 changes on 2nd run), and fault injection (a)(b)(c) (spec §12, §14).
"""
from __future__ import annotations

from types import SimpleNamespace

from tests.fakes import FakeDynalist, FakeLLM
from tutorbot import db, render
from tutorbot.dynalist.client import DynalistClient
from tutorbot.dynalist.tree import NodeIndex
from tutorbot.dynalist.writer import enqueue_op, flush_outbox
from tutorbot.llm.client import LLMClient
from tutorbot.parse.classify import DEFAULT_TAGS
from tutorbot.pipeline import run_pipeline

F = "doc1"


def make_settings(tmp_path):
    return SimpleNamespace(
        timezone="Asia/Seoul",
        db_path=str(tmp_path / "tb.sqlite3"),
        dynalist=SimpleNamespace(file_ids=[F]),
        tags=DEFAULT_TAGS,
        processing=SimpleNamespace(min_entry_chars=20),
    )


def sample_t1():
    return {
        "goal": {"title": "Rust 백엔드 마스터", "level": "실무", "deadline": "2026-12-31"},
        "concepts": [
            {"key": "c1", "parent_key": None, "name": "메모리 관리", "description": "소유권과 빌림", "prereq_keys": [], "order": 1},
            {"key": "c2", "parent_key": "c1", "name": "소유권", "description": "Ownership 규칙", "prereq_keys": [], "order": 1},
            {"key": "c3", "parent_key": "c1", "name": "빌림과 참조", "description": "Borrowing", "prereq_keys": ["c2"], "order": 2},
            {"key": "c4", "parent_key": None, "name": "비동기 프로그래밍", "description": "Future와 Tokio", "prereq_keys": [], "order": 2},
            {"key": "c5", "parent_key": "c4", "name": "Future 트레이트", "description": "비동기 기본", "prereq_keys": ["c3"], "order": 1},
        ],
    }


def setup_env(tmp_path):
    fk = FakeDynalist()
    fk.add_doc(F)
    conn = db.connect(":memory:")
    db.migrate(conn)
    fllm = FakeLLM()
    fllm.set("t1_curriculum", sample_t1())
    client = DynalistClient("tok", transport=fk, sleep=lambda _: None, changes_per_minute=1e9)
    llm = LLMClient(backend=fllm, conn=conn, sleep=lambda _: None)
    settings = make_settings(tmp_path)
    return fk, conn, fllm, client, llm, settings


# ---------------- render.py ----------------
def test_render_formats_and_feedback_budget():
    assert render.curriculum_root(42) == "🤖 커리큘럼 (진도 42%) #tutorbot"
    assert render.concept_leaf("소유권", "unknown") == "[⬜] 소유권"
    assert render.concept_leaf("소유권", "mastered") == "[⭐] 소유권"
    assert render.concept_branch("메모리 관리", 3, 7) == "📘 메모리 관리 (3/7)"
    lines = render.feedback_lines(
        corrections=["교정1", "교정2"],
        missing=["누락1", "누락2"],
        next_steps=["다음1"],
        mapped_concepts=["소유권", "빌림"],
    )
    # 2 corrections + 1 next + 1 missing (since budget is 4 before mapping) + 1 mapping = 5 lines max
    assert len(lines) == 5
    assert any(l.startswith("🧩 매핑:") for l in lines)
    assert sum(1 for l in lines if l.startswith("🔑 빠진 핵심:")) == 1


# ---------------- M2 pipeline & idempotency ----------------
def test_t1_curriculum_and_idempotency_second_run_zero_changes(tmp_path):
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path)
    goal_nid = fk.add(F, "root", "#목표 #rust Rust 백엔드 마스터")

    # Run 1: generates curriculum + progress report
    res1 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res1.status == "ok"
    assert res1.stats["goals"]["generated"] == 1
    changes_after_run1 = len(fk.applied_changes)
    assert changes_after_run1 > 0

    # Verify tree structure in FakeDynalist under goal_nid
    goal_children = fk.children(F, goal_nid)
    assert len(goal_children) == 2  # index 0: curriculum_root, index 1: progress_report
    curr_root = goal_children[0]
    prog_rep = goal_children[1]
    assert curr_root["content"] == "🤖 커리큘럼 (진도 0%) #tutorbot"
    assert curr_root["note"].startswith("tb:curriculum_root:1")
    assert prog_rep["content"] == "🤖 진도 리포트 (2026-11-12) #tutorbot"

    branches = fk.children(F, curr_root["id"])
    assert [b["content"] for b in branches] == ["📘 메모리 관리 (0/2)", "📘 비동기 프로그래밍 (0/1)"]
    leaves_c1 = fk.children(F, branches[0]["id"])
    assert [l["content"] for l in leaves_c1] == ["[⬜] 소유권", "[⬜] 빌림과 참조"]

    # Run 2 on the same day: MUST produce 0 Dynalist changes
    res2 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res2.status == "ok"
    assert len(fk.applied_changes) == changes_after_run1
    assert res2.stats["write"]["changes_sent"] == 0


def test_progress_update_edits_only_changed_concepts_and_root(tmp_path):
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path)
    goal_nid = fk.add(F, "root", "#목표 #rust Rust 백엔드")
    run_pipeline(conn, settings, client, llm, run_date="2026-11-12")

    # Transition '소유권' -> understood (1.0) and '빌림과 참조' -> partial (0.5)
    # 3 leaves total: (1.0 + 0.5 + 0) / 3 * 100 = 50%
    conn.execute("UPDATE concept SET state='understood' WHERE name='소유권'")
    conn.execute("UPDATE concept SET state='partial' WHERE name='빌림과 참조'")

    before = len(fk.applied_changes)
    res = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res.status == "ok"
    new_changes = [ch for _, ch in fk.applied_changes[before:]]
    # Only: 2 leaves + 1 branch ('메모리 관리 (1/2)') + 1 curriculum_root ('진도 50%') = 4 edits
    assert all(ch["action"] == "edit" for ch in new_changes)
    edited_contents = {ch["content"] for ch in new_changes}
    assert edited_contents == {
        "[✅] 소유권",
        "[🟡] 빌림과 참조",
        "📘 메모리 관리 (1/2)",
        "🤖 커리큘럼 (진도 50%) #tutorbot",
    }
    assert goal_nid in fk.docs[F]


def test_d5_user_deletes_concept_or_curriculum_root(tmp_path):
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path)
    goal_nid = fk.add(F, "root", "#목표 #rust Rust 백엔드")
    run_pipeline(conn, settings, client, llm, run_date="2026-11-12")

    # User deletes leaf 'Future 트레이트'
    c5 = conn.execute("SELECT id, node_id FROM concept WHERE name='Future 트레이트'").fetchone()
    idx = NodeIndex.from_doc_read(F, fk._doc_read({"file_id": F}))
    parent_id = idx.parent_of[c5["node_id"]]
    fk.docs[F][parent_id]["children"].remove(c5["node_id"])
    fk._delete_subtree(fk.docs[F], c5["node_id"])

    res = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res.status == "ok"
    assert conn.execute("SELECT user_deleted FROM concept WHERE id=?", (c5["id"],)).fetchone()[0] == 1

    # Now user deletes the entire curriculum_root -> rebuilt without user_deleted concept
    curr_root_nid = conn.execute(
        "SELECT node_id FROM bot_node WHERE kind='curriculum_root' AND deleted_at IS NULL"
    ).fetchone()[0]
    fk.docs[F][goal_nid]["children"].remove(curr_root_nid)
    fk._delete_subtree(fk.docs[F], curr_root_nid)

    res2 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res2.status == "ok"
    assert res2.stats["goals"]["rebuilt"] == 1
    new_root_nid = conn.execute(
        "SELECT node_id FROM bot_node WHERE kind='curriculum_root' AND deleted_at IS NULL"
    ).fetchone()[0]
    new_idx = NodeIndex.from_doc_read(F, fk._doc_read({"file_id": F}))
    curr_contents = [n.content for n, _ in new_idx.iter_subtree(new_root_nid)]
    assert "[⬜] 소유권" in curr_contents
    assert not any("Future 트레이트" in c for c in curr_contents)


# ---------------- Fault Injection (a)(b)(c) ----------------
def test_fault_a_crash_after_insert_reconciles_with_zero_duplicates(tmp_path):
    """(a) insert succeeds on server, process/request dies before response -> rerun creates 0 duplicate nodes."""
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path)
    goal_nid = fk.add(F, "root", "#목표 #rust Rust 백엔드")

    # Inject crash after the first doc/edit wave (curriculum_root + progress_report inserts)
    fk.crash_after_apply("doc/edit")
    res1 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res1.status == "partial"

    # Both top-level nodes were already created in FakeDynalist during wave 1 before UnknownOutcome
    assert len(fk.children(F, goal_nid)) == 2

    # Re-run pipeline: pre_flush/write reconciles the existing nodes and finishes the remaining waves
    res2 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res2.status == "ok"

    # Verify zero duplicate curriculum_root or progress_report or concept nodes
    goal_kids = fk.children(F, goal_nid)
    assert len(goal_kids) == 2
    curr_roots = [k for k in goal_kids if "커리큘럼" in k["content"]]
    prog_reps = [k for k in goal_kids if "진도 리포트" in k["content"]]
    assert len(curr_roots) == 1 and len(prog_reps) == 1

    # Also test reconcile for markerless child ops: crash on wave 2 (depth-1 concepts)
    fk2, conn2, fllm2, client2, llm2, settings2 = setup_env(tmp_path)
    g2 = fk2.add(F, "root", "#목표 #rust Rust 백엔드")
    enqueue_op(
        conn2,
        op_key="test_child:1",
        file_id=F,
        action="insert",
        parent_ref=f"node:{g2}",
        idx=-1,
        payload={"content": "[⬜] 단독개념"},
        bot_kind="concept",
        bot_ref_id=None,
        marker=None,
    )
    fk2.crash_after_apply("doc/edit")
    rep_crash = flush_outbox(conn2, client2)
    assert len(fk2.children(F, g2)) == 1
    rep_recover = flush_outbox(conn2, client2)
    assert rep_recover.reconciled == 1
    assert len(fk2.children(F, g2)) == 1


def test_fault_b_too_many_requests_twice_then_succeeds(tmp_path):
    """(b) TooManyRequests 2 times then succeeds."""
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path)
    fk.add(F, "root", "#목표 #rust Rust 백엔드")
    fk.fail("doc/edit", "TooManyRequests", times=2)

    res = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM write_op WHERE status!='done'").fetchone()[0] == 0


def test_fault_c_parent_deleted_before_write_marks_op_skipped_and_run_partial(tmp_path):
    """(c) Parent node deleted before write -> op and its dependents marked skipped, batch status is partial."""
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path)
    study_nid = fk.add(F, "root", "#공부 삭제될 공부 노드")

    # Enqueue a parent insert under study_nid and a child insert depending on that op
    enqueue_op(
        conn,
        op_key="fb:999",
        file_id=F,
        action="insert",
        parent_ref=f"node:{study_nid}",
        idx=-1,
        payload={"content": "🤖 피드백 #tutorbot"},
        bot_kind="feedback",
        bot_ref_id=999,
        marker="tb:feedback:999",
    )
    enqueue_op(
        conn,
        op_key="fb_line:999:1",
        file_id=F,
        action="insert",
        parent_ref="op:fb:999",
        idx=-1,
        payload={"content": "✏️ 교정: 내용"},
        bot_kind="feedback_item",
        bot_ref_id=999,
    )

    # Delete study_nid from FakeDynalist before running pipeline
    fk.docs[F]["root"]["children"].remove(study_nid)
    fk._delete_subtree(fk.docs[F], study_nid)

    res = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res.status == "partial"
    statuses = dict(conn.execute("SELECT op_key, status FROM write_op").fetchall())
    assert statuses["fb:999"] == "skipped"
    assert statuses["fb_line:999:1"] == "skipped"


def test_llm_invalid_json_repairs_once_then_fails_unit(tmp_path):
    """Spec §12 LLM test: invalid JSON -> 1 repair request -> if still invalid, unit error while others succeed."""
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path)
    fk.add(F, "root", "#목표 #rust Rust 백엔드")

    # First test: bad JSON once, then valid JSON on repair -> succeeds!
    fllm.set("t1_curriculum", "NOT VALID JSON {{{", sample_t1())
    res1 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res1.status == "ok"
    assert len(fllm.calls) == 2

    # Second test: another goal with persistent bad JSON -> unit status='error', retry_count=1
    fk.add(F, "root", "#목표 #ml 머신러닝 마스터")
    fllm.set("t1_curriculum", "BAD1", "BAD2")
    res2 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res2.status == "partial"
    ml_goal = conn.execute("SELECT status, retry_count FROM goal WHERE topic_tag='ml'").fetchone()
    assert ml_goal["status"] == "error" and ml_goal["retry_count"] == 1
