from __future__ import annotations

import sys
from pathlib import Path
import logging
import json

import pytest

from tutorbot.config import Settings, _parse_toml_bytes, load_settings
from tutorbot.db import connect, migrate
from tutorbot.dynalist.tree import NodeIndex
from datetime import datetime
from zoneinfo import ZoneInfo

from tutorbot.engine.domains import _life_incremental_text, _personal_tasks, process_domains
from tutorbot.llm.client import LLMClient
from tutorbot.parse.personal_tasks import parse_tracked_tasks
from tutorbot.dynalist.client import DynalistClient
from tutorbot.pipeline import run_pipeline
from tests.fakes import FakeDynalist


def test_source_read_failure_logs_failed_source_and_target(caplog):
    settings = Settings()
    settings.domains["work"].file_ids = ["work-target"]
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-15','now','running')"
    ).lastrowid

    with caplog.at_level(logging.WARNING, logger="tutorbot.engine.domains"):
        process_domains(conn, {}, settings, object(), run_id=run_id)

    assert "failed_source_ids=['work']" in caplog.text
    assert "target_id=work" in caplog.text
    assert "domain analysis succeeded" not in caplog.text


def test_toml_decode_error_is_not_chained_to_parser_fallback():
    if sys.version_info < (3, 11):
        pytest.importorskip("tomli")

    with pytest.raises(ValueError) as error:
        _parse_toml_bytes(b"invalid = [unquoted]")

    assert error.value.__context__ is None


def _index(fake: FakeDynalist, file_id: str) -> NodeIndex:
    return NodeIndex.from_doc_read(file_id, fake._doc_read({"file_id": file_id}))


def test_domain_settings_read_study_documents_from_study_profile(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[dynalist]\ntoken_env = "DYNALIST_TOKEN"\n'
        '[domains.study]\nenabled = true\nfile_ids = ["study-doc"]\n'
        '[domains.work]\nfile_ids = ["work-doc"]\ntags = ["project"]\n'
        '[domains.career]\nfile_ids = ["career-doc"]\n'
        '[domains.personal_tasks]\ntracking_tag_aliases = ["Task"]\n'
        '[domains.devlog]\nenabled = true\nfile_ids = ["devlog-doc"]\n',
        encoding="utf-8",
    )
    settings = load_settings(path, dotenv_path=path.with_suffix(".env"))

    assert settings.domains["study"].file_ids == ["study-doc"]
    assert settings.domains["work"].file_ids == ["work-doc"]
    assert settings.domains["work"].tags == ["project"]
    assert settings.domains["career"].source_domains == ["work", "life"]
    assert settings.domains["personal_tasks"].tracking_tag == "Track"
    assert settings.domains["personal_tasks"].tracking_tag_aliases == ["Task"]
    assert settings.domains["devlog"].enabled
    assert settings.domains["devlog"].file_ids == ["devlog-doc"]


def test_legacy_dynalist_file_ids_are_not_supported(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[dynalist]\nfile_ids = ["legacy-doc"]\n', encoding="utf-8")
    monkeypatch.setenv("DYNALIST_FILE_ID", "environment-doc")

    with pytest.raises(TypeError):
        load_settings(path, dotenv_path=path.with_suffix(".env"))

    monkeypatch.delenv("DYNALIST_FILE_ID")
    settings = load_settings(None, dotenv_path=path.with_suffix(".env"))
    assert not hasattr(settings.dynalist, "file_ids")
    assert settings.domains["study"].file_ids == []


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
    second = process_domains(conn, indices, settings, llm, run_id=run_id, run_date="2026-11-15")
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


def test_career_uses_only_configured_sources_and_writes_its_own_document(caplog):
    class Backend:
        def __init__(self):
            self.messages = []

        def complete_json(self, task, messages, schema):
            self.messages.append((task, messages))
            return {"summary": "요약", "feedback": ["피드백"], "evidence": ["기록"]}

    fake = FakeDynalist()
    fake.add_doc("work-doc")
    fake.add_doc("life-doc")
    fake.add_doc("life-doc-2")
    fake.add_doc("career-doc")
    fake.add("work-doc", "root", "업무 목표에 맞춰 보고 자동화를 진행했다")
    recent = int(datetime(2026, 11, 14, 15, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)
    fake.add("life-doc", "root", "가족 일정에 참석했다", created=recent, modified=recent)
    fake.add("life-doc-2", "root", "가족과 산책했다", created=recent, modified=recent)
    fake.add("career-doc", "root", "커리어 방향 관찰")
    indices = {fid: _index(fake, fid) for fid in fake.docs}

    settings = Settings()
    settings.domains["work"].file_ids = ["work-doc"]
    settings.domains["life"].file_ids = ["life-doc", "life-doc-2"]
    settings.domains["career"].file_ids = ["career-doc"]
    settings.domains["career"].source_domains = ["work", "life"]
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

    with caplog.at_level("INFO", logger="tutorbot.engine.domains"):
        first = process_domains(conn, indices, settings, llm, run_id=run_id, run_date="2026-11-15")
    second = process_domains(conn, indices, settings, llm, run_id=run_id, run_date="2026-11-15")

    career_message = next(
        messages for task, messages in backend.messages
        if task == "domain_feedback" and "도메인: career" in messages[-1]["content"]
    )
    career_user_text = career_message[-1]["content"]
    career_op = conn.execute(
        "SELECT file_id FROM write_op WHERE op_key LIKE 'domain-feedback:career:%'"
    ).fetchone()
    assert "업무 목표에 맞춰" in career_user_text
    assert "가족 일정에 참석했다" in career_user_text
    assert "추가 사용자 지침" in career_message[0]["content"]
    assert "프로필 관련 태그: #career, #direction" in career_message[0]["content"]
    assert career_op["file_id"] == "career-doc"
    payload = conn.execute("SELECT payload_json FROM write_op WHERE file_id='career-doc'").fetchone()
    assert "!(2026-11-15)" in payload["payload_json"]
    assert "source_ids=['work', 'life']" in caplog.text
    assert "target_id=career" in caplog.text
    assert "domain analysis succeeded" in caplog.text
    assert (
        "life source domain_id=career source_id=life target_id=career "
        "cutoff=2026-11-14T00:00:00+09:00 changed_nodes=1 result=succeeded"
    ) in caplog.text
    assert caplog.text.count(
        "life source domain_id=career source_id=life target_id=career "
    ) == 2
    assert "work-doc" not in caplog.text
    assert "life-doc" not in caplog.text
    assert "career-doc" not in caplog.text
    assert "업무 목표에 맞춰 보고 자동화를 진행했다" not in caplog.text
    assert "가족 일정에 참석했다" not in caplog.text
    assert "프로필 관련 태그" not in caplog.text
    assert "private source record" not in caplog.text
    assert "요약" not in caplog.text
    assert first.processed == 4
    assert second.skipped == 4


@pytest.mark.parametrize(
    ("source_domain", "source_file", "source_label"),
    [
        ("work", "private-work-file", "work"),
        ("life", "private-life-file", "life"),
    ],
)
def test_career_source_labels_match_incremental_policy_without_file_ids_in_logs(
    caplog, source_domain, source_file, source_label,
):
    class Backend:
        def __init__(self):
            self.messages = []

        def complete_json(self, task, messages, schema):
            self.messages.append((task, messages))
            return {"summary": "요약", "feedback": ["피드백"], "evidence": []}

    fake = FakeDynalist()
    fake.add_doc(source_file)
    fake.add_doc("private-career-file")
    if source_domain == "life":
        recent = int(
            datetime(2026, 11, 14, 15, tzinfo=ZoneInfo("UTC")).timestamp() * 1000
        )
        fake.add(source_file, "root", "최근 생활 기록", created=recent, modified=recent)
    else:
        fake.add(source_file, "root", "업무 기록")
    fake.add("private-career-file", "root", "오래된 커리어 관찰")

    settings = Settings()
    settings.domains[source_domain].file_ids = [source_file]
    settings.domains["career"].file_ids = ["private-career-file"]
    settings.domains["career"].source_domains = [source_domain]
    indices = {file_id: _index(fake, file_id) for file_id in fake.docs}
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-15','now','running')"
    ).lastrowid
    backend = Backend()

    with caplog.at_level(logging.INFO, logger="tutorbot.engine.domains"):
        process_domains(
            conn,
            indices,
            settings,
            LLMClient(backend=backend, conn=conn, run_id=run_id),
            run_id=run_id,
            run_date="2026-11-15",
        )

    career_message = next(
        messages for task, messages in backend.messages
        if task == "domain_feedback" and "도메인: career" in messages[-1]["content"]
    )
    if source_domain == "work":
        assert "업무 기록" in career_message[-1]["content"]
    else:
        assert "최근 생활 기록" in career_message[-1]["content"]

    domain_logs = [
        record.getMessage()
        for record in caplog.records
        if "domain analysis" in record.getMessage() or "life source" in record.getMessage()
    ]
    assert any(
        f"domain_id=career source_ids=['{source_label}'] target_id=career" in message
        for message in domain_logs
    )
    assert any(
        "life source domain_id=career source_id=life target_id=career" in message
        for message in domain_logs
    ) == (source_domain == "life")
    assert all(
        source_file not in message and "private-career-file" not in message
        for message in domain_logs
    )


def test_career_uses_work_when_life_has_no_recent_changes_and_warns(caplog):
    class Backend:
        def __init__(self):
            self.messages = []

        def complete_json(self, task, messages, schema):
            self.messages.append((task, messages))
            return {"summary": "요약", "feedback": [], "evidence": []}

    fake = FakeDynalist()
    for file_id in ("work-doc", "life-doc", "career-doc"):
        fake.add_doc(file_id)
    fake.add("work-doc", "root", "최근 업무 사용자 기록")
    old = int(datetime(2026, 11, 18, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    fake.add("life-doc", "root", "오래된 생활 기록", created=old, modified=old)

    settings = Settings()
    settings.domains["work"].file_ids = ["work-doc"]
    settings.domains["life"].file_ids = ["life-doc"]
    settings.domains["career"].file_ids = ["career-doc"]
    settings.domains["career"].source_domains = ["work", "life"]
    indices = {fid: _index(fake, fid) for fid in fake.docs}
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-20','now','running')"
    ).lastrowid
    backend = Backend()

    with caplog.at_level(logging.INFO, logger="tutorbot.engine.domains"):
        report = process_domains(
            conn, indices, settings, LLMClient(backend=backend, conn=conn, run_id=run_id),
            run_id=run_id, run_date="2026-11-20",
        )

    career_message = next(
        messages for task, messages in backend.messages
        if task == "domain_feedback" and "도메인: career" in messages[-1]["content"]
    )
    career_input = career_message[-1]["content"].split("기록:\n", 1)[1]
    assert "최근 업무 사용자 기록" in career_input
    assert "오래된 생활 기록" not in career_input
    assert any("career/life-doc: no life changes" in warning for warning in report.warnings)
    assert (
        "life source domain_id=career source_id=life target_id=career "
        "cutoff=2026-11-19T00:00:00+09:00 changed_nodes=0 result=skipped"
    ) in caplog.text
    assert "domain analysis succeeded domain_id=career" in caplog.text
    row = conn.execute(
        "SELECT status,error FROM domain_run WHERE domain_id='career'"
    ).fetchone()
    assert row["status"] == "warning"
    assert "no life changes" in row["error"]


def test_old_unrelated_life_record_is_absent_from_input_and_has_no_source_hash(caplog):
    class NoCallBackend:
        def complete_json(self, task, messages, schema):
            raise AssertionError("an old unrelated life record must not call Gemini")

    fake = FakeDynalist()
    fake.add_doc("life")
    old = int(datetime(2026, 11, 18, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    fake.add("life", "root", "오래된 비관련 기록", created=old, modified=old)
    settings = Settings()
    settings.domains["life"].file_ids = ["life"]
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-20','now','running')"
    ).lastrowid

    with caplog.at_level(logging.INFO, logger="tutorbot.engine.domains"):
        report = process_domains(
            conn, {"life": _index(fake, "life")}, settings,
            LLMClient(backend=NoCallBackend()), run_id=run_id, run_date="2026-11-20",
        )

    assert report.skipped == 1
    assert "오래된 비관련 기록" not in str(report)
    assert (
        "life source domain_id=life source_id=life target_id=life "
        "cutoff=2026-11-19T00:00:00+09:00 changed_nodes=0 result=skipped"
    ) in caplog.text
    assert (
        "domain analysis skipped domain_id=life source_ids=[] target_id=life "
        "reason=no_analyzable_user_records"
    ) in caplog.text
    row = conn.execute(
        "SELECT status,source_hash,error FROM domain_run WHERE domain_id='life'"
    ).fetchone()
    assert row["status"] == "warning"
    assert row["source_hash"] is None
    assert "no life changes" in row["error"]


def test_life_incremental_text_includes_context_and_changed_subtree_only():
    fake = FakeDynalist()
    fake.add_doc("life")
    old = int(datetime(2026, 11, 17, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)
    recent = int(datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    parent = fake.add("life", "root", "일상", created=old, modified=old)
    fake.add("life", parent, "오래된 형제", created=old, modified=old)
    changed = fake.add("life", parent, "최근 기록", created=recent, modified=old)
    fake.add("life", changed, "하위 내용", created=old, modified=old)
    cutoff = datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul"))

    text = _life_incremental_text(_index(fake, "life"), lambda _: False, cutoff)

    assert "일상" in text
    assert "최근 기록" in text
    assert "하위 내용" in text
    assert "오래된 형제" not in text


def test_life_cutoff_is_inclusive_and_modified_timestamp_counts():
    fake = FakeDynalist()
    fake.add_doc("life")
    cutoff_ms = int(datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    before_ms = cutoff_ms - 1
    fake.add("life", "root", "경계 포함", created=cutoff_ms, modified=0)
    fake.add("life", "root", "modified 변경", created=before_ms, modified=cutoff_ms)
    fake.add("life", "root", "경계 직전", created=before_ms, modified=before_ms)

    text = _life_incremental_text(
        _index(fake, "life"), lambda _: False,
        datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    assert "경계 포함" in text
    assert "modified 변경" in text
    assert "경계 직전" not in text


def test_life_nested_changes_render_minimal_context_once():
    fake = FakeDynalist()
    fake.add_doc("life")
    recent = int(datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    old = recent - 1000
    parent = fake.add("life", "root", "부모", created=recent, modified=old)
    child = fake.add("life", parent, "자녀", created=recent, modified=old)
    fake.add("life", child, "자녀 상세", created=old, modified=old)
    text = _life_incremental_text(
        _index(fake, "life"), lambda _: False,
        datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    assert text.count("부모") == 1
    assert sum(line.strip() == "자녀" for line in text.splitlines()) == 1
    assert text.count("자녀 상세") == 1


def test_empty_life_increment_skips_without_gemini_or_empty_hash(caplog):
    class NoCallBackend:
        def complete_json(self, task, messages, schema):
            raise AssertionError("empty life input must not call Gemini")

    fake = FakeDynalist()
    fake.add_doc("life")
    fake.add("life", "root", "오래된 기록", created=1, modified=1)
    settings = Settings()
    settings.domains["life"].file_ids = ["life"]
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-20','now','running')"
    ).lastrowid
    caplog.set_level(logging.INFO, logger="tutorbot.engine.domains")
    report = process_domains(
        conn, {"life": _index(fake, "life")}, settings,
        LLMClient(backend=NoCallBackend()), run_id=run_id, run_date="2026-11-20",
    )
    assert "target_id=pending" not in caplog.text
    assert "source_id=life" in caplog.text
    assert "cutoff=" in caplog.text
    assert "changed_nodes=0" in caplog.text
    row = conn.execute(
        "SELECT status,source_hash,error FROM domain_run WHERE domain_id='life'"
    ).fetchone()
    assert report.skipped == 1
    assert report.warnings
    assert row["status"] == "warning"
    assert row["source_hash"] is None
    assert "no life changes" in row["error"]
    assert conn.execute("SELECT COUNT(*) FROM domain_state WHERE domain_id='life'").fetchone()[0] == 0


def test_life_source_hash_matches_incremental_prompt_text():
    class Backend:
        def __init__(self):
            self.messages = []
        def complete_json(self, task, messages, schema):
            self.messages.append(messages)
            return {"summary": "ok", "feedback": [], "evidence": []}

    fake = FakeDynalist()
    fake.add_doc("life")
    recent = int(datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    fake.add("life", "root", "오늘 기록", created=recent, modified=recent)
    settings = Settings()
    settings.domains["life"].file_ids = ["life"]
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-20','now','running')"
    ).lastrowid
    backend = Backend()
    process_domains(
        conn, {"life": _index(fake, "life")}, settings,
        LLMClient(backend=backend), run_id=run_id, run_date="2026-11-20",
    )
    prompt_text = backend.messages[0][-1]["content"].split("기록:\n", 1)[1]
    saved = conn.execute(
        "SELECT source_hash FROM domain_state WHERE domain_id='life'"
    ).fetchone()["source_hash"]
    import hashlib
    assert saved == "v1:" + hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


def test_life_incremental_text_empty_when_no_recent_nodes():
    fake = FakeDynalist()
    fake.add_doc("life")
    old = int(datetime(2026, 11, 18, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    fake.add("life", "root", "오래된 기록", created=old, modified=old)

    assert _life_incremental_text(
        _index(fake, "life"), lambda _: False,
        datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul")),
    ) == ""


def test_life_incremental_text_excludes_bot_feedback_subtree():
    fake = FakeDynalist()
    fake.add_doc("life")
    recent = int(datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    bot = fake.add("life", "root", "봇 피드백", created=recent, modified=recent)
    fake.add("life", bot, "봇 하위", created=recent, modified=recent)
    user = fake.add("life", "root", "사용자 기록", created=recent, modified=recent)
    index = _index(fake, "life")

    text = _life_incremental_text(
        index, lambda node: node.id == bot,
        datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    assert "사용자 기록" in text
    assert "봇 피드백" not in text
    assert "봇 하위" not in text


def test_work_and_life_create_feedback_under_each_recent_changed_node():
    class Backend:
        def complete_json(self, task, messages, schema):
            return {"summary": "짧은 요약", "feedback": ["구체적 근거를 남긴다"], "evidence": ["사용자 기록"]}

    fake = FakeDynalist()
    for file_id in ("work", "life"):
        fake.add_doc(file_id)
    recent = int(datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    work_a = fake.add("work", "root", "업무 기록 A", created=recent, modified=recent)
    work_b = fake.add("work", "root", "업무 기록 B", created=recent, modified=recent)
    life_a = fake.add("life", "root", "생활 기록 A", created=recent, modified=recent)
    life_b = fake.add("life", "root", "생활 기록 B", created=recent, modified=recent)

    settings = Settings()
    settings.domains["work"].file_ids = ["work"]
    settings.domains["life"].file_ids = ["life"]
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-20','now','running')"
    ).lastrowid
    process_domains(
        conn, {"work": _index(fake, "work"), "life": _index(fake, "life")},
        settings, LLMClient(backend=Backend()), run_id=run_id, run_date="2026-11-20",
    )

    work_ops = conn.execute(
        "SELECT parent_ref,payload_json FROM write_op WHERE file_id='work'"
    ).fetchall()
    life_ops = conn.execute(
        "SELECT parent_ref,payload_json FROM write_op WHERE file_id='life'"
    ).fetchall()
    assert {row["parent_ref"] for row in work_ops} == {f"node:{work_a}", f"node:{work_b}"}
    assert {row["parent_ref"] for row in life_ops} == {f"node:{life_a}", f"node:{life_b}"}
    for row in [*work_ops, *life_ops]:
        payload = json.loads(row["payload_json"])
        assert "짧은 요약" in payload["content"]
        assert "!(" in payload["content"]
        assert "구체적 근거를 남긴다" in payload["note"]


def test_career_direct_recent_nodes_else_root_feedback():
    class Backend:
        def complete_json(self, task, messages, schema):
            return {"summary": "커리어 요약", "feedback": ["다음 행동"], "evidence": ["근거"]}

    fake = FakeDynalist()
    fake.add_doc("career")
    fake.add_doc("work")
    fake.add("work", "root", "업무 근거")
    old = int(datetime(2026, 11, 18, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    recent = int(datetime(2026, 11, 19, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    direct_a = fake.add("career", "root", "최근 커리어 노드 A", created=recent, modified=recent)
    direct_b = fake.add("career", "root", "최근 커리어 노드 B", created=recent, modified=recent)
    fake.add("career", direct_a, "자식도 최근이지만 직접 노드만 대상", created=recent, modified=recent)

    settings = Settings()
    settings.domains["work"].file_ids = ["work"]
    settings.domains["career"].file_ids = ["career"]
    settings.domains["career"].source_domains = ["work"]
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-20','now','running')"
    ).lastrowid
    process_domains(
        conn, {"career": _index(fake, "career"), "work": _index(fake, "work")}, settings,
        LLMClient(backend=Backend()), run_id=run_id, run_date="2026-11-20",
    )
    rows = conn.execute("SELECT parent_ref,payload_json FROM write_op WHERE file_id='career'").fetchall()
    assert {row["parent_ref"] for row in rows} == {f"node:{direct_a}", f"node:{direct_b}"}

    # Without direct recent nodes, the career root remains the fallback parent.
    fake2 = FakeDynalist()
    fake2.add_doc("career")
    fake2.add("career", "root", "오래된 커리어 기록", created=old, modified=old)
    conn2 = connect(":memory:")
    migrate(conn2)
    run_id2 = conn2.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-20','now','running')"
    ).lastrowid
    process_domains(
        conn2, {"career": _index(fake2, "career")}, Settings(),
        LLMClient(backend=Backend()), run_id=run_id2, run_date="2026-11-20",
    )


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


def test_devlog_feedback_is_per_recent_user_comment_and_idempotent():
    class Backend:
        def __init__(self):
            self.messages = []

        def complete_json(self, task, messages, schema):
            self.messages.append(messages)
            return {
                "summary": "구체적인 다음 행동",
                "feedback": ["측정 기준을 기록하세요."],
                "evidence": ["현재 코멘트에 기준이 없습니다."],
            }

    fake = FakeDynalist()
    fake.add_doc("devlog")
    recent = int(datetime(2026, 11, 19, 10, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    old = int(datetime(2026, 11, 18, 10, tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)
    comment = fake.add(
        "devlog", "root", "배포 자동화를 개선한다", "이번 주에 먼저 검증",
        created=recent, modified=recent,
    )
    fake.add("devlog", "root", "오래된 의견", created=old, modified=old)
    bot = fake.add(
        "devlog", "root", "자동 로그", "tb:devlog_log_time:devlog:2026-11-20:10:00:00",
        created=recent, modified=recent,
    )
    fake.add("devlog", bot, "자동 로그 자식", created=recent, modified=recent)
    settings = Settings()
    settings.domains["devlog"].file_ids = ["devlog"]
    conn = connect(":memory:")
    migrate(conn)
    run_id = conn.execute(
        "INSERT INTO run_log(run_date,started_at,status) VALUES('2026-11-20','now','running')"
    ).lastrowid
    backend = Backend()
    llm = LLMClient(backend=backend)
    indices = {"devlog": _index(fake, "devlog")}

    first = process_domains(
        conn, indices, settings, llm, run_id=run_id, run_date="2026-11-20",
    )
    second = process_domains(
        conn, indices, settings, llm, run_id=run_id, run_date="2026-11-20",
    )

    assert first.processed == 1
    assert second.skipped == 1
    assert len(backend.messages) == 1
    assert "배포 자동화를 개선한다" in backend.messages[0][-1]["content"]
    assert "오래된 의견" not in backend.messages[0][-1]["content"]
    assert "자동 로그" not in backend.messages[0][-1]["content"]
    row = conn.execute(
        "SELECT parent_ref,payload_json,bot_kind,marker FROM write_op"
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert row["parent_ref"] == f"node:{comment}"
    assert row["bot_kind"] == "domain_feedback"
    assert row["marker"].startswith(f"tb:domain_feedback:devlog:{comment}:")
    assert payload["content"] == "구체적인 다음 행동 !(2026-11-20) #tutorbot"
    assert "측정 기준을 기록하세요." in payload["note"]
    assert "근거: 현재 코멘트에 기준이 없습니다." in payload["note"]
