import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.test_feedback_foundations import task
from tests.test_feedback_store_focused import NOW, insert_run
from tutorbot import db
from tutorbot.feedback import output, store
from tutorbot.feedback.models import NodeRef, OutputSpec, UnitSpec
from tutorbot.feedback.schedule import decide, first_run_lower_bound, next_due
from tutorbot.feedback.timefilter import resolve_time_bounds

SEOUL = ZoneInfo("Asia/Seoul")


@pytest.mark.parametrize(("stamp", "key", "scheduled"), [
    ("2026-11-24T07:59", "2026-11-23@20:00", "2026-11-23T20:00"),
    ("2026-11-24T08:00", "2026-11-24@08:00", "2026-11-24T08:00"),
    ("2026-11-24T10:59", "2026-11-24@08:00", "2026-11-24T08:00"),
    ("2026-11-24T11:00", "2026-11-24@11:00", "2026-11-24T11:00"),
    ("2026-11-24T14:00", "2026-11-24@14:00", "2026-11-24T14:00"),
    ("2026-11-24T17:00", "2026-11-24@17:00", "2026-11-24T17:00"),
    ("2026-11-24T20:00", "2026-11-24@20:00", "2026-11-24T20:00"),
    ("2026-11-25T00:00", "2026-11-24@20:00", "2026-11-24T20:00"),
])
def test_work_slots(stamp, key, scheduled):
    spec = task(schedule="work_slots")
    assert spec.valid
    now = datetime.fromisoformat(stamp).replace(tzinfo=SEOUL)
    decision = decide(spec, now, lambda _: False)
    assert (decision.action, decision.cycle_key) == ("run", key)
    assert decision.scheduled_at.astimezone(SEOUL) == datetime.fromisoformat(scheduled).replace(tzinfo=SEOUL)
    assert decide(spec, now, lambda _: True).action == "already_done"
    assert first_run_lower_bound(spec, now) == now - timedelta(hours=3)
    assert next_due(spec, now, lambda _: True) > now


def test_week_end_and_validation():
    spec = task(schedule="weekly", schedule_week_end=True)
    assert spec.valid
    sunday = datetime(2026, 11, 29, 23, 59, 59, tzinfo=SEOUL)
    monday = sunday + timedelta(seconds=1)
    assert decide(spec, sunday, lambda _: False).cycle_key == "2026-W47"
    assert decide(spec, monday, lambda _: False).cycle_key == "2026-W48"
    assert decide(spec, monday, lambda _: False).scheduled_at == monday.astimezone(timezone.utc)
    assert decide(spec, datetime(2027, 1, 4, tzinfo=SEOUL), lambda _: False).cycle_key == "2026-W53"
    assert not task(schedule="weekly", schedule_week_end=True, schedule_weekday="sunday").valid
    assert not task(schedule="weekly", schedule_week_end=True, schedule_time="00:00").valid
    assert not task(schedule="daily", schedule_week_end=False).valid
    assert not task(schedule="work_slots", timezone="UTC").valid
    assert not task(schedule="work_slots", schedule_time="00:00").valid


def test_anchor_and_complete_write():
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        anchor = "2026-11-23T00:00:00Z"
        assert store.success_window_lower(conn, "test", "v1:config", anchor) == anchor
        assert store.success_window_lower(conn, "test", "v1:config", NOW) == anchor
        assert store.success_window_lower(conn, "test", "other", NOW) == NOW
        rid = insert_run(conn, status="succeeded", units_total=1, units_ok=1)
        assert store.success_window_lower(conn, "test", "v1:config", NOW) == anchor
        ref = NodeRef("doc", "root")
        output.enqueue_result(conn, task=task(), task_run_id=rid, run_id=None,
                              unit=UnitSpec("agg", (), (ref,)), write_to=ref,
                              out={"summary": "ok", "feedback": ["line"], "evidence": []},
                              local_date="2026-11-24", bot_tag="tutorbot", truncated=False,
                              over_target=False, snapshot=None)
        store.update_run(conn, rid, outputs_enqueued=1)
        assert store.success_window_lower(conn, "test", "v1:config", NOW) == anchor
        conn.execute("UPDATE write_op SET status='done'")
        store.sync_output_status(conn, now_iso=NOW)
        assert store.success_window_lower(conn, "test", "v1:config", NOW) == anchor
        conn.execute("UPDATE write_op SET result_node_id='synthetic-' || id")
        store.sync_output_status(conn, now_iso=NOW)
        assert store.success_window_lower(conn, "test", "v1:config", NOW) == NOW
        conn.execute("UPDATE write_op SET status='failed' WHERE op_key LIKE '%:line:0'")
        assert store.success_window_lower(conn, "test", "v1:config", NOW) == anchor
    finally:
        conn.close()


def test_success_filter_requires_explicit_anchor():
    now = datetime(2026, 11, 24, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        resolve_time_bounds("created_since_last_success", now, "UTC", None, task())
    bounds = resolve_time_bounds("modified_since_last_success", now, "UTC", None, task(),
                                 success_lower=now + timedelta(seconds=1))
    assert bounds.lower_ms > bounds.upper_ms


def test_career_note_is_single_atomic_op():
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        rid = insert_run(conn)
        spec = replace(task(), id="career-daily", domain="career", unit="aggregate",
                       output=OutputSpec("under_parent", "doc", "root", "career_note"))
        ref = NodeRef("doc", "root")
        kwargs = dict(task=spec, task_run_id=rid, run_id=None, unit=UnitSpec("agg", (), (ref,)),
                      write_to=ref, local_date="2026-11-24", bot_tag="tutorbot", truncated=False,
                      over_target=False, snapshot=None)
        with pytest.raises(ValueError):
            output.enqueue_result(conn, out={"summary": "title", "feedback": [], "evidence": []}, **kwargs)
        assert conn.execute("SELECT COUNT(*) FROM feedback_output").fetchone()[0] == 0
        output.enqueue_result(conn, out={"summary": "title\n#tag", "feedback": ["detail !(x)"],
                                         "evidence": []}, **kwargs)
        op = conn.execute("SELECT * FROM write_op").fetchall()
        assert len(op) == 1
        payload = json.loads(op[0]["payload_json"])
        assert payload["content"].endswith("!(2026-11-24) #tutorbot")
        assert "\n" not in payload["content"]
        assert "！(x)" in payload["note"] and "근거 없음" in payload["note"]
        assert op[0]["marker"]
    finally:
        conn.close()
