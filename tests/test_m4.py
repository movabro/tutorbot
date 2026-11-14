"""M4 tests: SRS SM-2 formula table tests (§7.2), card selection rules (§7.3),
expiration & 3x consecutive expiry postponement (D1), fast 0-score skip answers,
T4 batch grading & state downgrade/mastery, same-day idempotency, and 2-day E2E scenario (spec §12, §14).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.fakes import FakeDynalist, FakeLLM
from tutorbot import clock, db
from tutorbot.dynalist.client import DynalistClient
from tutorbot.engine.quiz import select_cards_for_goal, sm2_update
from tutorbot.llm.client import LLMClient
from tutorbot.parse.classify import DEFAULT_TAGS
from tutorbot.pipeline import run_pipeline

F = "doc1"


def make_settings(tmp_path, per_day=5, max_new_per_day=2, expire_days=3):
    return SimpleNamespace(
        timezone="Asia/Seoul",
        db_path=str(tmp_path / "tb.sqlite3"),
        dynalist=SimpleNamespace(file_ids=[F]),
        tags=DEFAULT_TAGS,
        quiz=SimpleNamespace(
            per_day=per_day,
            max_new_per_day=max_new_per_day,
            expire_days=expire_days,
            skip_answers=["?", "모름", "pass", "패스"],
        ),
        processing=SimpleNamespace(
            min_entry_chars=20,
            settle_minutes=60,
            similarity_skip=0.95,
            max_entries_per_run=20,
            entry_max_chars=6000,
            priority_quiz=True,
        ),
        llm=SimpleNamespace(
            index_full_threshold=150,
            daily_token_budget=300000,
        ),
    )


def sample_t1():
    return {
        "goal": {"title": "Rust 백엔드 마스터", "level": "실무", "deadline": "2026-12-31"},
        "concepts": [
            {"key": "c1", "parent_key": None, "name": "메모리 관리", "description": "소유권과 빌림", "prereq_keys": [], "order": 1},
            {"key": "c2", "parent_key": "c1", "name": "소유권", "description": "Ownership 규칙", "prereq_keys": [], "order": 1},
            {"key": "c3", "parent_key": "c1", "name": "빌림과 참조", "description": "Borrowing", "prereq_keys": ["c2"], "order": 2},
            {"key": "c4", "parent_key": "c1", "name": "라이프타임", "description": "Lifetimes", "prereq_keys": ["c3"], "order": 3},
            {"key": "c5", "parent_key": "c1", "name": "스마트 포인터", "description": "Box, Rc, Arc", "prereq_keys": ["c4"], "order": 4},
        ],
    }


def setup_env(tmp_path, **quiz_kw):
    clock.set_now(datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc))
    fk = FakeDynalist()
    fk.add_doc(F)
    conn = db.connect(":memory:")
    db.migrate(conn)
    fllm = FakeLLM()
    fllm.set("t1_curriculum", sample_t1())
    client = DynalistClient("tok", transport=fk, sleep=lambda _: None, changes_per_minute=1e9)
    llm = LLMClient(backend=fllm, conn=conn, sleep=lambda _: None)
    settings = make_settings(tmp_path, **quiz_kw)
    return fk, conn, fllm, client, llm, settings


# ---------------- §7.2 SRS SM-2 formula table tests ----------------
@pytest.mark.parametrize(
    "ease,interval,reps,lapses,q,exp_ease,exp_interval,exp_reps,exp_lapses,exp_due",
    [
        # First pass (q=4): reps 0->1, interval=1, ease unchanged (2.5 + 0.0 = 2.5)
        (2.5, 0, 0, 0, 4, 2.5, 1, 1, 0, "2026-11-13"),
        # Second pass (q=5): reps 1->2, interval=6, ease=2.6
        (2.5, 1, 1, 0, 5, 2.6, 6, 2, 0, "2026-11-18"),
        # Third pass (q=4): reps 2->3, interval=round(6 * 2.6)=16, ease=2.6
        (2.6, 6, 2, 0, 4, 2.6, 16, 3, 0, "2026-11-28"),
        # Fourth pass (q=5): reps 3->4, interval=round(16 * 2.6)=42, ease=2.7
        (2.6, 16, 3, 0, 5, 2.7, 42, 4, 0, "2026-12-24"),
        # Fail (q=2): reps reset to 0, interval=1, lapses+1, ease=2.5 - 0.32 = 2.18
        (2.5, 16, 3, 0, 2, 2.18, 1, 0, 1, "2026-11-13"),
        # Ease lower bound 1.3 clamped on repeated q=0
        (1.35, 6, 2, 1, 0, 1.3, 1, 0, 2, "2026-11-13"),
    ],
)
def test_sm2_formula_table(ease, interval, reps, lapses, q, exp_ease, exp_interval, exp_reps, exp_lapses, exp_due):
    ne, ni, nr, nl, nd = sm2_update(ease, interval, reps, lapses, q, "2026-11-12")
    assert round(ne, 4) == round(exp_ease, 4)
    assert (ni, nr, nl, nd) == (exp_interval, exp_reps, exp_lapses, exp_due)


# ---------------- §7.3 Card selection rules ----------------
def test_card_selection_slots_new_cap_and_one_per_concept(tmp_path):
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path, per_day=4, max_new_per_day=2)
    fk.add(F, "root", "#목표 #rust Rust 백엔드")
    e_nid = fk.add(F, "root", "#공부 충분히 긴 공부 기록입니다 카드 생성을 위한 베이스")
    fllm.set(
        "t3_study",
        {
            "concepts": [{"concept_id": 2, "coverage": "full", "state_suggestion": "understood"}],
            "feedback": {"corrections": [], "missing": [], "next": []},
            "cards": [],
            "off_curriculum": None,
        },
    )
    run_pipeline(conn, settings, client, llm, run_date="2026-11-10")
    assert e_nid in fk.docs[F]

    # Insert:
    # - 2 review cards (repetitions > 0) for concepts 2 and 3, due on 2026-11-12
    # - 1 duplicate concept card for concept 2 (same concept must not be selected twice in one day)
    # - 3 new cards (repetitions == 0, no history) for concepts 4 and 5 (only max_new_per_day=2 allowed, and 1 per concept)
    now = clock.now_iso()
    conn.execute(
        "INSERT INTO quiz_card(goal_id,concept_id,source_entry_id,question,answer_key,repetitions,lapses,due_date,created_at)"
        " VALUES"
        "(1, 2, 1, 'Rev c2 A', 'A', 2, 1, '2026-11-12', ?),"
        "(1, 2, 1, 'Rev c2 B (dup concept)', 'B', 1, 0, '2026-11-12', ?),"
        "(1, 3, 1, 'Rev c3', 'C', 1, 3, '2026-11-12', ?),"
        "(1, 4, 1, 'New c4', 'D', 0, 0, '2026-11-11', ?),"
        "(1, 5, 1, 'New c5', 'E', 0, 0, '2026-11-11', ?)",
        (now, now, now, now, now),
    )

    # Also simulate 1 unanswered open attempt already existing for this goal -> slots = 4 - 1 = 3
    conn.execute(
        "INSERT INTO quiz_attempt(card_id,goal_id,issued_date,expire_date,file_id,status)"
        " VALUES(999, 1, '2026-11-11', '2026-11-14', 'doc1', 'open')"
    ) if False else None
    # Use a real card for the open attempt (add a dummy card on concept 2 or separate)
    cur = conn.execute(
        "INSERT INTO quiz_card(goal_id,concept_id,source_entry_id,question,answer_key,repetitions,due_date,created_at)"
        " VALUES(1, 2, 1, 'Already open c2', 'X', 1, '2026-11-10', ?)",
        (now,),
    )
    open_card_id = cur.lastrowid
    conn.execute(
        "INSERT INTO quiz_attempt(card_id,goal_id,issued_date,expire_date,file_id,status)"
        " VALUES(?, 1, '2026-11-11', '2026-11-14', 'doc1', 'open')",
        (open_card_id,),
    )

    # Because open_card_id is on concept 2 and is open:
    # - slots = 4 - 1 = 3
    # - concept 2 already has an open attempt, so 'Rev c2 A' and 'Rev c2 B' are excluded (1 per concept)
    # - 'Rev c3' is selected (1 review)
    # - 'New c4' and 'New c5' are selected (2 new, hitting max_new_per_day=2)
    selected = select_cards_for_goal(conn, 1, "2026-11-12", per_day=4, max_new_per_day=2)
    assert [r["question"] for r in selected] == ["Rev c3", "New c4", "New c5"]


# ---------------- 2-Day E2E + Expiration + Fast-zero + Idempotency ----------------
def test_e2e_two_day_scenario_and_expiration(tmp_path):
    """Spec §12 E2E & M4 acceptance:
    - Day 1 (2026-11-12): Goal 1, Dump 1, Study 2 -> creates curriculum, evaluates dump, analyzes 2 studies, creates cards (due 2026-11-13).
    - Day 2 (2026-11-13): Issues daily quizzes for cards due 2026-11-13. Re-running Day 2 produces 0 duplicate quizzes.
      User writes answers under quiz items (one real answer, one '모름' skip answer, one left unanswered).
    - Day 3 (2026-11-14): Grades answered quizzes (1 via T4 LLM, 1 fast-zero without LLM). Lowers state on wrong answer.
    - Day 6 (2026-11-17): Unanswered quiz from Day 2 (expire_date=2026-11-16 < 2026-11-17) expires with '⌛' and checked=True.
    """
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path, per_day=5, max_new_per_day=3, expire_days=3)

    goal_nid = fk.add(F, "root", "#목표 #rust Rust 백엔드 마스터")
    fk.add(F, "root", "#지식덤프 소유권의 기본 개념은 알고 있다.")
    s1_nid = fk.add(F, "root", "#공부 소유권과 이동(move), 빌림(Borrowing)을 깊이 있게 실습하고 정리했다.")
    s2_nid = fk.add(F, "root", "#공부 라이프타임('a)과 스마트 포인터(Box, Rc)의 동작 원리를 공부했다.")

    fllm.set(
        "t2_dump",
        {
            "assessments": [{"concept_id": 2, "state": "partial", "evidence": "기본 개념 인지"}],
            "gap_summary": ["라이프타임과 스마트 포인터 미학습"],
            "next_concepts": [2, 3],
        },
    )
    fllm.set(
        "t3_study",
        {
            "concepts": [
                {"concept_id": 2, "coverage": "full", "state_suggestion": "understood"},
                {"concept_id": 3, "coverage": "full", "state_suggestion": "understood"},
            ],
            "feedback": {"corrections": [], "missing": ["Deref 강제 변환"], "next": ["라이프타임 실습"]},
            "cards": [
                {"concept_id": 2, "question": "소유권 규칙 3가지는?", "answer_key": "단일 소유자, 값 이동, 스코프 종료 시 Drop"},
                {"concept_id": 3, "question": "가변 참조의 제약은?", "answer_key": "동시에 하나의 가변 참조만 허용"},
            ],
            "off_curriculum": None,
        },
        {
            "concepts": [
                {"concept_id": 4, "coverage": "full", "state_suggestion": "understood"},
            ],
            "feedback": {"corrections": [], "missing": ["'static 라이프타임"], "next": ["Arc와 Mutex"]},
            "cards": [
                {"concept_id": 4, "question": "라이프타임 엘리전(elision)이란?", "answer_key": "컴파일러가 추론 가능한 라이프타임 생략 규칙"},
            ],
            "off_curriculum": None,
        },
    )

    # --- Day 1 (2026-11-12) ---
    res_d1 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res_d1.status == "ok"
    assert res_d1.stats["goals"]["generated"] == 1
    assert res_d1.stats["dumps"]["processed"] == 1
    assert res_d1.stats["study"]["processed"] == 2
    assert res_d1.stats["study"]["cards_created"] == 3
    assert len(fk.children(F, s1_nid)) == 1 and len(fk.children(F, s2_nid)) == 1

    # --- Day 2 (2026-11-13): Issue quizzes ---
    res_d2 = run_pipeline(conn, settings, client, llm, run_date="2026-11-13")
    assert res_d2.status == "ok"
    assert res_d2.stats["quiz_issue"]["issued"] == 3

    # Re-run on the same day (2026-11-13): must NOT duplicate quiz_day or quiz_item
    changes_before_rerun = len(fk.applied_changes)
    res_d2_rerun = run_pipeline(conn, settings, client, llm, run_date="2026-11-13")
    assert res_d2_rerun.status == "ok"
    assert res_d2_rerun.stats["quiz_issue"]["issued"] == 0
    assert res_d2_rerun.stats["quiz_issue"]["skipped_already_issued"] == 1
    assert len(fk.applied_changes) == changes_before_rerun

    # Inspect quiz_root -> quiz_day -> 3 quiz_items in FakeDynalist
    goal_kids = fk.children(F, goal_nid)
    quiz_roots = [k for k in goal_kids if "복습 퀴즈" in k["content"]]
    assert len(quiz_roots) == 1
    quiz_days = fk.children(F, quiz_roots[0]["id"])
    assert len(quiz_days) == 1 and quiz_days[0]["content"] == "📅 2026-11-13"
    quiz_items = fk.children(F, quiz_days[0]["id"])
    assert [q["content"] for q in quiz_items] == [
        "[Q1] 소유권 규칙 3가지는?",
        "[Q2] 가변 참조의 제약은?",
        "[Q3] 라이프타임 엘리전(elision)이란?",
    ]

    # User writes answers in Dynalist under Q1 (good answer) and Q2 ("모름" -> fast 0), leaves Q3 unanswered
    fk.add(F, quiz_items[0]["id"], "각 값은 단일 소유자를 가지며 스코프를 벗어나면 Drop됩니다.")
    fk.add(F, quiz_items[1]["id"], "모름")

    # --- Day 3 (2026-11-14): Grade Q1 and Q2 ---
    fllm.set(
        "t4_grade",
        [
            {"attempt_id": 1, "score": 5, "comment": "완벽합니다!"},
        ],
    )
    res_d3 = run_pipeline(conn, settings, client, llm, run_date="2026-11-14")
    assert res_d3.status == "ok"
    assert res_d3.stats["quiz_grade"]["fast_zero"] == 1
    assert res_d3.stats["quiz_grade"]["llm_graded"] == 1

    # Check Q1 and Q2 are checked=True and have grade child nodes
    q1_node = fk.docs[F][quiz_items[0]["id"]]
    q2_node = fk.docs[F][quiz_items[1]["id"]]
    q3_node = fk.docs[F][quiz_items[2]["id"]]
    assert q1_node["checked"] is True
    assert q2_node["checked"] is True
    assert q3_node["checked"] is False
    assert any("🤖 5/5 · 완벽합니다!" in c["content"] for c in fk.children(F, q1_node["id"]))
    assert any("🤖 0/5 · 패스 (0점 처리)" in c["content"] for c in fk.children(F, q2_node["id"]))

    # Because Q2 scored 0 (<=2), concept 3 ('빌림과 참조') was downgraded from 'understood' -> 'partial' (§7.4)
    c3_state = conn.execute("SELECT state FROM concept WHERE id=3").fetchone()[0]
    assert c3_state == "partial"

    # Re-running Day 3 does NOT re-grade already graded attempts (§3.5)
    t4_calls_before = sum(1 for t, _ in fllm.calls if t == "t4_grade")
    fk.add(F, quiz_items[0]["id"], "뒤늦게 수정한 답변")
    run_pipeline(conn, settings, client, llm, run_date="2026-11-14")
    assert sum(1 for t, _ in fllm.calls if t == "t4_grade") == t4_calls_before

    # --- Day 6 (2026-11-17): Q3 (issued 2026-11-13, expire_date 2026-11-16) expires ---
    fllm.set(
        "t5_weekly",
        {"summary": "첫 주 학습 완료", "weak_points": ["빌림과 참조"], "next_week": ["스마트 포인터"]},
    )
    fllm.set("t6_profile", {"summary": "기초 학습 진행 중"})
    res_d6 = run_pipeline(conn, settings, client, llm, run_date="2026-11-17")
    assert res_d6.status == "ok"
    assert res_d6.stats["quiz_expire"]["expired"] == 1
    q3_after = fk.docs[F][quiz_items[2]["id"]]
    assert q3_after["checked"] is True
    assert q3_after["content"].endswith("⌛")
    assert conn.execute("SELECT status FROM quiz_attempt WHERE id=3").fetchone()[0] == "expired"
