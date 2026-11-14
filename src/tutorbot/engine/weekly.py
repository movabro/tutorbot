"""Stage 12: Weekly report (T5) and learner profile rolling summary (T6) (spec D7, §3.4, §6.2, §8.2, §9.2)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from .. import clock, render
from ..dynalist.writer import enqueue_op
from ..llm.client import LLMClient, LLMError, load_prompt
from ..llm.schemas import T5WeeklyOut, T6ProfileOut
from .goals import _compute_leaf_counts
from .progress import _recommend_next_concepts

PROMPT_T5 = "t5_weekly:v1"
PROMPT_T6 = "t6_profile:v1"


def previous_iso_week(run_date: str) -> tuple[str, str, str]:
    """Return (iso_week_str, week_start_date, week_end_date) for the ISO week (Mon..Sun) immediately preceding `run_date`'s week."""
    d = date.fromisoformat(run_date)
    cur_monday = d - timedelta(days=d.weekday())
    prev_monday = cur_monday - timedelta(days=7)
    prev_sunday = cur_monday - timedelta(days=1)
    iso_y, iso_w, _ = prev_monday.isocalendar()
    return f"{iso_y}-W{iso_w:02d}", prev_monday.isoformat(), prev_sunday.isoformat()


def _ensure_weekly_root(
    conn: sqlite3.Connection,
    goal: sqlite3.Row,
    run_id: int | None,
    bot_tag: str,
) -> str:
    gid = int(goal["id"])
    fid = goal["file_id"]
    row = conn.execute(
        "SELECT node_id FROM bot_node WHERE file_id=? AND kind='weekly_root' AND ref_id=? AND deleted_at IS NULL",
        (fid, gid),
    ).fetchone()
    if row and row["node_id"]:
        return f"node:{row['node_id']}"

    pending = conn.execute(
        "SELECT op_key FROM write_op WHERE op_key LIKE ? AND status IN ('pending','failed') ORDER BY seq DESC LIMIT 1",
        (f"weekly_root:{gid}:%",),
    ).fetchone()
    if pending:
        return f"op:{pending['op_key']}"

    gen_row = conn.execute(
        "SELECT COUNT(*) AS c FROM write_op WHERE op_key LIKE ?",
        (f"weekly_root:{gid}:%",),
    ).fetchone()
    gen = (int(gen_row["c"]) if gen_row else 0) + 1
    op_key = f"weekly_root:{gid}:{gen}"
    enqueue_op(
        conn,
        run_id=run_id,
        op_key=op_key,
        file_id=fid,
        action="insert",
        parent_ref=f"node:{goal['node_id']}",
        idx=3,
        payload={"content": render.weekly_root(bot_tag)},
        bot_kind="weekly_root",
        bot_ref_id=gid,
        marker=render.marker("weekly_root", gid),
    )
    return f"op:{op_key}"


def _gather_weekly_stats(
    conn: sqlite3.Connection,
    goal_id: int,
    week_start: str,
    week_end: str,
) -> dict[str, Any]:
    """Gather SQL-aggregated stats for T5 (no raw study text)."""
    concepts = conn.execute(
        "SELECT * FROM concept WHERE goal_id=? AND user_deleted=0 ORDER BY depth ASC, sort_order ASC, id ASC",
        (goal_id,),
    ).fetchall()
    pct, _ = _compute_leaf_counts(concepts)

    study_count = conn.execute(
        "SELECT COUNT(*) FROM study_entry WHERE goal_id=? AND status='processed' AND entry_date BETWEEN ? AND ?",
        (goal_id, week_start, week_end),
    ).fetchone()[0]

    transitions_count = conn.execute(
        """
        SELECT COUNT(*) FROM concept_state_event cse
        JOIN concept c ON cse.concept_id = c.id
        WHERE c.goal_id=? AND substr(cse.created_at, 1, 10) BETWEEN ? AND ?
        """,
        (goal_id, week_start, week_end),
    ).fetchone()[0]

    quiz_rows = conn.execute(
        "SELECT score FROM quiz_attempt WHERE goal_id=? AND status='graded' AND issued_date BETWEEN ? AND ?",
        (goal_id, week_start, week_end),
    ).fetchall()
    if quiz_rows:
        passed = sum(1 for r in quiz_rows if (r["score"] or 0) >= 3)
        quiz_acc_pct = round(passed / len(quiz_rows) * 100)
    else:
        quiz_acc_pct = 0

    weak_rows = conn.execute(
        """
        SELECT c.name, MAX(qc.lapses) AS max_lapses, c.state
        FROM concept c
        LEFT JOIN quiz_card qc ON qc.concept_id = c.id
        WHERE c.goal_id=? AND c.is_leaf=1 AND c.user_deleted=0
          AND (c.state='partial' OR COALESCE(qc.lapses, 0) > 0)
        GROUP BY c.id
        ORDER BY max_lapses DESC, c.sort_order ASC
        LIMIT 5
        """,
        (goal_id,),
    ).fetchall()
    weak_concepts = [r["name"] for r in weak_rows]
    next_candidates = _recommend_next_concepts(conn, goal_id, limit=5)

    return {
        "week_range": f"{week_start}..{week_end}",
        "progress_pct": pct,
        "study_count": int(study_count),
        "state_transitions": int(transitions_count),
        "quiz_graded_count": len(quiz_rows),
        "quiz_accuracy_pct": quiz_acc_pct,
        "weak_concepts_top5": weak_concepts,
        "next_recommended_candidates": next_candidates,
    }


def _collect_recent_feedback_notes(
    conn: sqlite3.Connection,
    goal_id: int,
    week_start: str,
    week_end: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT f.body_json
        FROM feedback f
        JOIN study_entry se ON f.entry_id = se.id
        WHERE se.goal_id = ? AND se.entry_date BETWEEN ? AND ?
        ORDER BY f.id DESC LIMIT 20
        """,
        (goal_id, week_start, week_end),
    ).fetchall()
    notes: list[dict[str, Any]] = []
    for r in rows:
        try:
            d = json.loads(r["body_json"])
            corr = d.get("corrections") or []
            miss = d.get("missing") or []
            if corr or miss:
                notes.append({"corrections": corr, "missing": miss})
        except ValueError:
            continue
    return notes


def process_weekly_reports(
    conn: sqlite3.Connection,
    llm: LLMClient,
    run_date: str,
    run_id: int | None = None,
    bot_tag: str = "tutorbot",
    daily_token_budget: int = 300000,
) -> dict[str, int]:
    """Stage 12: Generate weekly report (T5) and update learner_profile (T6) for the previous ISO week
    if not yet generated (Monday or catch-up, spec D7, §6.2, §9.2).
    """
    stats = {"reports_created": 0, "profiles_updated": 0, "errors": 0}
    iso_week, week_start, week_end = previous_iso_week(run_date)

    goals = conn.execute("SELECT * FROM goal WHERE status='active' ORDER BY id ASC").fetchall()
    for g in goals:
        gid = int(g["id"])
        fid = g["file_id"]

        # Skip if weekly_report for (goal_id, iso_week) already exists
        exists = conn.execute(
            "SELECT 1 FROM weekly_report WHERE goal_id=? AND iso_week=?",
            (gid, iso_week),
        ).fetchone()
        if exists:
            continue

        # Only generate if run_date is Monday OR we missed Monday's run (a previous run_log exists on or before week_end)
        is_monday = date.fromisoformat(run_date).weekday() == 0
        had_past_run = conn.execute(
            "SELECT 1 FROM run_log WHERE run_date <= ? AND status IN ('ok','partial') LIMIT 1",
            (week_end,),
        ).fetchone()
        if not is_monday and not had_past_run:
            continue

        if (llm.stats["prompt_tokens"] + llm.stats["completion_tokens"]) >= daily_token_budget:
            continue

        agg = _gather_weekly_stats(conn, gid, week_start, week_end)
        sys_t5 = load_prompt("t5_weekly")
        msg_t5 = [
            {"role": "system", "content": sys_t5},
            {"role": "user", "content": json.dumps({"iso_week": iso_week, **agg}, ensure_ascii=False)},
        ]
        try:
            out_t5: T5WeeklyOut = llm.complete_json(
                PROMPT_T5,
                msg_t5,
                validator=T5WeeklyOut.validate,
                ref=f"weekly:{gid}:{iso_week}",
            )
        except LLMError:
            stats["errors"] += 1
            continue

        # Optional T6 learner profile update if there were feedback notes
        fb_notes = _collect_recent_feedback_notes(conn, gid, week_start, week_end)
        new_profile_summary: str | None = None
        if fb_notes and (llm.stats["prompt_tokens"] + llm.stats["completion_tokens"]) < daily_token_budget:
            cur_prof = conn.execute("SELECT summary FROM learner_profile WHERE goal_id=?", (gid,)).fetchone()
            old_sum = cur_prof["summary"] if cur_prof else ""
            sys_t6 = load_prompt("t6_profile")
            msg_t6 = [
                {"role": "system", "content": sys_t6},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"previous_summary": old_sum, "recent_feedback": fb_notes},
                        ensure_ascii=False,
                    ),
                },
            ]
            try:
                out_t6: T6ProfileOut = llm.complete_json(
                    PROMPT_T6,
                    msg_t6,
                    validator=T6ProfileOut.validate,
                    ref=f"profile:{gid}:{iso_week}",
                )
                new_profile_summary = out_t6.summary
            except LLMError:
                stats["errors"] += 1

        now_iso = clock.now_iso()
        body_json = json.dumps(
            {
                "stats": agg,
                "summary": out_t5.summary,
                "weak_points": out_t5.weak_points,
                "next_week": out_t5.next_week,
            },
            ensure_ascii=False,
        )

        conn.execute("BEGIN")
        try:
            cur = conn.execute(
                "INSERT INTO weekly_report(goal_id,iso_week,body_json,created_at) VALUES(?,?,?,?)",
                (gid, iso_week, body_json, now_iso),
            )
            wr_id = int(cur.lastrowid)

            if new_profile_summary is not None:
                conn.execute(
                    "INSERT INTO learner_profile(goal_id,summary,updated_at) VALUES(?,?,?)"
                    " ON CONFLICT(goal_id) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
                    (gid, new_profile_summary, now_iso),
                )
                stats["profiles_updated"] += 1

            w_root_ref = _ensure_weekly_root(conn, g, run_id, bot_tag)
            rep_op_key = f"weekly:{gid}:{iso_week}"
            header_txt = render.weekly_report_header(
                iso_week,
                agg["progress_pct"],
                agg["study_count"],
                agg["quiz_accuracy_pct"],
            )
            enqueue_op(
                conn,
                run_id=run_id,
                op_key=rep_op_key,
                file_id=fid,
                action="insert",
                parent_ref=w_root_ref,
                idx=0,  # newest weekly report at top (spec §3.4)
                payload={"content": header_txt},
                bot_kind="weekly_report",
                bot_ref_id=wr_id,
                marker=render.marker("weekly_report", wr_id),
            )

            child_lines: list[str] = [f"📝 요약: {out_t5.summary}"]
            if out_t5.weak_points:
                child_lines.append(f"⚠️ 약점: {', '.join(out_t5.weak_points)}")
            if out_t5.next_week:
                child_lines.append(f"➡️ 다음 주 추천: {', '.join(out_t5.next_week)}")

            for idx_line, line in enumerate(child_lines):
                enqueue_op(
                    conn,
                    run_id=run_id,
                    op_key=f"{rep_op_key}:line:{idx_line}",
                    file_id=fid,
                    action="insert",
                    parent_ref=f"op:{rep_op_key}",
                    idx=-1,
                    payload={"content": line},
                    bot_kind="weekly_report_item",
                    bot_ref_id=wr_id,
                )

            conn.execute("COMMIT")
            stats["reports_created"] += 1
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return stats
