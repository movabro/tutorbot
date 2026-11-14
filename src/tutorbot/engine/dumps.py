"""Stage 6: Knowledge dump evaluation (T2) and state event application (spec §6.2, §7.4, §9.2, §9.3)."""
from __future__ import annotations

import logging
import sqlite3

from .. import clock
from ..llm.client import LLMClient, LLMError, load_prompt
from ..llm.retriever import ConceptRetriever
from ..llm.schemas import T2DumpOut
from .state import apply_dump_transition

log = logging.getLogger(__name__)
PROMPT_VERSION = "t2_dump:v1"
DUMP_MAX_CHARS = 12000


def process_dumps(
    conn: sqlite3.Connection,
    llm: LLMClient,
    run_id: int | None = None,
    index_full_threshold: int = 150,
    daily_token_budget: int = 300000,
) -> dict[str, int]:
    """Process knowledge_dumps whose content_hash != processed_hash and status in ('pending','error')."""
    stats = {"processed": 0, "transitions": 0, "errors": 0, "budget_deferred": 0}
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

        # Filter hallucinated concept_ids not in candidate_ids
        assessed_map: dict[int, tuple[str, str]] = {}
        for item in out.assessments:
            if item.concept_id not in candidate_ids:
                log.warning("T2 dump %s hallucinated concept_id %s; ignoring", did, item.concept_id)
                continue
            assessed_map[item.concept_id] = (item.state, item.evidence)

        now_iso = clock.now_iso()
        conn.execute("BEGIN")
        try:
            # Apply assessments for mentioned concepts
            for cid, (st, ev) in assessed_map.items():
                if apply_dump_transition(conn, cid, st, did, ev, run_id):
                    stats["transitions"] += 1

            # Re-evaluation rule (§7.4): if this dump was previously processed, any concept that was
            # previously raised by dump ONLY and is no longer mentioned in the updated dump can be lowered to unknown.
            if d["processed_hash"] is not None:
                for cid in candidate_ids:
                    if cid not in assessed_map:
                        if apply_dump_transition(conn, cid, "unknown", did, "removed from dump", run_id):
                            stats["transitions"] += 1

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
