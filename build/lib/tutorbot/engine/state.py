"""Concept state transition engine (spec §7.4).

LLM only *suggests* states; all actual transitions are decided here and recorded in `concept_state_event`.
"""
from __future__ import annotations

import sqlite3

from .. import clock

STATE_RANK = {
    "unknown": 0,
    "partial": 1,
    "understood": 2,
    "mastered": 3,
}


def _record_transition(
    conn: sqlite3.Connection,
    concept_id: int,
    old_state: str,
    new_state: str,
    source: str,
    ref_id: int | None,
    reason: str | None,
    run_id: int | None,
) -> bool:
    if old_state == new_state:
        return False
    now_iso = clock.now_iso()
    conn.execute(
        "UPDATE concept SET state=?, state_reason=?, updated_at=? WHERE id=?",
        (new_state, reason, now_iso, concept_id),
    )
    conn.execute(
        "INSERT INTO concept_state_event(concept_id,old_state,new_state,source,ref_id,reason,run_id,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (concept_id, old_state, new_state, source, ref_id, reason, run_id, now_iso),
    )
    return True


def apply_dump_transition(
    conn: sqlite3.Connection,
    concept_id: int,
    suggested_state: str,
    dump_id: int,
    evidence: str | None = None,
    run_id: int | None = None,
) -> bool:
    """Spec §7.4 dump rule:
    - Can raise state to `partial` or `understood` (never `mastered`).
    - Can lower state ONLY if the concept has NO non-dump (`study`/`quiz`/`srs`) events.
    """
    if suggested_state not in ("unknown", "partial", "understood"):
        return False
    row = conn.execute("SELECT state, is_leaf, user_deleted FROM concept WHERE id=?", (concept_id,)).fetchone()
    if row is None or not row["is_leaf"] or row["user_deleted"]:
        return False
    old_state = row["state"]
    old_r = STATE_RANK.get(old_state, 0)
    new_r = STATE_RANK.get(suggested_state, 0)

    if new_r > old_r:
        return _record_transition(conn, concept_id, old_state, suggested_state, "dump", dump_id, evidence, run_id)
    if new_r < old_r:
        has_other = conn.execute(
            "SELECT 1 FROM concept_state_event WHERE concept_id=? AND source != 'dump' LIMIT 1",
            (concept_id,),
        ).fetchone()
        if not has_other and old_state != "mastered":
            return _record_transition(conn, concept_id, old_state, suggested_state, "dump", dump_id, evidence, run_id)
    return False


def apply_study_transition(
    conn: sqlite3.Connection,
    concept_id: int,
    coverage: str,
    state_suggestion: str | None,
    entry_id: int,
    reason: str | None = None,
    run_id: int | None = None,
) -> bool:
    """Spec §7.4 study rule:
    - Only raises state (never lowers, never sets mastered).
    - coverage='mention' -> no state raise.
    - coverage='partial' -> max target is 'partial'.
    - coverage='full' -> 'understood' if state_suggestion == 'understood', else 'partial'.
    """
    row = conn.execute("SELECT state, is_leaf, user_deleted FROM concept WHERE id=?", (concept_id,)).fetchone()
    if row is None or not row["is_leaf"] or row["user_deleted"]:
        return False
    old_state = row["state"]
    old_r = STATE_RANK.get(old_state, 0)

    if coverage == "mention":
        return False
    if coverage == "partial":
        target = "partial"
    elif coverage == "full":
        target = "understood" if state_suggestion == "understood" else "partial"
    else:
        return False

    if STATE_RANK[target] > old_r:
        return _record_transition(
            conn,
            concept_id,
            old_state,
            target,
            "study",
            entry_id,
            reason or f"study coverage={coverage}",
            run_id,
        )
    return False


def apply_quiz_transition(
    conn: sqlite3.Connection,
    concept_id: int,
    score: int,
    attempt_id: int,
    interval_days: int,
    run_id: int | None = None,
) -> bool:
    """Spec §7.4 quiz & srs rules:
    - quiz (score <= 2): lowers understood/mastered -> partial.
    - srs (interval_days >= 21 and score >= 4): raises to mastered.
    """
    row = conn.execute("SELECT state, is_leaf, user_deleted FROM concept WHERE id=?", (concept_id,)).fetchone()
    if row is None or not row["is_leaf"] or row["user_deleted"]:
        return False
    old_state = row["state"]

    if score <= 2 and old_state in ("understood", "mastered"):
        return _record_transition(
            conn,
            concept_id,
            old_state,
            "partial",
            "quiz",
            attempt_id,
            f"quiz score={score}<=2",
            run_id,
        )
    if interval_days >= 21 and score >= 4 and old_state != "mastered":
        return _record_transition(
            conn,
            concept_id,
            old_state,
            "mastered",
            "srs",
            attempt_id,
            f"srs interval={interval_days}>=21, score={score}",
            run_id,
        )
    return False
