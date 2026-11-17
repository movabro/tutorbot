from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tutorbot.config import Settings, _parse_toml_bytes, load_settings
from tutorbot.db import connect, migrate
from tutorbot.dynalist.tree import NodeIndex
from tutorbot.engine.domains import _personal_tasks, process_domains
from tutorbot.llm.client import LLMClient
from tutorbot.parse.personal_tasks import parse_tracked_tasks
from tutorbot.dynalist.client import DynalistClient
from tutorbot.pipeline import run_pipeline
from tests.fakes import FakeDynalist


def test_toml_decode_error_is_not_chained_to_parser_fallback():
    if sys.version_info < (3, 11):
        pytest.importorskip("tomli")

    with pytest.raises(ValueError) as error:
        _parse_toml_bytes(b"invalid = [unquoted]")

    assert error.value.__context__ is None


def _index(fake: FakeDynalist, file_id: str) -> NodeIndex:
    return NodeIndex.from_doc_read(file_id, fake._doc_read({"file_id": file_id}))


def test_domain_settings_preserve_legacy_learning_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[dynalist]\nfile_ids = ["study-doc"]\n'
        '[domains.work]\nfile_ids = ["work-doc"]\ntags = ["project"]\n'
        '[domains.career]\nfile_ids = ["career-doc"]\n'
        '[domains.personal_tasks]\ntracking_tag_aliases = ["Task"]\n',
        encoding="utf-8",
    )
    settings = load_settings(path, dotenv_path=path.with_suffix(".env"))

    assert settings.dynalist.file_ids == ["study-doc"]
    assert settings.domains["study"].file_ids == []
    assert settings.domains["work"].file_ids == ["work-doc"]
    assert settings.domains["work"].tags == ["project"]
    assert settings.domains["career"].source_domains == ["work", "life"]
    assert settings.domains["personal_tasks"].tracking_tag == "Track"
    assert settings.domains["personal_tasks"].tracking_tag_aliases == ["Task"]


def test_personal_task_parser_requires_track_and_preserves_recursive_detail():
    fake = FakeDynalist()
    fake.add_doc("tasks")
    backlog = fake.add("tasks", "root", "Backlog")
    category = fake.add("tasks", backlog, "해내고 싶은 프로젝트")
    task = fake.add("tasks", category, "포트폴리오 작성 #Track")
    progress = fake.add("tasks", task, "초안 작성 !(2026-11-14)", "범위와 성과를 정리함")
    detail = fake.add("tasks", progress, "프로젝트 A")
    leaf = fake.add("tasks", detail, "담당 업무와 결과")
    untracked = fake.add("tasks", category, "나중에 할 수도 있는 일")
    nested = fake.add("tasks", task, "별도 작업 #Track")

    tasks, warnings = parse_tracked_tasks(_index(fake, "tasks"))

    assert not warnings
    assert [item.node.id for item in tasks] == [task, nested]
    primary = tasks[0]
    assert primary.status == "Backlog"
    assert [entry.node_id for entry in primary.progress] == [progress]
    record = primary.progress[0]
    assert record.entry_date.isoformat() == "2026-11-14"
    assert "[note] 범위와 성과를 정리함" in record.detail_paths
    assert any("프로젝트 A > 담당 업무와 결과" in path for path in record.detail_paths)
    assert untracked not in {item.node.id for item in tasks}


def test_personal_task_tag_alias_is_recognized():
    fake = FakeDynalist()
    fake.add_doc("tasks")
    state = fake.add("tasks", "root", "InProgress")
    task = fake.add("tasks", state, "작업 #Task")

    tasks, warnings = parse_tracked_tasks(
        _index(fake, "tasks"), tracking_tag=("Track", "Task"),
    )

    assert [item.node.id for item in tasks] == [task]
    assert tasks[0].status == "InProgress"
    assert not warnings


def test_invalid_progress_date_is_diagnostic_and_not_normalized():
    fake = FakeDynalist()
    fake.add_doc("tasks")
    state = fake.add("tasks", "root", "FastTrack")
    task = fake.add("tasks", state, "작업 #Track")
    progress = fake.add("tasks", task, "작업 기록 !(2026-02-30)")

    tasks, warnings = parse_tracked_tasks(_index(fake, "tasks"))

    record = tasks[0].progress[0]
    assert record.entry_date is None
    assert record.date_text == "2026-02-30"
    assert any(node_id == progress and "invalid progress date" in message for node_id, message in warnings)


def test_unrecognized_task_status_is_logged_without_llm_guessing():
    class NoCallBackend:
        def complete_json(self, task, messages, schema):
            raise AssertionError("an unclassified task must not be sent for analysis")

    fake = FakeDynalist()
    fake.add_doc("tasks")
    fake.add("tasks", "root", "분류되지 않은 작업 #Track")
    settings = Settings()
    settings.domains["personal_tasks"].file_ids = ["tasks"]
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-15','now','running')"
    ).lastrowid

    report = process_domains(
        conn, {"tasks": _index(fake, "tasks")}, settings,
        LLMClient(backend=NoCallBackend()), run_id=run_id,
    )

    diagnostic = conn.execute(
        "SELECT status,error FROM domain_run WHERE domain_id='personal_tasks'"
    ).fetchone()
    assert diagnostic["status"] == "warning"
    assert "no recognized status ancestor" in diagnostic["error"]
    assert report.warnings


def test_domain_analysis_retries_same_hash_after_error():
    class Backend:
        def __init__(self):
            self.fail = True
            self.calls = 0

        def complete_json(self, task, messages, schema):
            self.calls += 1
            if self.fail:
                raise RuntimeError("temporary failure")
            return {"summary": "분석 완료", "feedback": [], "evidence": []}

    fake = FakeDynalist()
    fake.add_doc("work")
    fake.add("work", "root", "업무 진행을 기록했다")
    settings = Settings()
    settings.domains["work"].file_ids = ["work"]
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-15','now','running')"
    ).lastrowid
    backend = Backend()
    llm = LLMClient(backend=backend, sleep=lambda _: None)
    indices = {"work": _index(fake, "work")}

    first = process_domains(conn, indices, settings, llm, run_id=run_id)
    backend.fail = False
    second = process_domains(conn, indices, settings, llm, run_id=run_id)
    third = process_domains(conn, indices, settings, llm, run_id=run_id)

    assert first.errors
    assert second.processed == 1
    assert third.skipped == 1
    assert backend.calls == 4
    assert conn.execute(
        "SELECT status FROM domain_state WHERE domain_id='work'"
    ).fetchone()["status"] == "processed"
    assert conn.execute("SELECT COUNT(*) FROM write_op").fetchone()[0] == 1


def test_personal_task_parser_excludes_bot_feedback_subtrees():
    class Backend:
        def __init__(self):
            self.messages = []

        def complete_json(self, task, messages, schema):
            self.messages.append(messages)
            return {"summary": "과업 요약", "feedback": [], "evidence": []}

    fake = FakeDynalist()
    fake.add_doc("tasks")
    state = fake.add("tasks", "root", "InProgress")
    task = fake.add("tasks", state, "작업 #Track")
    progress = fake.add("tasks", task, "기록 !(2026-11-14)")
    generated_progress_feedback = fake.add(
        "tasks", progress, "🤖 생성 피드백 #Track", "tb:domain_feedback:1",
    )
    fake.add("tasks", generated_progress_feedback, "하위 봇 노드 #Track")
    generated_root_feedback = fake.add(
        "tasks", "root", "🤖 요약 #Track", "tb:domain_feedback:2",
    )
    fake.add("tasks", generated_root_feedback, "하위 요약 #Track")
    settings = Settings()
    settings.domains["personal_tasks"].file_ids = ["tasks"]
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-15','now','running')"
    ).lastrowid
    backend = Backend()

    report = process_domains(
        conn, {"tasks": _index(fake, "tasks")}, settings,
        LLMClient(backend=backend), run_id=run_id,
    )

    assert report.processed == 1
    assert report.warnings == []
    assert conn.execute("SELECT COUNT(*) FROM personal_task").fetchone()[0] == 1
    progress_row = conn.execute("SELECT detail_paths_json FROM personal_task_progress").fetchone()
    assert "생성 피드백" not in progress_row["detail_paths_json"]
    assert "요약" not in backend.messages[0][-1]["content"]


def test_completion_events_are_idempotent_and_record_recompletion():
    fake = FakeDynalist()
    fake.add_doc("tasks")
    completed = fake.add("tasks", "root", "Completed")
    category = fake.add("tasks", completed, "가정, 생활관련")
    task = fake.add("tasks", category, "정리하기 #Track")
    conn = connect(":memory:")
    migrate(conn)

    _personal_tasks(conn, "tasks", _index(fake, "tasks"), Settings().domains["personal_tasks"])
    _personal_tasks(conn, "tasks", _index(fake, "tasks"), Settings().domains["personal_tasks"])
    initial = conn.execute("SELECT * FROM completion_event").fetchall()
    assert len(initial) == 1
    assert initial[0]["completed_at"] is None
    assert initial[0]["initial_discovery"] == 1
    assert initial[0]["category_path_json"] == '["가정, 생활관련"]'

    in_progress = fake.add("tasks", "root", "InProgress")
    fake.docs["tasks"][category]["children"].remove(task)
    fake.docs["tasks"][in_progress]["children"].append(task)
    _personal_tasks(conn, "tasks", _index(fake, "tasks"), Settings().domains["personal_tasks"])
    fake.docs["tasks"][in_progress]["children"].remove(task)
    fake.docs["tasks"][category]["children"].append(task)
    _personal_tasks(conn, "tasks", _index(fake, "tasks"), Settings().domains["personal_tasks"])

    events = conn.execute("SELECT * FROM completion_event ORDER BY transition_id").fetchall()
    assert [event["transition_id"] for event in events] == [1, 3]
    assert events[1]["completed_at"] is not None


def test_career_uses_only_configured_sources_and_writes_its_own_document():
    class Backend:
        def __init__(self):
            self.messages = []

        def complete_json(self, task, messages, schema):
            self.messages.append((task, messages))
            return {"summary": "요약", "feedback": ["피드백"], "evidence": ["기록"]}

    fake = FakeDynalist()
    fake.add_doc("work-doc")
    fake.add_doc("life-doc")
    fake.add_doc("career-doc")
    fake.add("work-doc", "root", "업무 목표에 맞춰 보고 자동화를 진행했다")
    fake.add("life-doc", "root", "가족 일정에 참석했다")
    fake.add("career-doc", "root", "커리어 방향 관찰")
    indices = {fid: _index(fake, fid) for fid in fake.docs}

    settings = Settings()
    settings.domains["work"].file_ids = ["work-doc"]
    settings.domains["life"].file_ids = ["life-doc"]
    settings.domains["life"].enabled = False
    settings.domains["career"].file_ids = ["career-doc"]
    settings.domains["career"].source_domains = ["work"]
    settings.domains["career"].tags = ["career", "#direction"]
    settings.domains["career"].rules = ["기록의 근거를 한 가지 이상 명시한다."]
    settings.domains["career"].prompt = "추가 사용자 지침"
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-15','now','running')"
    ).lastrowid
    backend = Backend()
    llm = LLMClient(backend=backend, conn=conn, run_id=run_id)

    first = process_domains(conn, indices, settings, llm, run_id=run_id)
    second = process_domains(conn, indices, settings, llm, run_id=run_id)

    career_message = next(
        messages for task, messages in backend.messages
        if task == "domain_feedback" and "도메인: career" in messages[-1]["content"]
    )
    career_user_text = career_message[-1]["content"]
    career_op = conn.execute(
        "SELECT file_id FROM write_op WHERE op_key LIKE 'domain-feedback:career:%'"
    ).fetchone()
    assert "업무 목표에 맞춰" in career_user_text
    assert "가족 일정" not in career_user_text
    assert "추가 사용자 지침" in career_message[0]["content"]
    assert "프로필 관련 태그: #career, #direction" in career_message[0]["content"]
    assert career_op["file_id"] == "career-doc"
    assert first.processed == 2
    assert second.skipped == 2


def test_pipeline_keeps_work_document_out_of_study_ingest(tmp_path):
    class Backend:
        def complete_json(self, task, messages, schema):
            return {"summary": "업무 요약", "feedback": [], "evidence": []}

    fake = FakeDynalist()
    fake.add_doc("work-doc")
    fake.add("work-doc", "root", "업무 목표에 맞춰 결과를 기록했다")
    settings = Settings(db_path=str(tmp_path / "tutorbot.sqlite3"))
    settings.domains["work"].file_ids = ["work-doc"]
    conn = connect(settings.db_path)
    migrate(conn)
    dynalist = DynalistClient("token", transport=fake, sleep=lambda _: None)
    llm = LLMClient(backend=Backend(), sleep=lambda _: None)

    result = run_pipeline(
        conn, settings, dynalist, llm, run_date="2026-11-15",
        lock_path=tmp_path / "tutorbot.lock",
    )

    assert result.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM study_entry").fetchone()[0] == 0
    assert any("🤖 work 피드백" in node["content"] for node in fake.docs["work-doc"].values())
