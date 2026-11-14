"""Stages 8, 9, 10: Quiz grading (T4), expiration, card selection, and SRS (SM-2) (spec D1, §3.4, §3.5, §7.2, §7.3, §8.2, §9.2)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from .. import clock, render
from ..dynalist.tree import NodeIndex
from ..dynalist.writer import enqueue_op
from ..llm.client import LLMClient, LLMError, load_prompt
from ..llm.schemas import T4GradeOut
from ..parse.classify import is_skip_answer
from .state import apply_quiz_transition

PROMPT_VERSION = "t4_grade:v1"
GRADE_BATCH_SIZE = 20


def _add_days(iso_date: str, days: int) -> str:
    return (date.fromisoformat(iso_date) + timedelta(days=days)).isoformat()


def sm2_update(
    ease: float,
    interval_days: int,
    repetitions: int,
    lapses: int,
    q: int,
    run_date: str,
) -> tuple[float, int, int, int, str]:
    """Spec §7.2 SM-2 variant:
    Returns (new_ease, new_interval, new_repetitions, new_lapses, new_due_date).
    """
    if q < 3:
        repetitions = 0
        interval = 1
        lapses += 1
    else:
        repetitions += 1
        if repetitions == 1:
            interval = 1
        elif repetitions == 2:
            interval = 6
        else:
            interval = round(interval_days * ease)
    new_ease = max(1.3, ease + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)))
    due_date = _add_days(run_date, interval)
    return new_ease, interval, repetitions, lapses, due_date


def _apply_graded_attempt(
    conn: sqlite3.Connection,
    att: sqlite3.Row,
    score: int,
    comment: str,
    run_date: str,
    run_id: int | None,
) -> None:
    """Apply SRS update, concept state transition (§7.4), and enqueue grade write_ops (§8.2)."""
    aid = int(att["id"])
    cid = int(att["card_id"])
    card = conn.execute("SELECT * FROM quiz_card WHERE id=?", (cid,)).fetchone()
    if card is None:
        return

    new_ease, new_interval, new_reps, new_lapses, new_due = sm2_update(
        float(card["ease"]),
        int(card["interval_days"]),
        int(card["repetitions"]),
        int(card["lapses"]),
        score,
        run_date,
    )
    now_iso = clock.now_iso()

    conn.execute(
        "UPDATE quiz_card SET ease=?, interval_days=?, repetitions=?, lapses=?, due_date=?,"
        " consecutive_expired=0 WHERE id=?",
        (new_ease, new_interval, new_reps, new_lapses, new_due, cid),
    )
    conn.execute(
        "UPDATE quiz_attempt SET status='graded', score=?, grade_comment=?, graded_at=?, run_id=? WHERE id=?",
        (score, comment, now_iso, run_id, aid),
    )
    apply_quiz_transition(
        conn,
        int(card["concept_id"]),
        score=score,
        attempt_id=aid,
        interval_days=new_interval,
        run_id=run_id,
    )

    if att["node_id"]:
        enqueue_op(
            conn,
            run_id=run_id,
            op_key=f"grade:{aid}",
            file_id=att["file_id"],
            action="insert",
            parent_ref=f"node:{att['node_id']}",
            idx=-1,
            payload={"content": render.grade_node(score, comment, card["answer_key"])},
            bot_kind="grade",
            bot_ref_id=aid,
            marker=render.marker("grade", aid),
        )
        enqueue_op(
            conn,
            run_id=run_id,
            op_key=f"grade_check:{aid}",
            file_id=att["file_id"],
            action="edit",
            target_ref=f"node:{att['node_id']}",
            payload={"checked": True},
            bot_kind="quiz_item",
            bot_ref_id=aid,
        )


def grade_open_attempts(
    conn: sqlite3.Connection,
    llm: LLMClient,
    settings: Any,
    run_date: str,
    run_id: int | None = None,
) -> dict[str, int]:
    """Stage 8: Grade open attempts that have a non-empty answer_text (spec D1, §6.2, §9.2 T4)."""
    stats = {"fast_zero": 0, "llm_graded": 0, "errors": 0}
    quiz_cfg = getattr(settings, "quiz", None)
    skip_answers = list(getattr(quiz_cfg, "skip_answers", ["?", "모름", "pass", "패스"]))

    open_answered = conn.execute(
        "SELECT * FROM quiz_attempt WHERE status='open' AND answer_text IS NOT NULL AND trim(answer_text) != ''"
        " ORDER BY goal_id ASC, id ASC"
    ).fetchall()
    if not open_answered:
        return stats

    to_llm_by_goal: dict[int, list[sqlite3.Row]] = {}
    for att in open_answered:
        ans = att["answer_text"] or ""
        if is_skip_answer(ans, skip_answers):
            conn.execute("BEGIN")
            try:
                _apply_graded_attempt(conn, att, 0, "패스 (0점 처리)", run_date, run_id)
                conn.execute("COMMIT")
                stats["fast_zero"] += 1
            except Exception:
                conn.execute("ROLLBACK")
                raise
        else:
            to_llm_by_goal.setdefault(int(att["goal_id"]), []).append(att)

    sys_prompt = load_prompt("t4_grade")
    for gid, att_list in to_llm_by_goal.items():
        for i in range(0, len(att_list), GRADE_BATCH_SIZE):
            chunk = att_list[i : i + GRADE_BATCH_SIZE]
            items_in = []
            att_by_id: dict[int, sqlite3.Row] = {}
            for att in chunk:
                aid = int(att["id"])
                card = conn.execute("SELECT question, answer_key FROM quiz_card WHERE id=?", (att["card_id"],)).fetchone()
                if card is None:
                    continue
                att_by_id[aid] = att
                items_in.append(
                    {
                        "attempt_id": aid,
                        "question": card["question"],
                        "answer_key": card["answer_key"],
                        "user_answer": att["answer_text"],
                    }
                )
            if not items_in:
                continue

            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(items_in, ensure_ascii=False)},
            ]
            try:
                out: T4GradeOut = llm.complete_json(
                    PROMPT_VERSION,
                    messages,
                    validator=T4GradeOut.validate,
                    ref=f"grade_goal:{gid}",
                )
            except LLMError:
                stats["errors"] += 1
                continue

            conn.execute("BEGIN")
            try:
                for g_item in out.items:
                    att = att_by_id.get(g_item.attempt_id)
                    if att is None:
                        continue
                    _apply_graded_attempt(conn, att, g_item.score, g_item.comment, run_date, run_id)
                    stats["llm_graded"] += 1
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    return stats


def expire_attempts(
    conn: sqlite3.Connection,
    indices: dict[str, NodeIndex],
    run_date: str,
    run_id: int | None = None,
) -> dict[str, int]:
    """Stage 9: Expire open attempts where expire_date < run_date (spec D1, §6.2, §8.2)."""
    stats = {"expired": 0, "postponed_cards": 0}
    rows = conn.execute(
        "SELECT * FROM quiz_attempt WHERE status='open' AND expire_date < ? ORDER BY id ASC",
        (run_date,),
    ).fetchall()
    if not rows:
        return stats

    conn.execute("BEGIN")
    try:
        for att in rows:
            aid = int(att["id"])
            cid = int(att["card_id"])
            fid = att["file_id"]
            nid = att["node_id"]

            conn.execute("UPDATE quiz_attempt SET status='expired', run_id=? WHERE id=?", (run_id, aid))
            stats["expired"] += 1

            card = conn.execute("SELECT question, consecutive_expired, due_date FROM quiz_card WHERE id=?", (cid,)).fetchone()
            if card is not None:
                consec = int(card["consecutive_expired"]) + 1
                if consec >= 3:
                    # Spec D1: if the same card expires 3 times in a row, push due_date to run_date + 7 days
                    base_date = max(run_date, card["due_date"])
                    new_due = _add_days(base_date, 7)
                    conn.execute(
                        "UPDATE quiz_card SET consecutive_expired=?, due_date=? WHERE id=?",
                        (consec, new_due, cid),
                    )
                    stats["postponed_cards"] += 1
                else:
                    conn.execute("UPDATE quiz_card SET consecutive_expired=? WHERE id=?", (consec, cid))

            if nid:
                idx_obj = indices.get(fid)
                existing_txt = (
                    idx_obj.get(nid).content
                    if (idx_obj and nid in idx_obj)
                    else render.quiz_item(aid, card["question"] if card else "")
                )
                enqueue_op(
                    conn,
                    run_id=run_id,
                    op_key=f"expire:{aid}",
                    file_id=fid,
                    action="edit",
                    target_ref=f"node:{nid}",
                    payload={"content": render.expire_title(existing_txt), "checked": True},
                    bot_kind="quiz_item",
                    bot_ref_id=aid,
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return stats


def select_cards_for_goal(
    conn: sqlite3.Connection,
    goal_id: int,
    run_date: str,
    per_day: int = 5,
    max_new_per_day: int = 2,
) -> list[sqlite3.Row]:
    """Spec §7.3 Card Selection (per goal):
    1. slots = per_day - (open attempt count for this goal). If <= 0, return [].
    2. Candidates: status='active', due_date <= run_date, no open attempt, concept.user_deleted=0.
    3. Review cards: repetitions > 0 OR has any quiz_attempt history. Ordered by due_date ASC, lapses DESC, id ASC.
    4. New cards: repetitions == 0 AND no quiz_attempt history. Capped at max_new_per_day.
    5. At most 1 card per concept_id per day.
    """
    open_cnt = conn.execute(
        "SELECT COUNT(*) FROM quiz_attempt WHERE goal_id=? AND status IN ('open','pending_write')",
        (goal_id,),
    ).fetchone()[0]
    slots = per_day - int(open_cnt)
    if slots <= 0:
        return []

    # Concepts that already have an open/pending_write attempt or were already issued on run_date
    used_concepts = {
        int(r["concept_id"])
        for r in conn.execute(
            "SELECT c.concept_id FROM quiz_attempt a JOIN quiz_card c ON a.card_id=c.id"
            " WHERE a.goal_id=? AND (a.status IN ('open','pending_write') OR a.issued_date=?)",
            (goal_id, run_date),
        )
    }

    rows = conn.execute(
        """
        SELECT qc.*,
               EXISTS(SELECT 1 FROM quiz_attempt qa WHERE qa.card_id = qc.id) AS has_history
        FROM quiz_card qc
        JOIN concept c ON qc.concept_id = c.id
        WHERE qc.goal_id = ?
          AND qc.status = 'active'
          AND qc.due_date <= ?
          AND c.user_deleted = 0
          AND NOT EXISTS (
              SELECT 1 FROM quiz_attempt qa
              WHERE qa.card_id = qc.id AND qa.status IN ('open', 'pending_write')
          )
        ORDER BY qc.due_date ASC, qc.lapses DESC, qc.id ASC
        """,
        (goal_id, run_date),
    ).fetchall()

    reviews = [r for r in rows if r["repetitions"] > 0 or r["has_history"]]
    new_cards = [r for r in rows if r["repetitions"] == 0 and not r["has_history"]]

    chosen: list[sqlite3.Row] = []
    for r in reviews:
        if len(chosen) >= slots:
            break
        cid = int(r["concept_id"])
        if cid in used_concepts:
            continue
        used_concepts.add(cid)
        chosen.append(r)

    new_taken = 0
    for r in new_cards:
        if len(chosen) >= slots or new_taken >= max_new_per_day:
            break
        cid = int(r["concept_id"])
        if cid in used_concepts:
            continue
        used_concepts.add(cid)
        chosen.append(r)
        new_taken += 1

    return chosen


def _ensure_quiz_root(
    conn: sqlite3.Connection,
    goal: sqlite3.Row,
    run_id: int | None,
    bot_tag: str,
) -> str:
    """Return parent_ref ('node:<id>' or 'op:<op_key>') for the goal's quiz_root container."""
    gid = int(goal["id"])
    fid = goal["file_id"]
    row = conn.execute(
        "SELECT node_id FROM bot_node WHERE file_id=? AND kind='quiz_root' AND ref_id=? AND deleted_at IS NULL",
        (fid, gid),
    ).fetchone()
    if row and row["node_id"]:
        return f"node:{row['node_id']}"

    pending = conn.execute(
        "SELECT op_key FROM write_op WHERE op_key LIKE ? AND status IN ('pending','failed') ORDER BY seq DESC LIMIT 1",
        (f"quiz_root:{gid}:%",),
    ).fetchone()
    if pending:
        return f"op:{pending['op_key']}"

    gen_row = conn.execute(
        "SELECT COUNT(*) AS c FROM write_op WHERE op_key LIKE ?",
        (f"quiz_root:{gid}:%",),
    ).fetchone()
    gen = (int(gen_row["c"]) if gen_row else 0) + 1
    op_key = f"quiz_root:{gid}:{gen}"
    enqueue_op(
        conn,
        run_id=run_id,
        op_key=op_key,
        file_id=fid,
        action="insert",
        parent_ref=f"node:{goal['node_id']}",
        idx=2,
        payload={"content": render.quiz_root(bot_tag)},
        bot_kind="quiz_root",
        bot_ref_id=gid,
        marker=render.marker("quiz_root", gid),
    )
    return f"op:{op_key}"


def issue_daily_quizzes(
    conn: sqlite3.Connection,
    settings: Any,
    run_date: str,
    run_id: int | None = None,
) -> dict[str, int]:
    """Stage 10: Issue daily quizzes for active goals (spec §6.2, §7.3, §8.2)."""
    stats = {"issued": 0, "skipped_already_issued": 0}
    quiz_cfg = getattr(settings, "quiz", None)
    per_day = int(getattr(quiz_cfg, "per_day", 5))
    max_new = int(getattr(quiz_cfg, "max_new_per_day", 2))
    expire_days = int(getattr(quiz_cfg, "expire_days", 3))
    bot_tag = getattr(getattr(settings, "tags", None), "bot", "tutorbot")

    goals = conn.execute("SELECT * FROM goal WHERE status='active' ORDER BY id ASC").fetchall()
    expire_date = _add_days(run_date, expire_days)

    for g in goals:
        gid = int(g["id"])
        fid = g["file_id"]

        # Spec §6.2 step 10: If already issued for (goal, run_date), skip
        already = conn.execute(
            "SELECT 1 FROM quiz_attempt WHERE goal_id=? AND issued_date=? LIMIT 1",
            (gid, run_date),
        ).fetchone()
        if already:
            stats["skipped_already_issued"] += 1
            continue

        selected = select_cards_for_goal(conn, gid, run_date, per_day=per_day, max_new_per_day=max_new)
        if not selected:
            continue

        conn.execute("BEGIN")
        try:
            quiz_root_ref = _ensure_quiz_root(conn, g, run_id, bot_tag)
            day_op_key = f"quiz_day:{gid}:{run_date}"
            enqueue_op(
                conn,
                run_id=run_id,
                op_key=day_op_key,
                file_id=fid,
                action="insert",
                parent_ref=quiz_root_ref,
                idx=0,  # newest day at top (spec §3.4)
                payload={"content": render.quiz_day(run_date)},
                bot_kind="quiz_day",
                bot_ref_id=gid,
            )

            for card in selected:
                cur = conn.execute(
                    "INSERT INTO quiz_attempt(card_id,goal_id,issued_date,expire_date,file_id,status,run_id)"
                    " VALUES(?,?,?,?,?,'pending_write',?)",
                    (card["id"], gid, run_date, expire_date, fid, run_id),
                )
                aid = int(cur.lastrowid)
                enqueue_op(
                    conn,
                    run_id=run_id,
                    op_key=f"quiz_item:{aid}",
                    file_id=fid,
                    action="insert",
                    parent_ref=f"op:{day_op_key}",
                    idx=-1,
                    payload={"content": render.quiz_item(aid, card["question"])},
                    bot_kind="quiz_item",
                    bot_ref_id=aid,
                )
                stats["issued"] += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return stats
