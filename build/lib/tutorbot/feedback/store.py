"""SQLite persistence for feedback runs, locks, approvals, and outbox state."""
from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from .. import clock
from .models import BotWriteMark, LockResult, SyncResult

log = logging.getLogger(__name__)


@contextmanager
def _transaction(conn, *, immediate=False):
    nested = conn.in_transaction
    name = "fb_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {name}" if nested else ("BEGIN IMMEDIATE" if immediate else "BEGIN"))
    try:
        yield
    except BaseException:
        if nested:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
        else:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute(f"RELEASE {name}" if nested else "COMMIT")


def has_cycle(conn, task_id: str, cycle_key: str) -> bool:
    return conn.execute("SELECT 1 FROM feedback_task_run WHERE task_id=? AND cycle_key=?",
                        (task_id, cycle_key)).fetchone() is not None


def prev_started_at(conn, task_id: str) -> str | None:
    row = conn.execute("SELECT started_at FROM feedback_task_run WHERE task_id=? "
                       "ORDER BY started_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
    return row[0] if row else None


def sweep_interrupted(conn, *, current_run_id: int, now_iso: str) -> int:
    with _transaction(conn, immediate=True):
        rows = conn.execute("SELECT id, task_id FROM feedback_task_run "
                            "WHERE status='started' AND run_id IS NOT ?", (current_run_id,)).fetchall()
        for row in rows:
            conn.execute("UPDATE feedback_task_run SET status='failed', error='interrupted', finished_at=? "
                         "WHERE id=?", (now_iso, row[0]))
            conn.execute("DELETE FROM feedback_task_lock WHERE task_id=?", (row[1],))
        return len(rows)


def _pid_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OverflowError):
        return False
    return False


def try_lock(conn, task_id: str, *, now_iso: str, stale_minutes: int, host: str, pid: int) -> LockResult:
    with _transaction(conn, immediate=True):
        row = conn.execute("SELECT owner_pid, owner_host, acquired_at, heartbeat_at, task_run_id "
                           "FROM feedback_task_lock WHERE task_id=?", (task_id,)).fetchone()
        stolen = ()
        if row:
            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
            heartbeat_at = datetime.fromisoformat(row[3].replace("Z", "+00:00"))
            stale = heartbeat_at < now - timedelta(minutes=stale_minutes)
            if not stale and not (row[1] == host and _pid_dead(row[0])):
                return LockResult(False, owner_pid=row[0], since=row[2])
            if row[4] is not None:
                updated = conn.execute("UPDATE feedback_task_run SET status='failed', error='interrupted', "
                                       "finished_at=? WHERE id=? AND status='started'", (now_iso, row[4]))
                if updated.rowcount:
                    stolen = (row[4],)
            conn.execute("DELETE FROM feedback_task_lock WHERE task_id=?", (task_id,))
            log.warning("feedback_lock_stolen task_id=%s", task_id)
        token = uuid.uuid4().hex
        conn.execute("INSERT INTO feedback_task_lock(task_id,owner_pid,owner_host,owner_token,acquired_at,heartbeat_at) "
                     "VALUES(?,?,?,?,?,?)", (task_id, pid, host, token, now_iso, now_iso))
        return LockResult(True, token=token, stolen_run_ids=stolen)


def heartbeat(conn, task_id: str, token: str, now_iso: str, *, task_run_id: int | None = None) -> bool:
    with _transaction(conn):
        cur = conn.execute("UPDATE feedback_task_lock SET heartbeat_at=?, task_run_id=COALESCE(?,task_run_id) "
                           "WHERE task_id=? AND owner_token=?", (now_iso, task_run_id, task_id, token))
        return cur.rowcount == 1


def release_lock(conn, task_id: str, token: str) -> None:
    with _transaction(conn):
        conn.execute("DELETE FROM feedback_task_lock WHERE task_id=? AND owner_token=?", (task_id, token))


_RUN_COLUMNS = frozenset("""task_id cycle_key trigger_kind schedule scheduled_at started_at finished_at status
    skip_reason error prev_started_at window_lower run_id config_hash persona_hash selected_count units_total
    units_ok units_failed units_skipped_cap input_truncated_units input_over_target_units llm_calls prompt_tokens
    completion_tokens outputs_enqueued""".split())
_OUTPUT_COLUMNS = frozenset("""task_run_id task_id unit_ref target_file_id target_node_id root_op_key status
    input_hash truncated over_target body_json target_own_hash target_effective_modified_ms result_node_id
    error created_at written_at""".split())


def _insert(conn, table, columns, allowed):
    if not columns or not columns.keys() <= allowed:
        raise ValueError("invalid persistence columns")
    keys = list(columns)
    cur = conn.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                       tuple(columns[k] for k in keys))
    return cur.lastrowid


def insert_run(conn, *, consume_approval_id: int | None = None, **cols) -> int | None:
    try:
        with _transaction(conn, immediate=True):
            run_row_id = _insert(conn, "feedback_task_run", cols, _RUN_COLUMNS)
            if consume_approval_id is not None:
                if cols.get("status") != "started":
                    raise ValueError("only a started run can consume approval")
                cur = conn.execute(
                    "UPDATE feedback_persona_approval SET consumed_at=?, consumed_by_run=? "
                    "WHERE id=? AND task_id=? AND persona_hash=? AND kind='once' "
                    "AND consumed_at IS NULL AND revoked_at IS NULL",
                    (clock.now_iso(), run_row_id, consume_approval_id, cols.get("task_id"), cols.get("persona_hash")))
                if cur.rowcount != 1:
                    raise ValueError("once approval unavailable")
            return run_row_id
    except sqlite3.IntegrityError as exc:
        if "feedback_task_run.task_id, feedback_task_run.cycle_key" in str(exc):
            return None
        raise


def update_run(conn, run_row_id: int, **cols) -> None:
    if not cols:
        return
    if not cols.keys() <= _RUN_COLUMNS:
        raise ValueError("invalid run columns")
    with _transaction(conn):
        conn.execute("UPDATE feedback_task_run SET " + ",".join(f"{key}=?" for key in cols) + " WHERE id=?",
                     (*cols.values(), run_row_id))


def insert_output(conn, **cols) -> int:
    with _transaction(conn):
        return _insert(conn, "feedback_output", cols, _OUTPUT_COLUMNS)


def load_bot_write_marks(conn, file_ids: Iterable[str]) -> dict[tuple[str, str], BotWriteMark]:
    ids = list(dict.fromkeys(file_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT target_file_id,target_node_id,target_own_hash,target_effective_modified_ms "
        "FROM feedback_output WHERE id IN (SELECT MAX(id) FROM feedback_output "
        "WHERE status IN ('pending_write','written') AND target_own_hash IS NOT NULL "
        "AND target_effective_modified_ms IS NOT NULL AND target_node_id!='root' "
        f"AND target_file_id IN ({placeholders}) GROUP BY target_file_id,target_node_id)", ids).fetchall()
    return {(r[0], r[1]): BotWriteMark(r[2], r[3]) for r in rows}


def sync_output_status(conn, *, now_iso: str) -> SyncResult:
    written = failed = pending = 0
    late = set()
    with _transaction(conn, immediate=True):
        outputs = conn.execute("SELECT id,task_run_id,root_op_key,status FROM feedback_output "
                               "WHERE status='pending_write'").fetchall()
        for output in outputs:
            key = output[2]
            prefix = key + ":line:"
            ops = conn.execute("SELECT op_key,status,attempts,result_node_id FROM write_op "
                               "WHERE op_key=? OR substr(op_key,1,?)=?",
                               (key, len(prefix), prefix)).fetchall()
            root = next((op for op in ops if op[0] == key), None)
            bad = any(op[1] == "skipped" or (op[1] == "failed" and op[2] >= 5) for op in ops)
            if bad:
                failed += 1
                conn.execute("UPDATE feedback_output SET status='failed',error='write_failed' WHERE id=?",
                             (output[0],))
                cur = conn.execute("UPDATE feedback_task_run SET status='failed',error='write_failed_later',"
                                   "finished_at=? WHERE id=? AND status='succeeded'", (now_iso, output[1]))
                if cur.rowcount:
                    late.add(output[1])
            elif root and all(op[1] == "done" for op in ops):
                written += 1
                conn.execute("UPDATE feedback_output SET status='written',result_node_id=?,"
                             "written_at=COALESCE(written_at,?) WHERE id=?", (root[3], now_iso, output[0]))
            else:
                pending += 1
    return SyncResult(written, failed, pending, tuple(sorted(late)))


def finalize_runs(conn, run_id: int, *, now_iso: str) -> dict[str, int]:
    counts = {"succeeded": 0, "failed": 0}
    with _transaction(conn, immediate=True):
        rows = conn.execute("SELECT id,units_failed FROM feedback_task_run WHERE run_id=? AND status='started'",
                            (run_id,)).fetchall()
        for row in rows:
            bad = row[1] or conn.execute("SELECT 1 FROM feedback_output WHERE task_run_id=? AND status='failed'",
                                         (row[0],)).fetchone()
            state = "failed" if bad else "succeeded"
            conn.execute("UPDATE feedback_task_run SET status=?,error=?,finished_at=? WHERE id=?",
                         (state, "write_failed" if bad else None, now_iso, row[0]))
            counts[state] += 1
    return counts


def find_approval(conn, task_id, persona_hash, kind) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM feedback_persona_approval WHERE task_id=? AND persona_hash=? AND kind=? "
                        "AND revoked_at IS NULL AND (kind='standing' OR consumed_at IS NULL) "
                        "ORDER BY id DESC LIMIT 1", (task_id, persona_hash, kind)).fetchone()


def grant_approval(conn, task_id, persona_path, persona_hash, kind, now_iso) -> int:
    with _transaction(conn, immediate=True):
        row = find_approval(conn, task_id, persona_hash, kind)
        if row:
            return row[0]
        cur = conn.execute("INSERT INTO feedback_persona_approval(task_id,persona_path,persona_hash,kind,granted_at) "
                           "VALUES(?,?,?,?,?)", (task_id, persona_path, persona_hash, kind, now_iso))
        return cur.lastrowid


def revoke_approvals(conn, task_id, now_iso) -> int:
    with _transaction(conn):
        cur = conn.execute("UPDATE feedback_persona_approval SET revoked_at=? WHERE task_id=? "
                           "AND revoked_at IS NULL AND (kind='standing' OR consumed_at IS NULL)",
                           (now_iso, task_id))
        return cur.rowcount


def recent_runs(conn, task_id: str | None, limit: int) -> list[sqlite3.Row]:
    if task_id is None:
        return conn.execute("SELECT * FROM feedback_task_run ORDER BY started_at DESC,id DESC LIMIT ?", (limit,)).fetchall()
    return conn.execute("SELECT * FROM feedback_task_run WHERE task_id=? ORDER BY started_at DESC,id DESC LIMIT ?",
                        (task_id, limit)).fetchall()


def open_readonly(db_path: str | Path) -> sqlite3.Connection | None:
    path = Path(db_path).resolve()
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        log.debug("feedback readonly database unavailable")
        return None


def has_feedback_tables(conn) -> bool:
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                            "('feedback_task_run','feedback_task_lock','feedback_output','feedback_persona_approval')")
        return len(rows.fetchall()) == 4
    except sqlite3.Error:
        return False
