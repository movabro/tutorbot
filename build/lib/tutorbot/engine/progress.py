"""Stage 11: Progress update (concept node rendering, curriculum_root title, progress_report) (spec §6.2, §7.1, §8.2)."""
from __future__ import annotations

import hashlib
import sqlite3

from .. import render
from ..dynalist.tree import NodeIndex
from ..dynalist.writer import enqueue_op
from .goals import _compute_leaf_counts


def _sha8(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _recommend_next_concepts(conn: sqlite3.Connection, goal_id: int, limit: int = 5) -> list[str]:
    """Recommend up to `limit` leaf concepts that are unknown/partial and whose prereqs are understood/mastered."""
    concepts = conn.execute(
        "SELECT * FROM concept WHERE goal_id=? AND user_deleted=0 ORDER BY depth ASC, sort_order ASC, id ASC",
        (goal_id,),
    ).fetchall()
    state_by_id = {c["id"]: c["state"] for c in concepts}
    prereqs_by_id: dict[int, list[int]] = {}
    for r in conn.execute(
        "SELECT concept_id, prereq_id FROM concept_prereq WHERE concept_id IN (SELECT id FROM concept WHERE goal_id=?)",
        (goal_id,),
    ):
        prereqs_by_id.setdefault(r["concept_id"], []).append(r["prereq_id"])

    ready: list[str] = []
    fallback: list[str] = []
    for c in concepts:
        if not c["is_leaf"] or c["state"] in ("understood", "mastered"):
            continue
        pqs = prereqs_by_id.get(c["id"], [])
        if all(state_by_id.get(p) in ("understood", "mastered") for p in pqs):
            ready.append(c["name"])
        else:
            fallback.append(c["name"])
        if len(ready) >= limit:
            break
    return (ready + fallback)[:limit]


def update_progress(
    conn: sqlite3.Connection,
    indices: dict[str, NodeIndex],
    run_date: str,
    run_id: int | None = None,
    bot_tag: str = "tutorbot",
) -> dict[str, int]:
    """Enqueue edit ops for any concept or curriculum_root whose rendered text changed, plus progress_report."""
    stats = {"concept_edits": 0, "root_edits": 0, "reports": 0}
    goals = conn.execute("SELECT * FROM goal WHERE status='active' ORDER BY id ASC").fetchall()

    for g in goals:
        gid = g["id"]
        fid = g["file_id"]
        idx_obj = indices.get(fid)
        concepts = conn.execute(
            "SELECT * FROM concept WHERE goal_id=? AND user_deleted=0 ORDER BY depth ASC, sort_order ASC, id ASC",
            (gid,),
        ).fetchall()
        if not concepts:
            continue

        pct, branch_counts = _compute_leaf_counts(concepts)
        conn.execute("BEGIN")
        try:
            # 1) Concept node edits (only if node_id is already created and rendered_text differs)
            for c in concepts:
                cid = c["id"]
                nid = c["node_id"]
                if not nid:
                    continue
                if c["is_leaf"]:
                    new_txt = render.concept_leaf(c["name"], c["state"])
                else:
                    done_l, tot_l = branch_counts.get(cid, (0, 0))
                    new_txt = render.concept_branch(c["name"], done_l, tot_l)

                # Also check if user edited the node content in Dynalist (D5: bot overwrites content)
                current_in_doc = idx_obj.get(nid).content if (idx_obj and nid in idx_obj) else c["rendered_text"]
                if c["rendered_text"] != new_txt or current_in_doc != new_txt:
                    op_key = f"concept_edit:{cid}:{_sha8(new_txt)}"
                    # If user edited the node in Dynalist back to a state whose op_key was already done, re-open it
                    if current_in_doc != new_txt:
                        conn.execute(
                            "UPDATE write_op SET status='pending', attempts=0 WHERE op_key=? AND status='done'",
                            (op_key,),
                        )
                    if enqueue_op(
                        conn,
                        run_id=run_id,
                        op_key=op_key,
                        file_id=fid,
                        action="edit",
                        target_ref=f"node:{nid}",
                        payload={"content": new_txt},
                        bot_kind="concept",
                        bot_ref_id=cid,
                    ):
                        stats["concept_edits"] += 1

            # 2) Curriculum root title update (only if content changed)
            curr_row = conn.execute(
                "SELECT node_id FROM bot_node WHERE file_id=? AND kind='curriculum_root' AND ref_id=? AND deleted_at IS NULL",
                (fid, gid),
            ).fetchone()
            if curr_row and curr_row["node_id"]:
                root_nid = curr_row["node_id"]
                want_root_txt = render.curriculum_root(pct, bot_tag)
                cur_root_txt = idx_obj.get(root_nid).content if (idx_obj and root_nid in idx_obj) else None
                if cur_root_txt is not None and cur_root_txt != want_root_txt:
                    op_key = f"curr_root_edit:{gid}:{_sha8(want_root_txt)}"
                    if enqueue_op(
                        conn,
                        run_id=run_id,
                        op_key=op_key,
                        file_id=fid,
                        action="edit",
                        target_ref=f"node:{root_nid}",
                        payload={"content": want_root_txt},
                        bot_kind="curriculum_root",
                        bot_ref_id=gid,
                    ):
                        stats["root_edits"] += 1

            # 3) Progress report container (index 1 under goal): create if missing
            rep_row = conn.execute(
                "SELECT node_id FROM bot_node WHERE file_id=? AND kind='progress_report' AND ref_id=? AND deleted_at IS NULL",
                (fid, gid),
            ).fetchone()
            has_pending_rep = conn.execute(
                "SELECT 1 FROM write_op WHERE op_key LIKE ? AND status IN ('pending','failed')",
                (f"prog_report:{gid}:%",),
            ).fetchone()
            if (rep_row is None or not rep_row["node_id"]) and not has_pending_rep:
                gen_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM write_op WHERE op_key LIKE ?",
                    (f"prog_report:{gid}:%",),
                ).fetchone()
                gen = (int(gen_row["c"]) if gen_row else 0) + 1
                rep_op_key = f"prog_report:{gid}:{gen}"
                enqueue_op(
                    conn,
                    run_id=run_id,
                    op_key=rep_op_key,
                    file_id=fid,
                    action="insert",
                    parent_ref=f"node:{g['node_id']}",
                    idx=1,
                    payload={"content": render.progress_report_header(run_date, bot_tag)},
                    bot_kind="progress_report",
                    bot_ref_id=gid,
                    marker=render.marker("progress_report", gid),
                )
                next_names = _recommend_next_concepts(conn, gid)
                enqueue_op(
                    conn,
                    run_id=run_id,
                    op_key=f"prog_report_next:{gid}:{gen}",
                    file_id=fid,
                    action="insert",
                    parent_ref=f"op:{rep_op_key}",
                    idx=-1,
                    payload={"content": render.progress_next_line(next_names)},
                    bot_kind="progress_report_item",
                    bot_ref_id=gid,
                )
                stats["reports"] += 1

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return stats
