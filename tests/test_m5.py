"""M5 tests: Weekly report (T5) on Monday & catch-up, Learner profile rolling update (T6),
daily token budget & run_log / llm_call aggregation, `tutorbot stats` CLI, and SQLite 7-day backup rotation (spec §12, §13, §14).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.fakes import FakeDynalist, FakeLLM
from tutorbot import cli, clock, db
from tutorbot.dynalist.client import DynalistClient
from tutorbot.llm.client import LLMClient
from tutorbot.parse.classify import DEFAULT_TAGS
from tutorbot.pipeline import backup_sqlite_db, run_pipeline

F = "doc1"


def make_settings(tmp_path, daily_token_budget=300000):
    return SimpleNamespace(
        timezone="Asia/Seoul",
        db_path=str(tmp_path / "tb.sqlite3"),
        domains={"study": SimpleNamespace(enabled=True, file_ids=[F])},
        tags=DEFAULT_TAGS,
        quiz=SimpleNamespace(
            per_day=5,
            max_new_per_day=2,
            expire_days=3,
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
            provider="gemini",
            model_smart="gemini-3.5-flash-lite",
            model_fast="gemini-3.5-flash-lite",
            index_full_threshold=150,
            daily_token_budget=daily_token_budget,
        ),
    )


def sample_t1():
    return {
        "goal": {"title": "Rust 백엔드 마스터", "level": "실무", "deadline": "2026-12-31"},
        "concepts": [
            {"key": "c1", "parent_key": None, "name": "메모리 관리", "description": "소유권과 빌림", "prereq_keys": [], "order": 1},
            {"key": "c2", "parent_key": "c1", "name": "소유권", "description": "Ownership 규칙", "prereq_keys": [], "order": 1},
            {"key": "c3", "parent_key": "c1", "name": "빌림과 참조", "description": "Borrowing", "prereq_keys": ["c2"], "order": 2},
        ],
    }


def setup_env(tmp_path, daily_token_budget=300000):
    clock.set_now(datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc))
    fk = FakeDynalist()
    fk.add_doc(F)
    settings = make_settings(tmp_path, daily_token_budget=daily_token_budget)
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    fllm = FakeLLM()
    fllm.set("t1_curriculum", sample_t1())
    client = DynalistClient("tok", transport=fk, sleep=lambda _: None, changes_per_minute=1e9)
    llm = LLMClient(backend=fllm, conn=conn, sleep=lambda _: None)
    return fk, conn, fllm, client, llm, settings


# ---------------- T5 Weekly Report & T6 Learner Profile ----------------
def test_t5_weekly_report_and_t6_learner_profile_and_catchup(tmp_path):
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path)
    goal_nid = fk.add(F, "root", "#목표 #rust Rust 백엔드 마스터")
    fk.add(F, "root", "#공부 !(2026-11-12) 소유권과 빌림의 핵심 개념을 정리하고 실습했다.")

    fllm.set(
        "t3_study",
        {
            "concepts": [{"concept_id": 2, "coverage": "full", "state_suggestion": "understood"}],
            "feedback": {
                "corrections": ["Copy와 Clone 차이를 혼동함"],
                "missing": ["Drop 순서"],
                "next": ["빌림 실습"],
            },
            "cards": [],
            "off_curriculum": None,
        },
    )

    # 1) Thursday 2026-11-12 (2026-W46): Not Monday, and no activity on or before 2026-11-08 (W45 end) -> 0 weekly reports
    res_thu = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res_thu.status == "ok"
    assert res_thu.stats["weekly"]["reports_created"] == 0

    # 2) Monday 2026-11-16 (start of 2026-W47): Generates weekly report for 2026-W46 (2026-11-09..2026-11-15) + updates T6 learner_profile
    fllm.set(
        "t5_weekly",
        {
            "summary": "소유권 기초를 다진 한 주였습니다.",
            "weak_points": ["Copy/Clone 구분"],
            "next_week": ["빌림과 참조 집중 학습"],
        },
    )
    fllm.set(
        "t6_profile",
        {
            "summary": "학습자는 실습 위주로 빠르게 습득하나 Copy와 Clone 트레이트의 차이를 자주 혼동함.",
        },
    )
    res_mon = run_pipeline(conn, settings, client, llm, run_date="2026-11-16")
    assert res_mon.status == "ok"
    assert res_mon.stats["weekly"]["reports_created"] == 1
    assert res_mon.stats["weekly"]["profiles_updated"] == 1

    # Check weekly_root -> weekly_report in FakeDynalist
    goal_kids = fk.children(F, goal_nid)
    weekly_roots = [k for k in goal_kids if "주간 리포트" in k["content"]]
    assert len(weekly_roots) == 1
    reports = fk.children(F, weekly_roots[0]["id"])
    assert len(reports) == 1
    assert reports[0]["content"] == "📊 2026-W46 (진도 50% · 공부 1건 · 퀴즈 0%)"
    rep_lines = [c["content"] for c in fk.children(F, reports[0]["id"])]
    assert any("📝 요약: 소유권 기초를 다진 한 주였습니다." in l for l in rep_lines)
    assert any("⚠️ 약점: Copy/Clone 구분" in l for l in rep_lines)

    # Check learner_profile saved in DB
    prof = conn.execute("SELECT summary FROM learner_profile WHERE goal_id=1").fetchone()
    assert "Copy와 Clone" in prof["summary"]

    # 3) Catch-up test: Suppose Monday 2026-11-23 was missed and bot runs on Tuesday 2026-11-24 -> still creates 2026-W47 report once!
    res_tue = run_pipeline(conn, settings, client, llm, run_date="2026-11-24")
    assert res_tue.status == "ok"
    assert res_tue.stats["weekly"]["reports_created"] == 1
    reports2 = fk.children(F, weekly_roots[0]["id"])
    assert len(reports2) == 2
    assert reports2[0]["content"].startswith("📊 2026-W47")  # newest at index 0


# ---------------- Budget limit, run_log/llm_call stats & SQLite backup ----------------
def test_budget_cap_and_stats_command_and_sqlite_backup_rotation(tmp_path, capsys):
    # Each FakeLLM call records 100 prompt + 50 completion = 150 tokens.
    # Set budget to 300 tokens: T1 curriculum (150 tokens) + 1st study entry (150 tokens) = 300 tokens -> 2nd study entry is deferred by budget!
    fk, conn, fllm, client, llm, settings = setup_env(tmp_path, daily_token_budget=300)
    fk.add(F, "root", "#목표 #rust Rust 백엔드 마스터")
    fk.add(F, "root", "#공부 첫 번째 충분히 긴 공부 기록입니다 예산 내 처리됨")
    fk.add(F, "root", "#공부 두 번째 충분히 긴 공부 기록입니다 예산 초과로 다음 배치로 미뤄짐")

    fllm.set(
        "t3_study",
        {
            "concepts": [{"concept_id": 2, "coverage": "partial", "state_suggestion": "partial"}],
            "feedback": {"corrections": [], "missing": [], "next": ["계속"]},
            "cards": [],
            "off_curriculum": None,
        },
    )

    res = run_pipeline(conn, settings, client, llm, run_date="2026-11-12")
    assert res.status == "ok"
    assert res.stats["study"]["processed"] == 1
    assert res.stats["study"]["budget_deferred"] == 1

    # Verify run_log and llm_call aggregation via `cmd_stats`
    rc = cli.cmd_stats(SimpleNamespace(), settings)
    assert rc == 0
    captured = json.loads(capsys.readouterr().out)
    assert len(captured["recent_runs"]) == 1
    assert captured["recent_runs"][0]["llm_calls"] == 2
    assert captured["recent_runs"][0]["llm_prompt_tokens"] == 200
    assert captured["recent_runs"][0]["llm_completion_tokens"] == 100
    tasks = {r["task"]: r["calls"] for r in captured["llm_by_task"]}
    assert tasks == {"t1_curriculum:v1": 1, "t3_study:v1": 1}

    # Verify SQLite backup rotation keeps at most 7 files
    for day in range(10, 20):
        backup_sqlite_db(settings.db_path, f"2026-11-{day:02d}", keep_days=7)
    backup_dir = Path(settings.db_path).parent / "backup"
    backups = sorted(p.name for p in backup_dir.glob("tutorbot-*.sqlite3"))
    assert len(backups) == 7
    assert backups[0] == "tutorbot-20261113.sqlite3"
    assert backups[-1] == "tutorbot-20261119.sqlite3"
