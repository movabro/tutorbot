"""Stage 5: Goal & Curriculum generation (T1) and tree reconstruction (spec D5, §6.2, §8.2, §9.2)."""
from __future__ import annotations

import sqlite3
from typing import Any

from .. import clock, render
from ..dynalist.tree import NodeIndex
from ..dynalist.writer import enqueue_op
from ..llm.client import LLMClient, LLMError, load_prompt
from ..llm.schemas import T1CurriculumOut

PROMPT_VERSION = "t1_curriculum:v1"


def _curriculum_gen(conn: sqlite3.Connection, goal_id: int) -> int:
    """Count how many curriculum_root write_ops have been created for this goal."""
    prefix = f"curr_root:{goal_id}:"
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM write_op WHERE op_key LIKE ?",
        (prefix + "%",),
    ).fetchone()
    return int(row["c"]) if row else 0


def _active_curriculum_root(conn: sqlite3.Connection, file_id: str, goal_id: int) -> str | None:
    row = conn.execute(
        "SELECT node_id FROM bot_node WHERE file_id=? AND kind='curriculum_root' AND ref_id=? AND deleted_at IS NULL",
        (file_id, goal_id),
    ).fetchone()
    return row["node_id"] if row else None


def _compute_leaf_counts(concepts: list[sqlite3.Row]) -> tuple[int, dict[int, tuple[int, int]]]:
    """Return (progress_pct, {concept_id: (done_leaves, total_leaves)})."""
    by_parent: dict[int | None, list[sqlite3.Row]] = {}
    for c in concepts:
        if c["user_deleted"]:
            continue
        by_parent.setdefault(c["parent_id"], []).append(c)

    weights = {"unknown": 0.0, "partial": 0.5, "understood": 1.0, "mastered": 1.0}
    total_leaf_weight = 0.0
    total_leaves = 0
    branch_counts: dict[int, tuple[int, int]] = {}

    def walk_counts(cid: int) -> tuple[int, int]:
        kids = by_parent.get(cid, [])
        if not kids:
            return 0, 0
        done_sum = 0
        leaf_sum = 0
        for k in kids:
            if k["is_leaf"]:
                leaf_sum += 1
                if k["state"] in ("understood", "mastered"):
                    done_sum += 1
            else:
                d, l = walk_counts(k["id"])
                done_sum += d
                leaf_sum += l
        branch_counts[cid] = (done_sum, leaf_sum)
        return done_sum, leaf_sum

    for c in concepts:
        if c["user_deleted"]:
            continue
        if c["is_leaf"]:
            total_leaves += 1
            total_leaf_weight += weights.get(c["state"], 0.0)
        else:
            walk_counts(c["id"])

    pct = round(total_leaf_weight / total_leaves * 100) if total_leaves > 0 else 0
    return pct, branch_counts


def enqueue_curriculum_tree(
    conn: sqlite3.Connection,
    goal: sqlite3.Row,
    gen: int,
    run_id: int | None = None,
    bot_tag: str = "tutorbot",
) -> None:
    """Enqueue write_ops for curriculum_root and all non-deleted concepts in depth/order."""
    gid = goal["id"]
    fid = goal["file_id"]
    concepts = conn.execute(
        "SELECT * FROM concept WHERE goal_id=? AND user_deleted=0 ORDER BY depth ASC, sort_order ASC, id ASC",
        (gid,),
    ).fetchall()
    pct, branch_counts = _compute_leaf_counts(concepts)

    root_op_key = f"curr_root:{gid}:{gen}"
    enqueue_op(
        conn,
        run_id=run_id,
        op_key=root_op_key,
        file_id=fid,
        action="insert",
        parent_ref=f"node:{goal['node_id']}",
        idx=0,
        payload={"content": render.curriculum_root(pct, bot_tag)},
        bot_kind="curriculum_root",
        bot_ref_id=gid,
        marker=render.marker("curriculum_root", gid),
    )

    for c in concepts:
        cid = c["id"]
        op_key = f"concept:{cid}:{gen}"
        if c["parent_id"] is None:
            parent_ref = f"op:{root_op_key}"
        else:
            parent_ref = f"op:concept:{c['parent_id']}:{gen}"
        if c["is_leaf"]:
            txt = render.concept_leaf(c["name"], c["state"])
        else:
            done_l, tot_l = branch_counts.get(cid, (0, 0))
            txt = render.concept_branch(c["name"], done_l, tot_l)
        enqueue_op(
            conn,
            run_id=run_id,
            op_key=op_key,
            file_id=fid,
            action="insert",
            parent_ref=parent_ref,
            idx=-1,
            payload={"content": txt},
            bot_kind="concept",
            bot_ref_id=cid,
            marker=None,
        )


def process_goals(
    conn: sqlite3.Connection,
    indices: dict[str, NodeIndex],
    llm: LLMClient,
    run_date: str,
    run_id: int | None = None,
    bot_tag: str = "tutorbot",
) -> dict[str, int]:
    """Process pending/error goals (T1) and check active goals for deleted curriculum_root or concept nodes (D5)."""
    stats = {"generated": 0, "rebuilt": 0, "errors": 0, "user_deleted_concepts": 0}
    goals = conn.execute("SELECT * FROM goal WHERE status != 'archived' ORDER BY id ASC").fetchall()

    for g in goals:
        gid = g["id"]
        fid = g["file_id"]
        idx_obj = indices.get(fid)

        # Case 1: Goal needs initial curriculum generation
        if g["status"] in ("pending_curriculum", "error") and g["retry_count"] < 3:
            raw_text = g["title"]
            if idx_obj is not None and g["node_id"] in idx_obj:
                raw_text = idx_obj.text_of(g["node_id"])
            sys_prompt = load_prompt("t1_curriculum")
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"run_date: {run_date}\n\n목표 원문:\n{raw_text}"},
            ]
            try:
                out: T1CurriculumOut = llm.complete_json(
                    PROMPT_VERSION,
                    messages,
                    validator=lambda d: T1CurriculumOut.validate(d, strict_leaf_count=False),
                    ref=f"goal:{gid}",
                )
            except LLMError as e:
                now_iso = clock.now_iso()
                new_retry = int(g["retry_count"]) + 1
                new_status = "failed" if new_retry >= 3 else "error"
                conn.execute("BEGIN")
                conn.execute(
                    "UPDATE goal SET status=?, retry_count=?, last_error=?, updated_at=? WHERE id=?",
                    (new_status, new_retry, str(e), now_iso, gid),
                )
                if new_status == "failed":
                    enqueue_op(
                        conn,
                        run_id=run_id,
                        op_key=f"warn:goal:{fid}:{g['node_id']}:curriculum_failed",
                        file_id=fid,
                        action="insert",
                        parent_ref=f"node:{g['node_id']}",
                        idx=-1,
                        payload={"content": render.warning_node("커리큘럼 생성에 3회 실패했습니다.", bot_tag)},
                        bot_kind="warning",
                        bot_ref_id=gid,
                        marker=render.marker("warning", gid),
                    )
                conn.execute("COMMIT")
                stats["errors"] += 1
                continue

            # Save concepts + write_ops in one transaction
            now_iso = clock.now_iso()
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "UPDATE goal SET title=?, level=?, deadline=?, status='active', retry_count=0,"
                    " last_error=NULL, updated_at=? WHERE id=?",
                    (out.goal.title, out.goal.level, out.goal.deadline, now_iso, gid),
                )
                # Insert concepts in depth order so parent_id is available
                key_to_id: dict[str, int] = {}
                sorted_items = sorted(out.concepts, key=lambda c: (out.depths[c.key], c.order))
                for item in sorted_items:
                    pid = key_to_id[item.parent_key] if item.parent_key else None
                    cur = conn.execute(
                        "INSERT INTO concept(goal_id,parent_id,name,description,depth,sort_order,is_leaf,"
                        "state,user_deleted,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'unknown',0,?,?)",
                        (
                            gid,
                            pid,
                            item.name,
                            item.description,
                            out.depths[item.key],
                            item.order,
                            1 if out.is_leaf[item.key] else 0,
                            now_iso,
                            now_iso,
                        ),
                    )
                    key_to_id[item.key] = int(cur.lastrowid)

                for item in sorted_items:
                    cid = key_to_id[item.key]
                    for pq in item.prereq_keys:
                        conn.execute(
                            "INSERT OR IGNORE INTO concept_prereq(concept_id,prereq_id) VALUES(?,?)",
                            (cid, key_to_id[pq]),
                        )

                gen = _curriculum_gen(conn, gid) + 1
                enqueue_curriculum_tree(conn, g, gen=gen, run_id=run_id, bot_tag=bot_tag)
                conn.execute("COMMIT")
                stats["generated"] += 1
            except Exception:
                conn.execute("ROLLBACK")
                raise
            continue

        # Case 2: Active goal -> check D5 (deleted curriculum_root or deleted individual concept nodes)
        if g["status"] == "active" and idx_obj is not None:
            curr_root_nid = _active_curriculum_root(conn, fid, gid)
            has_pending_root = conn.execute(
                "SELECT 1 FROM write_op WHERE op_key LIKE ? AND status IN ('pending','failed')",
                (f"curr_root:{gid}:%",),
            ).fetchone()
            if curr_root_nid is None and not has_pending_root:
                # Curriculum root container was deleted by user -> rebuild entire tree from DB (excluding user_deleted)
                conn.execute("BEGIN")
                conn.execute(
                    "UPDATE concept SET node_id=NULL, rendered_text=NULL, updated_at=? WHERE goal_id=? AND user_deleted=0",
                    (clock.now_iso(), gid),
                )
                gen = _curriculum_gen(conn, gid) + 1
                enqueue_curriculum_tree(conn, g, gen=gen, run_id=run_id, bot_tag=bot_tag)
                conn.execute("COMMIT")
                stats["rebuilt"] += 1
            elif curr_root_nid is not None and curr_root_nid in idx_obj:
                # Root exists: any concept whose node_id was written and is now missing in idx_obj was deleted by user
                concepts = conn.execute(
                    "SELECT id, node_id FROM concept WHERE goal_id=? AND user_deleted=0 AND node_id IS NOT NULL",
                    (gid,),
                ).fetchall()
                missing_ids = [c["id"] for c in concepts if c["node_id"] not in idx_obj]
                if missing_ids:
                    now_iso = clock.now_iso()
                    conn.execute("BEGIN")
                    for cid in missing_ids:
                        conn.execute(
                            "UPDATE concept SET user_deleted=1, updated_at=? WHERE id=?",
                            (now_iso, cid),
                        )
                    conn.execute("COMMIT")
                    stats["user_deleted_concepts"] += len(missing_ids)

    return stats
