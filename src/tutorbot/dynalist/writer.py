"""Outbox writer + reconcile (spec §8)."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import clock
from .client import (
    MAX_CHANGES_PER_REQUEST,
    RETRY_BACKOFF,
    DynalistClient,
    NodeNotFound,
    RetryableError,
    UnknownOutcome,
    ch_delete,
    ch_edit,
    ch_insert,
)
from .tree import NodeIndex


@dataclass
class WriteReport:
    done: int = 0
    reconciled: int = 0
    skipped: int = 0
    failed: int = 0
    changes_sent: int = 0
    errors: list[str] = field(default_factory=list)


def enqueue_op(
    conn: sqlite3.Connection,
    *,
    run_id: int | None = None,
    op_key: str,
    file_id: str,
    action: str,
    parent_ref: str | None = None,
    target_ref: str | None = None,
    idx: int | None = -1,
    payload: dict[str, Any] | None = None,
    bot_kind: str | None = None,
    bot_ref_id: int | None = None,
    marker: str | None = None,
) -> bool:
    """Insert a write_op if op_key does not already exist. Returns True if newly inserted."""
    row = conn.execute("SELECT id, status FROM write_op WHERE op_key=?", (op_key,)).fetchone()
    if row is not None:
        return False
    seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM write_op").fetchone()
    seq = int(seq_row["s"]) if seq_row else 1
    conn.execute(
        "INSERT INTO write_op(run_id,op_key,file_id,action,parent_ref,target_ref,idx,"
        "payload_json,bot_kind,bot_ref_id,marker,seq,status,attempts,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',0,?)",
        (
            run_id,
            op_key,
            file_id,
            action,
            parent_ref,
            target_ref,
            idx,
            json.dumps(payload or {}, ensure_ascii=False),
            bot_kind,
            bot_ref_id,
            marker,
            seq,
            clock.now_iso(),
        ),
    )
    return True


def _resolve_ref(conn: sqlite3.Connection, ref: str | None) -> tuple[str | None, str]:
    """Resolve 'node:<id>' or 'op:<op_key>' -> (node_id, state) where state is 'ready'|'wait'|'skip'."""
    if not ref:
        return None, "ready"
    if ref.startswith("node:"):
        return ref[5:], "ready"
    if ref.startswith("op:"):
        op_key = ref[3:]
        row = conn.execute("SELECT status, result_node_id FROM write_op WHERE op_key=?", (op_key,)).fetchone()
        if row is None:
            return None, "skip"
        if row["status"] == "done" and row["result_node_id"]:
            return row["result_node_id"], "ready"
        if row["status"] in ("skipped", "failed"):
            return None, "skip"
        return None, "wait"
    return ref, "ready"


def _apply_domain_done(
    conn: sqlite3.Connection,
    op: sqlite3.Row,
    result_node_id: str | None,
    now_iso: str,
) -> None:
    """Update write_op, bot_node, and domain tables in a single transaction."""
    conn.execute(
        "UPDATE write_op SET status='done', result_node_id=?, done_at=?, last_error=NULL WHERE id=?",
        (result_node_id, now_iso, op["id"]),
    )
    payload = json.loads(op["payload_json"])
    kind = op["bot_kind"]
    ref_id = op["bot_ref_id"]
    fid = op["file_id"]

    if op["action"] == "insert" and result_node_id:
        if kind:
            conn.execute(
                "INSERT INTO bot_node(file_id,node_id,kind,ref_id,op_key,created_at,deleted_at)"
                " VALUES(?,?,?,?,?,?,NULL)"
                " ON CONFLICT(file_id,node_id) DO UPDATE SET kind=excluded.kind,"
                " ref_id=excluded.ref_id, op_key=excluded.op_key, deleted_at=NULL",
                (fid, result_node_id, kind, ref_id, op["op_key"], now_iso),
            )
        if kind == "concept" and ref_id is not None:
            conn.execute(
                "UPDATE concept SET node_id=?, rendered_text=?, updated_at=? WHERE id=?",
                (result_node_id, payload.get("content", ""), now_iso, ref_id),
            )
        elif kind == "feedback" and ref_id is not None:
            conn.execute(
                "UPDATE feedback SET node_id=? WHERE entry_id=? AND node_id IS NULL",
                (result_node_id, ref_id),
            )
        elif kind == "quiz_item" and ref_id is not None:
            conn.execute(
                "UPDATE quiz_attempt SET node_id=?, status='open' WHERE id=?",
                (result_node_id, ref_id),
            )
        elif kind == "weekly_report" and ref_id is not None:
            conn.execute(
                "UPDATE weekly_report SET node_id=? WHERE id=?",
                (result_node_id, ref_id),
            )
    elif op["action"] == "edit" and result_node_id:
        if kind == "concept" and ref_id is not None and "content" in payload:
            conn.execute(
                "UPDATE concept SET rendered_text=?, updated_at=? WHERE id=?",
                (payload["content"], now_iso, ref_id),
            )
    elif op["action"] == "delete" and result_node_id:
        conn.execute(
            "UPDATE bot_node SET deleted_at=? WHERE file_id=? AND node_id=?",
            (now_iso, fid, result_node_id),
        )


def _mark_skipped(conn: sqlite3.Connection, op_id: int, reason: str) -> None:
    conn.execute("UPDATE write_op SET status='skipped', last_error=? WHERE id=?", (reason, op_id))


def _reconcile_insert(
    conn: sqlite3.Connection,
    index: NodeIndex,
    op: sqlite3.Row,
    parent_id: str,
) -> str | None:
    """Check if an insert write_op already landed under parent_id (spec §8.3 step 5)."""
    if parent_id not in index:
        return None
    children = index.children_of(parent_id)
    marker = op["marker"]
    payload = json.loads(op["payload_json"])
    want_content = payload.get("content", "")

    if marker:
        for ch in children:
            first_line = ((ch.note or "").lstrip().splitlines() or [""])[0].strip()
            if first_line == marker:
                return ch.id
    else:
        registered = {
            r["node_id"]
            for r in conn.execute(
                "SELECT node_id FROM bot_node WHERE file_id=? AND deleted_at IS NULL",
                (op["file_id"],),
            )
        }
        for ch in children:
            if ch.content == want_content and ch.id not in registered:
                return ch.id
    return None


def _build_change(op: sqlite3.Row, parent_id: str | None, target_id: str | None) -> dict[str, Any]:
    payload = json.loads(op["payload_json"])
    act = op["action"]
    if act == "insert":
        idx = op["idx"] if op["idx"] is not None else -1
        note = payload.get("note", "")
        if op["marker"]:
            if not note:
                note = op["marker"]
            elif not note.lstrip().startswith(op["marker"]):
                note = op["marker"] + "\n" + note
        return ch_insert(
            parent_id or "root",
            payload.get("content", ""),
            note=note,
            index=idx,
            checked=bool(payload.get("checked", False)),
        )
    if act == "edit":
        fields = {k: payload[k] for k in ("content", "note", "checked") if k in payload}
        return ch_edit(target_id or "", **fields)
    if act == "delete":
        return ch_delete(target_id or "")
    raise ValueError(f"Unsupported write_op action: {act}")


def flush_outbox(
    conn: sqlite3.Connection,
    client: DynalistClient,
    indices: dict[str, NodeIndex] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> WriteReport:
    """Execute all pending/failed(attempts<5) write_ops in waves (spec §8.3)."""
    rep = WriteReport()
    indices = dict(indices or {})
    reconcile_needed: set[str] = set()
    aborted_files: set[str] = set()

    # If any op from a previous run has attempts > 0, reconcile that file first
    prev_attempted = conn.execute(
        "SELECT DISTINCT file_id FROM write_op WHERE status IN ('pending','failed') AND attempts > 0 AND attempts < 5"
    ).fetchall()
    for r in prev_attempted:
        reconcile_needed.add(r["file_id"])

    while True:
        ops = conn.execute(
            "SELECT * FROM write_op WHERE status IN ('pending','failed') AND attempts < 5 ORDER BY seq ASC"
        ).fetchall()
        ops = [op for op in ops if op["file_id"] not in aborted_files]
        if not ops:
            break

        # Refresh indices for files needing reconcile or missing
        for r in ops:
            fid = r["file_id"]
            if fid in reconcile_needed or fid not in indices:
                try:
                    indices[fid] = NodeIndex.from_doc_read(fid, client.doc_read(fid))
                    reconcile_needed.discard(fid)
                except Exception as e:
                    rep.errors.append(f"doc_read({fid}): {e}")

        progressed = False
        wave_by_file: dict[str, list[tuple[sqlite3.Row, str | None, str | None]]] = {}

        conn.execute("BEGIN")
        try:
            now_iso = clock.now_iso()
            for op in ops:
                fid = op["file_id"]
                idx_obj = indices.get(fid)
                parent_id, p_state = _resolve_ref(conn, op["parent_ref"])
                target_id, t_state = _resolve_ref(conn, op["target_ref"])

                if p_state == "skip" or t_state == "skip":
                    _mark_skipped(conn, op["id"], "dependency skipped/failed")
                    rep.skipped += 1
                    progressed = True
                    continue
                if p_state == "wait" or t_state == "wait":
                    continue

                # Check if parent or target exists in the known NodeIndex
                if idx_obj is not None:
                    if op["action"] == "insert" and parent_id and parent_id not in idx_obj:
                        _mark_skipped(conn, op["id"], f"parent {parent_id} not in document")
                        rep.skipped += 1
                        progressed = True
                        continue
                    if op["action"] in ("edit", "delete") and target_id and target_id not in idx_obj:
                        _mark_skipped(conn, op["id"], f"target {target_id} not in document")
                        rep.skipped += 1
                        progressed = True
                        continue

                # Reconcile insert before sending ONLY if op was previously attempted (crash recovery)
                if op["action"] == "insert" and op["attempts"] > 0 and idx_obj is not None and parent_id:
                    existing_nid = _reconcile_insert(conn, idx_obj, op, parent_id)
                    if existing_nid is not None:
                        _apply_domain_done(conn, op, existing_nid, now_iso)
                        rep.reconciled += 1
                        rep.done += 1
                        progressed = True
                        continue

                wave_by_file.setdefault(fid, []).append((op, parent_id, target_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        if not wave_by_file:
            if not progressed:
                break
            continue

        for fid, ready_list in wave_by_file.items():
            for i in range(0, len(ready_list), MAX_CHANGES_PER_REQUEST):
                chunk = ready_list[i : i + MAX_CHANGES_PER_REQUEST]
                changes = [_build_change(op, pid, tid) for op, pid, tid in chunk]
                op_ids = [op["id"] for op, _, _ in chunk]

                conn.execute("BEGIN")
                for oid in op_ids:
                    conn.execute("UPDATE write_op SET attempts = attempts + 1 WHERE id=?", (oid,))
                conn.execute("COMMIT")

                try:
                    new_ids = client.doc_edit(fid, changes)
                    rep.changes_sent += len(changes)
                except UnknownOutcome as e:
                    conn.execute("BEGIN")
                    for oid in op_ids:
                        conn.execute("UPDATE write_op SET status='failed', last_error=? WHERE id=?", (str(e), oid))
                    conn.execute("COMMIT")
                    rep.errors.append(str(e))
                    aborted_files.add(fid)
                    break
                except NodeNotFound as e:
                    # A node vanished right before doc_edit; refresh index and let per-op check skip missing ones
                    reconcile_needed.add(fid)
                    conn.execute("BEGIN")
                    for oid in op_ids:
                        conn.execute("UPDATE write_op SET status='failed', last_error=? WHERE id=?", (str(e), oid))
                    conn.execute("COMMIT")
                    progressed = True
                    break
                except RetryableError as e:
                    conn.execute("BEGIN")
                    for oid in op_ids:
                        conn.execute(
                            "UPDATE write_op SET status='failed', last_error=? WHERE id=?",
                            (str(e), oid),
                        )
                    conn.execute("COMMIT")
                    rep.errors.append(str(e))
                    progressed = True
                    break
                except Exception as e:
                    conn.execute("BEGIN")
                    for oid in op_ids:
                        conn.execute(
                            "UPDATE write_op SET status='failed', attempts=5, last_error=? WHERE id=?",
                            (str(e), oid),
                        )
                    conn.execute("COMMIT")
                    rep.failed += len(op_ids)
                    rep.errors.append(str(e))
                    progressed = True
                    break

                # Success: map new_ids in insert order and update DB + in-memory NodeIndex
                now_iso = clock.now_iso()
                new_iter = iter(new_ids)
                conn.execute("BEGIN")
                try:
                    for (op, pid, tid), ch in zip(chunk, changes):
                        if op["action"] == "insert":
                            res_id = next(new_iter, None)
                        else:
                            res_id = tid
                        _apply_domain_done(conn, op, res_id, now_iso)
                        rep.done += 1
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

                # Refresh in-memory index so subsequent waves see newly inserted nodes
                try:
                    indices[fid] = NodeIndex.from_doc_read(fid, client.doc_read(fid))
                except Exception:
                    pass
                progressed = True

        # Count any ops that reached attempts >= 5 and are still failed
        if not progressed:
            break

    exhausted = conn.execute(
        "SELECT COUNT(*) FROM write_op WHERE status='failed' AND attempts >= 5"
    ).fetchone()[0]
    rep.failed = max(rep.failed, int(exhausted))
    return rep
