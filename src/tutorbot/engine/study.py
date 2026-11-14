"""Stage 7: Study entry analysis (T3: mapping + feedback + cards) and feedback write_ops (spec D2, §6.2, §6.3, §8.2, §9.2)."""
from __future__ import annotations

import difflib
import json
import logging
import sqlite3
from datetime import date, timedelta
from typing import Any

from .. import clock, render
from ..dynalist.tree import NodeIndex
from ..dynalist.writer import enqueue_op
from ..llm.client import LLMClient, LLMError, load_prompt
from ..llm.retriever import ConceptRetriever
from ..llm.schemas import T3StudyOut
from ..parse.hashing import norm
from .state import apply_study_transition

log = logging.getLogger(__name__)
PROMPT_VERSION = "t3_study:v1"


def _truncate_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    half = (max_chars - 20) // 2
    return text[:half] + "\n...(중략)...\n" + text[-half:]


def _next_day(iso_date: str) -> str:
    return (date.fromisoformat(iso_date) + timedelta(days=1)).isoformat()


def _enqueue_feedback_ops(
    conn: sqlite3.Connection,
    entry: sqlite3.Row,
    fb_version: int,
    header_text: str,
    child_lines: list[str],
    existing_header_nid: str | None,
    idx_obj: NodeIndex | None,
    run_id: int | None,
) -> None:
    """Enqueue either initial feedback creation (`fb:{entry_id}`) or in-place update (`fb_edit:{entry_id}:{hash[:12]}`)."""
    eid = int(entry["id"])
    fid = entry["file_id"]
    h12 = (entry["content_hash"] or "")[:12]

    header_exists_in_doc = bool(
        existing_header_nid and (idx_obj is None or existing_header_nid in idx_obj)
    )

    if not header_exists_in_doc:
        # Either first feedback (version 1) or user deleted previous feedback and then modified the study entry (§3.4)
        root_op = f"fb:{eid}" if fb_version == 1 else f"fb:{eid}:v{fb_version}"
        enqueue_op(
            conn,
            run_id=run_id,
            op_key=root_op,
            file_id=fid,
            action="insert",
            parent_ref=f"node:{entry['node_id']}",
            idx=-1,
            payload={"content": header_text},
            bot_kind="feedback",
            bot_ref_id=eid,
            marker=render.marker("feedback", eid),
        )
        for i, line in enumerate(child_lines):
            enqueue_op(
                conn,
                run_id=run_id,
                op_key=f"{root_op}:line:{i}",
                file_id=fid,
                action="insert",
                parent_ref=f"op:{root_op}",
                idx=-1,
                payload={"content": line},
                bot_kind="feedback_item",
                bot_ref_id=eid,
            )
    else:
        # Update existing feedback header in-place: edit header + delete old children + insert new children (spec §8.2)
        base_key = f"fb_edit:{eid}:{h12}"
        enqueue_op(
            conn,
            run_id=run_id,
            op_key=base_key,
            file_id=fid,
            action="edit",
            target_ref=f"node:{existing_header_nid}",
            payload={"content": header_text},
            bot_kind="feedback",
            bot_ref_id=eid,
        )
        if idx_obj is not None and existing_header_nid:
            for old_child in idx_obj.children_of(existing_header_nid):
                enqueue_op(
                    conn,
                    run_id=run_id,
                    op_key=f"{base_key}:del:{old_child.id}",
                    file_id=fid,
                    action="delete",
                    target_ref=f"node:{old_child.id}",
                    payload={},
                    bot_kind="feedback_item",
                    bot_ref_id=eid,
                )
        for i, line in enumerate(child_lines):
            enqueue_op(
                conn,
                run_id=run_id,
                op_key=f"{base_key}:ins:{i}",
                file_id=fid,
                action="insert",
                parent_ref=f"node:{existing_header_nid}",
                idx=-1,
                payload={"content": line},
                bot_kind="feedback_item",
                bot_ref_id=eid,
            )


def process_study_entries(
    conn: sqlite3.Connection,
    indices: dict[str, NodeIndex],
    llm: LLMClient,
    settings: Any,
    run_date: str,
    run_id: int | None = None,
) -> dict[str, int]:
    """Process eligible study entries (spec §6.3, D2, §9.2 T3)."""
    stats = {
        "processed": 0,
        "deferred_settle": 0,
        "skipped_similar": 0,
        "capped": 0,
        "cards_created": 0,
        "transitions": 0,
        "errors": 0,
        "budget_deferred": 0,
    }

    proc_cfg = getattr(settings, "processing", None)
    settle_minutes = int(getattr(proc_cfg, "settle_minutes", 60))
    similarity_skip = float(getattr(proc_cfg, "similarity_skip", 0.95))
    max_entries = int(getattr(proc_cfg, "max_entries_per_run", 20))
    entry_max_chars = int(getattr(proc_cfg, "entry_max_chars", 6000))
    llm_cfg = getattr(settings, "llm", None)
    index_full_threshold = int(getattr(llm_cfg, "index_full_threshold", 150))
    daily_token_budget = int(getattr(llm_cfg, "daily_token_budget", 300000))
    bot_tag = getattr(getattr(settings, "tags", None), "bot", "tutorbot")

    now_ms = clock.now_ms()
    settle_ms = settle_minutes * 60 * 1000

    rows = conn.execute(
        "SELECT * FROM study_entry WHERE status IN ('pending','error','deferred') AND retry_count < 3"
        " AND (processed_hash IS NULL OR content_hash != processed_hash) ORDER BY entry_date ASC, id ASC"
    ).fetchall()

    llm_processed_count = 0
    for e in rows:
        eid = int(e["id"])
        gid = e["goal_id"]
        if not gid:
            continue

        # 1) Settle rule (D2 & §6.3): now - max_modified >= settle_minutes
        max_mod = int(e["max_modified"] or 0)
        if settle_ms > 0 and max_mod > 0 and (now_ms - max_mod) < settle_ms:
            conn.execute("UPDATE study_entry SET status='deferred' WHERE id=?", (eid,))
            stats["deferred_settle"] += 1
            continue

        # 2) Similarity skip rule (D2 & §6.3): if previously processed and ratio >= 0.95 -> update hash only
        prev_text = e["processed_text"]
        if prev_text:
            ratio = difflib.SequenceMatcher(None, norm(prev_text), norm(e["raw_text"])).ratio()
            if ratio >= similarity_skip:
                conn.execute(
                    "UPDATE study_entry SET processed_hash=content_hash, processed_text=raw_text,"
                    " status='processed', processed_at=? WHERE id=?",
                    (clock.now_iso(), eid),
                )
                stats["skipped_similar"] += 1
                continue

        # 3) Per-run cap (§6.3) and token budget (§6.4)
        if llm_processed_count >= max_entries:
            stats["capped"] += 1
            continue
        if (llm.stats["prompt_tokens"] + llm.stats["completion_tokens"]) >= daily_token_budget:
            stats["budget_deferred"] += 1
            continue

        retriever = ConceptRetriever(conn, int(gid), full_threshold=index_full_threshold)
        candidates = retriever.candidates(e["raw_text"] or "")
        if not candidates:
            continue
        candidate_by_id = {c.id: c for c in candidates}
        index_str = retriever.format_index(candidates)

        # Learner profile summary & existing card concepts for this entry
        prof_row = conn.execute("SELECT summary FROM learner_profile WHERE goal_id=?", (gid,)).fetchone()
        profile_summary = prof_row["summary"] if prof_row else "(없음)"

        existing_card_cids = [
            int(r["concept_id"])
            for r in conn.execute("SELECT concept_id FROM quiz_card WHERE source_entry_id=?", (eid,))
        ]
        prev_fb_row = conn.execute(
            "SELECT * FROM feedback WHERE entry_id=? ORDER BY version DESC LIMIT 1",
            (eid,),
        ).fetchone()
        prev_fb_json = prev_fb_row["body_json"] if prev_fb_row else None

        body_text = _truncate_middle(e["raw_text"] or "", entry_max_chars)
        sys_prompt = load_prompt("t3_study")
        user_parts = [
            f"개념 인덱스:\n{index_str}",
            f"학습자 프로필 요약:\n{profile_summary}",
            f"이미 카드가 있는 개념 ID (excluded_concept_ids): {existing_card_cids}",
        ]
        if prev_fb_json:
            user_parts.append(f"이전 피드백 JSON:\n{prev_fb_json}")
        user_parts.append(f"공부 기록 원문:\n{body_text}")

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]

        try:
            out: T3StudyOut = llm.complete_json(
                PROMPT_VERSION,
                messages,
                validator=T3StudyOut.validate,
                ref=f"entry:{eid}",
            )
        except LLMError as err:
            new_retry = int(e["retry_count"]) + 1
            new_status = "failed" if new_retry >= 3 else "error"
            conn.execute(
                "UPDATE study_entry SET status=?, retry_count=?, last_error=? WHERE id=?",
                (new_status, new_retry, str(err), eid),
            )
            stats["errors"] += 1
            continue

        llm_processed_count += 1

        # Filter hallucinated concept_ids
        valid_concepts = []
        for item in out.concepts:
            if item.concept_id not in candidate_by_id:
                log.warning("T3 entry %s hallucinated concept_id %s; ignoring", eid, item.concept_id)
                continue
            valid_concepts.append(item)

        eligible_card_cids = {
            item.concept_id for item in valid_concepts if item.coverage in ("partial", "full")
        } - set(existing_card_cids)

        valid_cards = []
        for card in out.cards:
            if card.concept_id not in candidate_by_id:
                log.warning("T3 card hallucinated concept_id %s; ignoring", card.concept_id)
                continue
            if card.concept_id not in eligible_card_cids:
                continue
            valid_cards.append(card)

        mapped_names = [candidate_by_id[item.concept_id].name for item in valid_concepts]
        fb_lines = render.feedback_lines(
            out.feedback.corrections,
            out.feedback.missing,
            out.feedback.next,
            mapped_names,
        )
        fb_header = render.feedback_header(run_date, bot_tag)
        next_version = (int(prev_fb_row["version"]) + 1) if prev_fb_row else 1
        existing_header_nid = prev_fb_row["node_id"] if prev_fb_row else None

        now_iso = clock.now_iso()
        due_date = _next_day(run_date)
        idx_obj = indices.get(e["file_id"])

        conn.execute("BEGIN")
        try:
            # 1) Save entry_concept mappings & apply state transitions (§7.4)
            conn.execute("DELETE FROM entry_concept WHERE entry_id=?", (eid,))
            for item in valid_concepts:
                conn.execute(
                    "INSERT OR REPLACE INTO entry_concept(entry_id,concept_id,coverage) VALUES(?,?,?)",
                    (eid, item.concept_id, item.coverage),
                )
                if apply_study_transition(
                    conn,
                    item.concept_id,
                    item.coverage,
                    item.state_suggestion,
                    eid,
                    run_id=run_id,
                ):
                    stats["transitions"] += 1

            # 2) Save feedback version row
            fb_body = json.dumps(
                {
                    "corrections": out.feedback.corrections,
                    "missing": out.feedback.missing,
                    "next": out.feedback.next,
                    "mapped": mapped_names,
                    "off_curriculum": out.off_curriculum,
                },
                ensure_ascii=False,
            )
            conn.execute(
                "INSERT OR REPLACE INTO feedback(entry_id,based_on_hash,version,body_json,node_id,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (eid, e["content_hash"], next_version, fb_body, existing_header_nid, now_iso),
            )

            # 3) Save new quiz cards (only for concepts without an existing card for this entry)
            for card in valid_cards:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO quiz_card(goal_id,concept_id,source_entry_id,question,answer_key,"
                    "due_date,created_at) VALUES(?,?,?,?,?,?,?)",
                    (gid, card.concept_id, eid, card.question, card.answer_key, due_date, now_iso),
                )
                if cur.rowcount:
                    stats["cards_created"] += 1

            # 4) Enqueue feedback write_ops (initial insert or in-place edit)
            _enqueue_feedback_ops(
                conn,
                e,
                next_version,
                fb_header,
                fb_lines,
                existing_header_nid,
                idx_obj,
                run_id,
            )

            # 5) Mark study_entry as processed
            conn.execute(
                "UPDATE study_entry SET processed_hash=content_hash, processed_text=raw_text,"
                " status='processed', retry_count=0, last_error=NULL, processed_at=? WHERE id=?",
                (now_iso, eid),
            )
            conn.execute("COMMIT")
            stats["processed"] += 1
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return stats
