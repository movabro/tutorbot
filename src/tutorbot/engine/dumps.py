"""Stage 6: Knowledge dump evaluation (T2) and state event application (spec §6.2, §7.4, §9.2, §9.3)."""
from __future__ import annotations

import logging
import sqlite3

from .. import clock, render
from ..dynalist.tree import NodeIndex
from ..dynalist.writer import enqueue_op
from ..llm.client import LLMClient, LLMError, load_prompt
from ..llm.retriever import ConceptRetriever
from ..llm.schemas import T2DumpOut
from .state import apply_dump_transition

log = logging.getLogger(__name__)
PROMPT_VERSION = "t2_dump:v2"
DUMP_MAX_CHARS = 12000


def _enqueue_dump_feedback(
    conn: sqlite3.Connection,
    *,
    dump: sqlite3.Row,
    lines: list[str],
    run_id: int | None,
    run_date: str,
    bot_tag: str,
    indices: dict[str, NodeIndex] | None,
) -> None:
    dump_id = int(dump["id"])
    file_id = str(dump["file_id"])
    dump_node_id = str(dump["node_id"])
    run_key = str(run_id) if run_id is not None else clock.now_iso()
    index = indices.get(file_id) if indices else None
    header_id = None

    if index is not None:
        headers = conn.execute(
            "SELECT node_id FROM bot_node WHERE file_id=? AND kind='dump_feedback'"
            " AND ref_id=? AND deleted_at IS NULL ORDER BY created_at DESC, node_id DESC",
            (file_id, dump_id),
        ).fetchall()
        header_id = next(
            (
                str(row["node_id"])
                for row in headers
                if row["node_id"] in index and index.parent_of.get(str(row["node_id"])) == dump_node_id
            ),
            None,
        )

    if header_id is None:
        root_op_key = f"dump_feedback:{dump_id}:{run_key}"
        enqueue_op(
            conn,
            run_id=run_id,
            op_key=root_op_key,
            file_id=file_id,
            action="insert",
            parent_ref=f"node:{dump_node_id}",
            idx=-1,
            payload={"content": render.dump_feedback_header(run_date, bot_tag)},
            bot_kind="dump_feedback",
            bot_ref_id=dump_id,
            marker=render.marker("dump_feedback", f"{dump_id}:{run_key}"),
        )
        for line_index, line in enumerate(lines):
            enqueue_op(
                conn,
                run_id=run_id,
                op_key=f"{root_op_key}:line:{line_index}",
                file_id=file_id,
                action="insert",
                parent_ref=f"op:{root_op_key}",
                idx=-1,
                payload={"content": line},
                bot_kind="dump_feedback_item",
                bot_ref_id=dump_id,
            )
        return

    edit_prefix = f"dump_feedback_edit:{dump_id}:{run_key}"
    enqueue_op(
        conn,
        run_id=run_id,
        op_key=f"{edit_prefix}:header",
        file_id=file_id,
        action="edit",
        target_ref=f"node:{header_id}",
        payload={"content": render.dump_feedback_header(run_date, bot_tag)},
        bot_kind="dump_feedback",
        bot_ref_id=dump_id,
    )
    for line_index, child in enumerate(index.children_of(header_id)):
        enqueue_op(
            conn,
            run_id=run_id,
            op_key=f"{edit_prefix}:delete:{line_index}",
            file_id=file_id,
            action="delete",
            target_ref=f"node:{child.id}",
            bot_kind="dump_feedback_item",
            bot_ref_id=dump_id,
        )
    for line_index, line in enumerate(lines):
        enqueue_op(
            conn,
            run_id=run_id,
            op_key=f"{edit_prefix}:line:{line_index}",
            file_id=file_id,
            action="insert",
            parent_ref=f"node:{header_id}",
            idx=-1,
            payload={"content": line},
            bot_kind="dump_feedback_item",
            bot_ref_id=dump_id,
        )


def process_dumps(
    conn: sqlite3.Connection,
    llm: LLMClient,
    run_id: int | None = None,
    index_full_threshold: int = 150,
    daily_token_budget: int = 300000,
    indices: dict[str, NodeIndex] | None = None,
    run_date: str | None = None,
    bot_tag: str = "tutorbot",
) -> dict[str, int]:
    """Process knowledge_dumps whose content_hash != processed_hash and status in ('pending','error')."""
    stats = {"processed": 0, "transitions": 0, "errors": 0, "budget_deferred": 0}
    evaluation_date = run_date or clock.local_date("Asia/Seoul").isoformat()
    dumps = conn.execute(
        "SELECT * FROM knowledge_dump WHERE status IN ('pending','error') AND retry_count < 3"
        " AND (processed_hash IS NULL OR content_hash != processed_hash) ORDER BY id ASC"
    ).fetchall()

    for d in dumps:
        if (llm.stats["prompt_tokens"] + llm.stats["completion_tokens"]) >= daily_token_budget:
            stats["budget_deferred"] += 1
            continue

        did = int(d["id"])
        gid = d["goal_id"]
        if not gid:
            continue

        retriever = ConceptRetriever(conn, int(gid), full_threshold=index_full_threshold)
        candidates = retriever.all_leaf_candidates()
        if not candidates:
            continue
        candidate_ids = {c.id for c in candidates}
        index_str = retriever.format_index(candidates)

        raw_text = (d["raw_text"] or "")[:DUMP_MAX_CHARS]
        sys_prompt = load_prompt("t2_dump")
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"개념 인덱스:\n{index_str}\n\n지식덤프 원문:\n{raw_text}"},
        ]

        try:
            out: T2DumpOut = llm.complete_json(
                PROMPT_VERSION,
                messages,
                validator=T2DumpOut.validate,
                ref=f"dump:{did}",
            )
        except LLMError as e:
            new_retry = int(d["retry_count"]) + 1
            new_status = "failed" if new_retry >= 3 else "error"
            conn.execute(
                "UPDATE knowledge_dump SET status=?, retry_count=?, last_error=? WHERE id=?",
                (new_status, new_retry, str(e), did),
            )
            stats["errors"] += 1
            continue

        # Ignore IDs outside the current curriculum before changing state or writing feedback.
        candidate_names = {c.id: c.name for c in candidates}
        assessed_map: dict[int, tuple[str, str]] = {}
        for item in out.assessments:
            if item.concept_id not in candidate_ids:
                log.warning("T2 dump %s hallucinated concept_id %s; ignoring", did, item.concept_id)
                continue
            assessed_map[item.concept_id] = (item.state, item.evidence)

        valid_issues = []
        incorrect_by_concept: dict[int, list[str]] = {}
        uncertain_concepts: set[int] = set()
        for issue in out.issues:
            if issue.concept_id not in candidate_ids:
                log.warning("T2 dump %s hallucinated issue concept_id %s; ignoring", did, issue.concept_id)
                continue
            valid_issues.append(issue)
            if issue.verdict == "incorrect":
                incorrect_by_concept.setdefault(issue.concept_id, []).append(issue.claim)
            else:
                uncertain_concepts.add(issue.concept_id)

        for cid, claims in incorrect_by_concept.items():
            issue_summary = "오개념 확인: " + "; ".join(claims)
            if cid in assessed_map:
                state, evidence = assessed_map[cid]
                if state == "understood":
                    state = "partial"
                assessed_map[cid] = (state, f"{evidence}; {issue_summary}"[:240] if evidence else issue_summary[:240])
            else:
                assessed_map[cid] = ("unknown", issue_summary[:240])

        next_names: list[str] = []
        seen_next: set[int] = set()
        for cid in out.next_concepts:
            if cid not in candidate_names:
                log.warning("T2 dump %s hallucinated next concept_id %s; ignoring", did, cid)
                continue
            if cid not in seen_next:
                next_names.append(candidate_names[cid])
                seen_next.add(cid)

        now_iso = clock.now_iso()
        conn.execute("BEGIN")
        try:
            state_changes: list[tuple[str, str, str, str]] = []

            def apply_and_track(cid: int, state: str, evidence: str) -> None:
                row = conn.execute("SELECT name, state FROM concept WHERE id=?", (cid,)).fetchone()
                if row is None:
                    return
                old_state = str(row["state"])
                if apply_dump_transition(conn, cid, state, did, evidence, run_id):
                    new_row = conn.execute("SELECT state FROM concept WHERE id=?", (cid,)).fetchone()
                    if new_row is not None and new_row["state"] != old_state:
                        state_changes.append(
                            (str(row["name"]), old_state, str(new_row["state"]), evidence)
                        )
                    stats["transitions"] += 1

            # Apply assessments for mentioned concepts
            for cid, (st, ev) in assessed_map.items():
                apply_and_track(cid, st, ev)

            # Do not keep dump-only progress when an updated dump no longer gives evidence,
            # except when the model explicitly marks that concept's claim as uncertain.
            if d["processed_hash"] is not None:
                for cid in candidate_ids:
                    if cid not in assessed_map and cid not in uncertain_concepts:
                        apply_and_track(cid, "unknown", "수정된 덤프에서 확인 가능한 근거 없음")

            feedback_lines = [render.dump_feedback_summary(out.summary)]
            for issue in valid_issues:
                feedback_lines.extend(
                    render.dump_issue_lines(
                        issue.verdict,
                        candidate_names[issue.concept_id],
                        issue.claim,
                        issue.correction,
                        issue.reason,
                    )
                )
            if state_changes:
                feedback_lines.extend(
                    render.dump_curriculum_change(name, old, new, reason)
                    for name, old, new, reason in state_changes
                )
            else:
                feedback_lines.append(render.dump_no_curriculum_change())
            feedback_lines.extend(render.dump_gap_line(gap) for gap in out.gap_summary)
            feedback_lines.append(render.dump_next_concepts(next_names))
            _enqueue_dump_feedback(
                conn,
                dump=d,
                lines=feedback_lines,
                run_id=run_id,
                run_date=evaluation_date,
                bot_tag=bot_tag,
                indices=indices,
            )

            conn.execute(
                "UPDATE knowledge_dump SET processed_hash=content_hash, status='processed',"
                " retry_count=0, last_error=NULL, processed_at=? WHERE id=?",
                (now_iso, did),
            )
            conn.execute("COMMIT")
            stats["processed"] += 1
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return stats
