"""M3 tests: Knowledge dump evaluation (T2), Study entry analysis (T3: mapping + feedback + cards),
feedback in-place update on modified study node, minor edit similarity skip, settle deferral,
and concept state transition rules §7.4 (spec §12, §14).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from tests.fakes import FakeDynalist, FakeLLM
from tutorbot import clock, db
from tutorbot.dynalist.client import DynalistClient
from tutorbot.engine.state import (
    apply_dump_transition,
    apply_quiz_transition,
    apply_study_transition,
)
from tutorbot.llm.client import LLMClient
from tutorbot.parse.classify import DEFAULT_TAGS
from tutorbot.pipeline import run_pipeline

F = "doc1"


def make_settings(tmp_path, settle_minutes=60):
    return SimpleNamespace(
        timezone="Asia/Seoul",
        db_path=str(tmp_path / "tb.sqlite3"),
        dynalist=SimpleNamespace(file_ids=[F]),
        tags=DEFAULT_TAGS,
        processing=SimpleNamespace(
            min_entry_chars=20,
            settle_minutes=settle_minutes,
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
        ],
    }


def setup_env(tmp_path, settle_minutes=60):
    # FakeDynalist clock starts around 1_700_000_000_000 ms (2023-11-14).
    # Set clock.now() well after that so nodes are settled by default unless tested otherwise.
    clock.set_now(datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc))
    fk = FakeDynalist()
    fk.add_doc(F)
    conn = db.connect(":memory:")
    db.migrate(conn)
    fllm = FakeLLM()
    fllm.set("t1_curriculum", sample_t1())
    client = DynalistClient("tok", transport=fk, sleep=lambda _: None, changes_per_minute=1e9)
    llm = LLMClient(backend=fllm, conn=conn, sleep=lambda _: None)
    settings = make_settings(tmp_path, settle_minutes=settle_minutes)
    return fk, conn, fllm, client, llm, settings


# ---------------- §7.4 State transition rules ----------------
def test_state_transition_rules_sec_7_4(tmp_path):
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path)
    fk.add(F, "root", "#목표 #rust Rust 백엔드")
    run_pipeline(conn, settings, client, llm, run_date="2026-11-12")

    c_own = conn.execute("SELECT id FROM concept WHERE name='소유권'").fetchone()["id"]
    c_bor = conn.execute("SELECT id FROM concept WHERE name='빌림과 참조'").fetchone()["id"]

    # 1) Dump raises unknown -> understood, and since no study/quiz event exists, dump re-eval can lower it
    assert apply_dump_transition(conn, c_own, "understood", dump_id=1, evidence="잘 앎")
    assert conn.execute("SELECT state FROM concept WHERE id=?", (c_own,)).fetchone()[0] == "understood"
    assert apply_dump_transition(conn, c_own, "partial", dump_id=1, evidence="다시 보니 부분")
    assert conn.execute("SELECT state FROM concept WHERE id=?", (c_own,)).fetchone()[0] == "partial"

    # 2) Study transition: coverage=mention -> no change; coverage=partial -> partial; coverage=full + understood -> understood
    assert not apply_study_transition(conn, c_bor, "mention", "understood", entry_id=10)
    assert conn.execute("SELECT state FROM concept WHERE id=?", (c_bor,)).fetchone()[0] == "unknown"

    assert apply_study_transition(conn, c_bor, "partial", "understood", entry_id=10)
    assert conn.execute("SELECT state FROM concept WHERE id=?", (c_bor,)).fetchone()[0] == "partial"

    assert apply_study_transition(conn, c_bor, "full", "understood", entry_id=11)
    assert conn.execute("SELECT state FROM concept WHERE id=?", (c_bor,)).fetchone()[0] == "understood"

    # Once a study event exists on c_bor, dump cannot lower it
    assert not apply_dump_transition(conn, c_bor, "partial", dump_id=2, evidence="하향 시도")
    assert conn.execute("SELECT state FROM concept WHERE id=?", (c_bor,)).fetchone()[0] == "understood"

    # Study never lowers understood -> partial
    assert not apply_study_transition(conn, c_bor, "partial", "partial", entry_id=12)
    assert conn.execute("SELECT state FROM concept WHERE id=?", (c_bor,)).fetchone()[0] == "understood"

    # 3) Quiz q <= 2 lowers understood/mastered -> partial
    assert apply_quiz_transition(conn, c_bor, score=2, attempt_id=100, interval_days=1)
    assert conn.execute("SELECT state FROM concept WHERE id=?", (c_bor,)).fetchone()[0] == "partial"

    # 4) SRS interval >= 21 and score >= 4 raises to mastered
    assert apply_quiz_transition(conn, c_bor, score=4, attempt_id=101, interval_days=21)
    assert conn.execute("SELECT state FROM concept WHERE id=?", (c_bor,)).fetchone()[0] == "mastered"


# ---------------- T2 Knowledge Dump ----------------
def test_t2_dump_evaluates_filters_hallucinations_and_updates_progress(tmp_path):
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path)
    fk.add(F, "root", "#목표 #rust Rust 백엔드")
    dump_nid = fk.add(F, "root", "#지식덤프 나는 러스트 소유권은 확실히 알고 빌림은 조금 안다")

    # Run 1 creates curriculum (concept ids: 1=메모리 관리(branch), 2=소유권, 3=빌림과 참조, 4=라이프타임)
    fllm.set(
        "t2_dump",
        {
            "assessments": [
                {"concept_id": 2, "state": "understood", "evidence": "소유권 확실히 앎"},
                {"concept_id": 3, "state": "partial", "evidence": "빌림 조금 앎"},
                {"concept_id": 9999, "state": "understood", "evidence": "환각 ID"},
            ],
            "gap_summary": ["라이프타임 보충 필요"],
            "next_concepts": [4],
        },
    )
    res = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res.status == "ok"
    assert res.stats["dumps"]["processed"] == 1
    assert res.stats["dumps"]["transitions"] == 2

    states = dict(conn.execute("SELECT name, state FROM concept WHERE is_leaf=1").fetchall())
    assert states == {"소유권": "understood", "빌림과 참조": "partial", "라이프타임": "unknown"}

    # Modify dump to remove '빌림과 참조' -> since c3 had only dump events, it drops back to unknown
    fk.docs[F][dump_nid]["content"] = "#지식덤프 소유권만 알고 빌림은 사실 모른다"
    fllm.set(
        "t2_dump",
        {
            "assessments": [
                {"concept_id": 2, "state": "understood", "evidence": "소유권만 앎"},
            ],
            "gap_summary": ["빌림부터 다시"],
            "next_concepts": [3, 4],
        },
    )
    res2 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res2.status == "ok"
    states2 = dict(conn.execute("SELECT name, state FROM concept WHERE is_leaf=1").fetchall())
    assert states2["빌림과 참조"] == "unknown"


# ---------------- T3 Study Analysis, In-place Feedback Update & Minor Edit Skip ----------------
def test_t3_study_feedback_update_in_place_and_similarity_skip(tmp_path):
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path)
    fk.add(F, "root", "#목표 #rust Rust 백엔드")
    entry_nid = fk.add(
        F,
        "root",
        "#공부 오늘은 러스트의 소유권 이동(move)과 복사(Copy 트레이트)의 차이를 자세히 공부하고 예제를 작성했다.",
    )

    fllm.set(
        "t3_study",
        {
            "concepts": [
                {"concept_id": 2, "coverage": "full", "state_suggestion": "understood"},
                {"concept_id": 999, "coverage": "full", "state_suggestion": "understood"},  # hallucinated id
            ],
            "feedback": {
                "corrections": ["Copy 트레이트는 힙 데이터를 깊은 복사하지 않습니다."],
                "missing": ["Drop 트레이트와의 상호배제 규칙"],
                "next": ["빌림과 불변/가변 참조 규칙 공부하기"],
            },
            "cards": [
                {"concept_id": 2, "question": "Move와 Copy의 차이는?", "answer_key": "소유권 이전 vs 비트 단위 복사"},
                {"concept_id": 999, "question": "환각 질문", "answer_key": "환각 답"},
            ],
            "off_curriculum": None,
        },
    )

    # 1) Initial study analysis
    res1 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res1.status == "ok"
    assert res1.stats["study"]["processed"] == 1
    assert res1.stats["study"]["cards_created"] == 1

    # Check feedback node created under entry_nid
    entry_kids1 = fk.children(F, entry_nid)
    assert len(entry_kids1) == 1
    fb_node_id = entry_kids1[0]["id"]
    assert entry_kids1[0]["content"] == "🤖 피드백 (2026-11-12) #tutorbot"
    assert entry_kids1[0]["note"].startswith("tb:feedback:1")
    fb_lines1 = [c["content"] for c in fk.children(F, fb_node_id)]
    assert len(fb_lines1) == 4
    assert any("✏️ 교정:" in l for l in fb_lines1)
    assert any("🧩 매핑: 소유권" in l for l in fb_lines1)

    # 2) Minor edit (similarity >= 0.95): add a single punctuation/word -> skipped without LLM call
    calls_before_minor = len(fllm.calls)
    fk.docs[F][entry_nid]["content"] += "!"
    res_minor = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res_minor.status == "ok"
    assert res_minor.stats["study"]["skipped_similar"] == 1
    assert len(fllm.calls) == calls_before_minor
    assert len(fk.children(F, entry_nid)) == 1

    # 3) Substantive edit (similarity < 0.95): add child nodes about Borrowing -> re-analyzed & updates SAME feedback node
    fk.add(
        F,
        entry_nid,
        "추가로 빌림(Borrowing)과 불변 참조(&T), 가변 참조(&mut T)가 동시에 존재할 수 없는 이유를 정리했다.",
    )
    fllm.set(
        "t3_study",
        {
            "concepts": [
                {"concept_id": 2, "coverage": "full", "state_suggestion": "understood"},
                {"concept_id": 3, "coverage": "full", "state_suggestion": "understood"},
            ],
            "feedback": {
                "corrections": [],
                "missing": ["NLL(Non-Lexical Lifetimes) 개념"],
                "next": ["라이프타임 문법 'a 학습"],
            },
            "cards": [
                # Card for concept 2 should be ignored because (entry, concept 2) already has a card (D2)
                {"concept_id": 2, "question": "중복 카드 질문", "answer_key": "중복 답"},
                # Card for concept 3 is new and should be created
                {"concept_id": 3, "question": "&T와 &mut T의 공존 규칙은?", "answer_key": "가변 참조는 단독으로만 가능"},
            ],
            "off_curriculum": None,
        },
    )

    res2 = run_pipeline(conn, settings, client, llm, run_date="2026-11-13")
    assert res2.status == "ok"
    assert res2.stats["study"]["processed"] == 1
    assert res2.stats["study"]["cards_created"] == 1

    # Verify the feedback header node ID is STILL fb_node_id (updated in-place, not duplicated)
    fb_nodes = [c for c in fk.children(F, entry_nid) if "피드백" in c["content"]]
    assert len(fb_nodes) == 1
    assert fb_nodes[0]["id"] == fb_node_id
    assert fb_nodes[0]["content"] == "🤖 피드백 (2026-11-13) #tutorbot"

    # Old child lines were deleted and replaced by the new 3 lines
    fb_lines2 = [c["content"] for c in fk.children(F, fb_node_id)]
    assert fb_lines2 == [
        "🔑 빠진 핵심: NLL(Non-Lexical Lifetimes) 개념",
        "➡️ 다음: 라이프타임 문법 'a 학습",
        "🧩 매핑: 소유권, 빌림과 참조",
    ]

    # DB has 2 feedback versions and 2 quiz cards total (concept 2 and concept 3)
    assert conn.execute("SELECT COUNT(*) FROM feedback WHERE entry_id=1").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM quiz_card WHERE source_entry_id=1").fetchone()[0] == 2


def test_settle_rule_defers_recently_modified_entry(tmp_path):
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path, settle_minutes=60)
    fk.add(F, "root", "#목표 #rust Rust 백엔드")

    # Set clock right at FakeDynalist's node modified time + 10 minutes (< 60m settle_minutes)
    entry_nid = fk.add(
        F,
        "root",
        "#공부 지금 막 작성 중인 공부 기록입니다 아직 정착 시간이 지나지 않았습니다.",
    )
    mod_ms = fk.docs[F][entry_nid]["modified"]
    clock.set_now(datetime.fromtimestamp((mod_ms + 10 * 60 * 1000) / 1000, tz=timezone.utc))

    res1 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res1.status == "ok"
    assert res1.stats["study"]["deferred_settle"] == 1
    assert conn.execute("SELECT status FROM study_entry").fetchone()[0] == "deferred"

    # Advance clock past 60 minutes -> now it gets processed
    clock.set_now(datetime.fromtimestamp((mod_ms + 65 * 60 * 1000) / 1000, tz=timezone.utc))
    fllm.set(
        "t3_study",
        {
            "concepts": [{"concept_id": 2, "coverage": "partial", "state_suggestion": "partial"}],
            "feedback": {"corrections": [], "missing": [], "next": ["계속 작성"]},
            "cards": [],
            "off_curriculum": None,
        },
    )
    res2 = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res2.status == "ok"
    assert res2.stats["study"]["processed"] == 1
    assert conn.execute("SELECT status FROM study_entry").fetchone()[0] == "processed"
