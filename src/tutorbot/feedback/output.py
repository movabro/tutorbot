"""Persist sanitized feedback and its dependent outbox operations."""
from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import quote

from .. import clock
from ..dynalist.writer import enqueue_op
from ..render import marker
from . import store
from .models import NodeRef, TargetBlock, TargetSnapshot, UnitSpec


def sanitize_text(s: str, max_len: int) -> str:
    text = unicodedata.normalize("NFC", s)
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) != "Cc")
    text = re.sub(r"#[^\s]+", lambda match: "＃" + match.group()[1:], text)
    text = text.replace("!(", "！(")
    return re.sub(r"[^\S\n]+", " ", text).strip()[:max_len]


def enqueue_result(conn, *, task, task_run_id: int, run_id: int, unit: UnitSpec, write_to: NodeRef,
                   out, local_date: str, bot_tag: str, truncated: bool, over_target: bool,
                   snapshot: TargetSnapshot | None, input_hash: str | None = None,
                   selected_target: TargetBlock | None = None) -> int:
    """Enqueue one target. An enclosing transaction groups all targets of a unit."""
    get = out.get if isinstance(out, dict) else lambda key: getattr(out, key)
    summary = sanitize_text(get("summary"), 120)
    if not summary:
        raise ValueError("empty_summary")
    feedback = [value for item in get("feedback")[:20] if (value := sanitize_text(item, 800))]
    evidence = [value for item in get("evidence")[:10] if (value := sanitize_text(item, 300))]
    body = {"summary": summary, "feedback": feedback, "evidence": evidence}
    ref = f"{task.id}:{task_run_id}:{unit.ref}:{quote(write_to.file_id, safe='')}:{write_to.node_id}"
    root_key = f"fr:{ref}"
    with store._transaction(conn):
        existing = conn.execute("SELECT id FROM feedback_output WHERE task_run_id=? AND unit_ref=? "
                                "AND target_file_id=? AND target_node_id=?",
                                (task_run_id, unit.ref, write_to.file_id, write_to.node_id)).fetchone()
        if existing:
            return existing[0]
        if write_to.node_id == "root":
            snapshot = None
        output_id = store.insert_output(conn,
            task_run_id=task_run_id, task_id=task.id, unit_ref=unit.ref,
            target_file_id=write_to.file_id, target_node_id=write_to.node_id,
            root_op_key=root_key, status="pending_write", input_hash=input_hash,
            truncated=int(truncated), over_target=int(over_target),
            body_json=json.dumps(body, ensure_ascii=False),
            target_own_hash=snapshot.own_hash if snapshot else None,
            target_effective_modified_ms=snapshot.effective_modified_ms if snapshot else None,
            created_at=clock.now_iso())
        header = f"🤖 [{task.id}] {summary[:60]} !({local_date}) #{bot_tag}"[:200]
        enqueue_op(conn, run_id=run_id, op_key=root_key, file_id=write_to.file_id,
                   action="insert", parent_ref=f"node:{write_to.node_id}", payload={"content": header},
                   bot_kind="feedback_rule", bot_ref_id=output_id, marker=marker("feedback_rule", ref))
        lines = []
        if task.unit == "per_node" and task.output.placement == "under_parent" and unit.inputs:
            target = unit.inputs[0].ref
            excerpt = ""
            if selected_target is not None:
                if selected_target.selected.ref != target:
                    raise ValueError("selected target does not match unit")
                line = next((line for line in selected_target.lines if line.ref == target), None)
                if line is None:
                    raise ValueError("selected target content missing")
                excerpt = sanitize_text(line.text.replace("\n", " "), 40)
            lines.append(f"🎯 대상: [{target.file_id}/{target.node_id}]" + (f" {excerpt}" if excerpt else ""))
        lines.append(f"📝 {summary}")
        lines.extend(f"• {item}" for item in feedback)
        lines.extend(f"🔎 근거: {item}" for item in evidence)
        if truncated:
            lines.append("⚠️ 입력 일부가 잘렸습니다")
        if over_target:
            lines.append(f"⚠️ 입력이 목표 토큰 범위(max {task.input_token_range[1]})를 넘었지만 줄일 수 없어 그대로 사용했습니다")
        for i, line in enumerate(lines[:40]):
            enqueue_op(conn, run_id=run_id, op_key=f"{root_key}:line:{i}", file_id=write_to.file_id,
                       action="insert", parent_ref=f"op:{root_key}", payload={"content": line[:1000]},
                       bot_kind="feedback_rule_item", bot_ref_id=output_id,
                       marker=marker("feedback_rule_item", f"{ref}:{i}"))
        return output_id
