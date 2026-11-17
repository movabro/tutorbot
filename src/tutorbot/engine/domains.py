"""Profile-driven processing for non-study domains."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import clock
from ..config import DomainProfileCfg, Settings
from ..dynalist.tree import Node, NodeIndex
from ..dynalist.writer import enqueue_op
from ..llm.client import LLMClient, load_prompt
from ..llm.schemas import SchemaValidationError
from ..parse.classify import Classifier
from ..parse.personal_tasks import parse_tracked_tasks
from ..parse.tags import strip_tags


@dataclass
class DomainReport:
    processed: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "skipped": self.skipped,
            "warnings": len(self.warnings),
            "errors": len(self.errors),
        }


def _validate_feedback(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SchemaValidationError("domain feedback must be a JSON object")
    summary = data.get("summary")
    feedback = data.get("feedback")
    evidence = data.get("evidence")
    if not isinstance(summary, str) or not summary.strip():
        raise SchemaValidationError("domain feedback summary must be non-empty")
    if not isinstance(feedback, list) or not isinstance(evidence, list):
        raise SchemaValidationError("domain feedback and evidence must be lists")
    if any(not isinstance(item, str) for item in [*feedback, *evidence]):
        raise SchemaValidationError("domain feedback and evidence items must be strings")
    return {
        "summary": summary.strip()[:600],
        "feedback": [str(item).strip()[:240] for item in feedback if str(item).strip()][:5],
        "evidence": [str(item).strip()[:240] for item in evidence if str(item).strip()][:5],
    }


def _personal_tasks(
    conn: sqlite3.Connection,
    file_id: str,
    index: NodeIndex,
    profile: DomainProfileCfg,
    skip: Callable[[Node], bool] | None = None,
) -> tuple[str, list[str], list[tuple[str, str, str]]]:
    tracked, diagnostics = parse_tracked_tasks(
        index,
        tracking_tag=tuple(dict.fromkeys([profile.tracking_tag, *profile.tracking_tag_aliases])),
        status_names=tuple(profile.status_nodes),
        skip=skip,
    )
    warnings = [f"{file_id}/{node_id}: {message}" for node_id, message in diagnostics]
    now = clock.now_iso()
    blocks: list[str] = []
    pending_hashes: list[tuple[str, str, str]] = []

    for task in tracked:
        node = task.node
        task_hash = hashlib.sha256()
        task_hash.update(strip_tags(node.content).encode("utf-8"))
        task_hash.update((node.note or "").encode("utf-8"))
        task_hash.update((task.status or "unknown").encode("utf-8"))
        task_hash.update(json.dumps(task.category_path, ensure_ascii=False).encode("utf-8"))
        for record in task.progress:
            task_hash.update(record.content_hash.encode("utf-8"))
        task_hash_value = "v1:" + task_hash.hexdigest()

        old = conn.execute(
            "SELECT status, status_transition, analyzed_hash FROM personal_task "
            "WHERE file_id=? AND node_id=?",
            (file_id, node.id),
        ).fetchone()
        initial = old is None
        old_status = old["status"] if old else None
        transition = int(old["status_transition"]) if old else 0
        if initial:
            transition = 1
        elif old_status != task.status:
            transition += 1

        if task.status == "Completed" and (initial or old_status != "Completed"):
            conn.execute(
                "INSERT OR IGNORE INTO completion_event("
                "file_id,node_id,transition_id,observed_at,completed_at,initial_discovery,"
                "category_path_json,source_hash) VALUES(?,?,?,?,?,?,?,?)",
                (
                    file_id, node.id, transition, now, None if initial else now,
                    int(initial), json.dumps(task.category_path, ensure_ascii=False), task_hash_value,
                ),
            )

        conn.execute(
            "INSERT INTO personal_task(file_id,node_id,status,status_transition,content_hash,updated_at)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(file_id,node_id) DO UPDATE SET "
            "status=excluded.status,status_transition=excluded.status_transition,"
            "content_hash=excluded.content_hash,updated_at=excluded.updated_at",
            (file_id, node.id, task.status, transition, task_hash_value, now),
        )
        for record in task.progress:
            conn.execute(
                "INSERT INTO personal_task_progress(file_id,task_node_id,record_node_id,date_text,"
                "entry_date,subtree_hash,detail_paths_json,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(file_id,task_node_id,record_node_id) DO UPDATE SET "
                "date_text=excluded.date_text,entry_date=excluded.entry_date,"
                "subtree_hash=excluded.subtree_hash,detail_paths_json=excluded.detail_paths_json,"
                "updated_at=excluded.updated_at",
                (
                    file_id, node.id, record.node_id, record.date_text,
                    record.entry_date.isoformat() if record.entry_date else None,
                    record.content_hash, json.dumps(record.detail_paths, ensure_ascii=False), now,
                ),
            )
        current_records = {record.node_id for record in task.progress}
        stored_records = conn.execute(
            "SELECT record_node_id FROM personal_task_progress WHERE file_id=? AND task_node_id=?",
            (file_id, node.id),
        ).fetchall()
        removed_records = [
            row["record_node_id"] for row in stored_records
            if row["record_node_id"] not in current_records
        ]
        if removed_records:
            conn.executemany(
                "DELETE FROM personal_task_progress WHERE file_id=? AND task_node_id=? AND record_node_id=?",
                [(file_id, node.id, record_id) for record_id in removed_records],
            )
        eligible = (
            task.status in ("InProgress", "FastTrack")
            or (task.status == "Completed" and bool(task.progress))
            or (task.status == "Backlog" and bool(task.progress))
        )
        if eligible and (old is None or old["analyzed_hash"] != task_hash_value):
            detail = []
            for record in task.progress:
                paths = "\n".join(f"  - {path}" for path in record.detail_paths)
                detail.append(
                    f"  진행: {record.content} | 날짜: {record.date_text or '미확인'}"
                    + (f"\n{paths}" if paths else "")
                )
            blocks.append(
                f"[{task.status}] {strip_tags(node.content)}"
                + (f"\n  설명: {node.note}" if node.note else "")
                + (f"\n  완료 분류: {' > '.join(task.category_path)}" if task.category_path else "")
                + ("\n" + "\n".join(detail) if detail else "")
            )
            pending_hashes.append((file_id, node.id, task_hash_value))

    # Keep task identity and transition history; disappearance is not an implicit state change.
    return "\n\n".join(blocks), warnings, pending_hashes


def _profile_sources(
    domain_id: str,
    profile: DomainProfileCfg,
    profiles: dict[str, DomainProfileCfg],
) -> list[str]:
    if domain_id != "career":
        return list(dict.fromkeys(profile.file_ids))
    sources: list[str] = []
    for source_domain in profile.source_domains:
        source_profile = profiles.get(source_domain)
        if source_profile is not None and source_profile.enabled:
            sources.extend(source_profile.file_ids)
    return list(dict.fromkeys(sources))


def _source_content(
    conn: sqlite3.Connection,
    domain_id: str,
    file_id: str,
    index: NodeIndex,
    profile: DomainProfileCfg,
    settings: Settings,
) -> tuple[str, list[str], list[tuple[str, str, str]]]:
    registry = {
        row["node_id"]: row["kind"]
        for row in conn.execute(
            "SELECT node_id,kind FROM bot_node WHERE file_id=? AND deleted_at IS NULL",
            (file_id,),
        )
    }
    classifier = Classifier(index, settings.tags, registry)
    if domain_id == "personal_tasks":
        text, warnings, pending_hashes = _personal_tasks(
            conn, file_id, index, profile, classifier.is_bot,
        )
        return text, warnings, pending_hashes
    return index.text_of("root", skip=classifier.is_bot), [], []


def _messages(
    domain_id: str,
    source_text: str,
    profile: DomainProfileCfg,
) -> list[dict[str, str]]:
    prompt = load_prompt("domain_feedback")
    if not prompt.strip():
        raise RuntimeError("domain_feedback prompt is missing or empty")
    additions = [*profile.rules]
    if profile.tags:
        tags = ", ".join(f"#{tag.lstrip('#')}" for tag in profile.tags)
        additions.append(f"프로필 관련 태그: {tags}")
    if profile.prompt.strip():
        additions.append(profile.prompt.strip())
    if additions:
        prompt += "\n\n프로필별 추가 지침:\n" + "\n".join(f"- {item}" for item in additions)
    return [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": f"도메인: {domain_id}\n기록:\n{source_text}",
        },
    ]


def process_domains(
    conn: sqlite3.Connection,
    indices: dict[str, NodeIndex],
    settings: Settings,
    llm: LLMClient,
    *,
    run_id: int,
    bot_tag: str = "tutorbot",
) -> DomainReport:
    """Analyze changed domain inputs and enqueue feedback in the profile's own document."""
    report = DomainReport()
    profiles = settings.domains
    for domain_id, profile in profiles.items():
        if domain_id == "study" or not profile.enabled or not profile.file_ids:
            continue
        target_ids = list(dict.fromkeys(profile.file_ids))
        source_ids = _profile_sources(domain_id, profile, profiles)
        if not source_ids:
            message = f"{domain_id}: no configured source documents"
            report.warnings.append(message)
            for file_id in target_ids:
                conn.execute(
                    "INSERT INTO domain_run(run_id,domain_id,file_id,status,error,created_at)"
                    " VALUES(?,?,?,'warning',?,?)",
                    (run_id, domain_id, file_id, message, clock.now_iso()),
                )
            continue

        source_parts: list[str] = []
        source_warnings: list[str] = []
        source_failures: list[str] = []
        pending_task_hashes: list[tuple[str, str, str]] = []
        for file_id in source_ids:
            index = indices.get(file_id)
            if index is None:
                source_failures.append(f"{domain_id}/{file_id}: source document was not read")
                continue
            try:
                text, warnings, task_hashes = _source_content(
                    conn, domain_id, file_id, index, profile, settings,
                )
                if text.strip():
                    source_parts.append(f"## {file_id}\n{text}")
                source_warnings.extend(warnings)
                pending_task_hashes.extend(task_hashes)
            except Exception as exc:
                source_failures.append(f"{domain_id}/{file_id}: {exc}")

        if source_failures:
            report.errors.extend(source_failures)
            for file_id in target_ids:
                conn.execute(
                    "INSERT INTO domain_run(run_id,domain_id,file_id,status,error,created_at)"
                    " VALUES(?,?,?,'error',?,?)",
                    (run_id, domain_id, file_id, "; ".join(source_failures), clock.now_iso()),
                )
            continue

        source_text = "\n\n".join(source_parts).strip()
        source_hash = "v1:" + hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        report.warnings.extend(source_warnings)
        for target_id in target_ids:
            target_index = indices.get(target_id)
            if target_index is None:
                message = f"{domain_id}/{target_id}: feedback document was not read"
                report.errors.append(message)
                conn.execute(
                    "INSERT INTO domain_run(run_id,domain_id,file_id,status,source_hash,error,created_at)"
                    " VALUES(?,?,?,'error',?,?,?)",
                    (run_id, domain_id, target_id, source_hash, message, clock.now_iso()),
                )
                continue
            previous = conn.execute(
                "SELECT source_hash,status FROM domain_state WHERE domain_id=? AND file_id=?",
                (domain_id, target_id),
            ).fetchone()
            if (
                previous is not None
                and previous["source_hash"] == source_hash
                and previous["status"] == "processed"
            ):
                report.skipped += 1
                continue
            if not source_text:
                report.warnings.append(f"{domain_id}/{target_id}: no analyzable user records")
                conn.execute(
                    "INSERT INTO domain_run(run_id,domain_id,file_id,status,source_hash,error,created_at)"
                    " VALUES(?,?,?,'warning',?,?,?)",
                    (
                        run_id, domain_id, target_id, source_hash,
                        "; ".join(source_warnings) if source_warnings else "no analyzable user records",
                        clock.now_iso(),
                    ),
                )
                report.skipped += 1
                continue

            try:
                result = llm.complete_json(
                    "domain_feedback",
                    _messages(domain_id, source_text, profile),
                    _validate_feedback,
                    ref=f"{domain_id}:{target_id}",
                )
            except Exception as exc:
                message = f"{domain_id}/{target_id}: analysis failed: {exc}"
                report.errors.append(message)
                conn.execute(
                    "INSERT INTO domain_run(run_id,domain_id,file_id,status,source_hash,error,created_at)"
                    " VALUES(?,?,?,'error',?,?,?)",
                    (run_id, domain_id, target_id, source_hash, str(exc), clock.now_iso()),
                )
                conn.execute(
                    "INSERT INTO domain_state(domain_id,file_id,source_hash,status,last_error,updated_at)"
                    " VALUES(?,?,?,'error',?,?) ON CONFLICT(domain_id,file_id) DO UPDATE SET "
                    "status='error',last_error=excluded.last_error,updated_at=excluded.updated_at",
                    (domain_id, target_id, source_hash, str(exc), clock.now_iso()),
                )
                continue

            feedback_lines = [result["summary"], *result["feedback"]]
            if result["evidence"]:
                feedback_lines.append("근거: " + "; ".join(result["evidence"]))
            marker = f"tb:domain_feedback:{domain_id}:{source_hash[3:19]}"
            op_key = f"domain-feedback:{domain_id}:{target_id}:{source_hash}"
            conn.execute("BEGIN")
            try:
                enqueue_op(
                    conn,
                    run_id=run_id,
                    op_key=op_key,
                    file_id=target_id,
                    action="insert",
                    parent_ref="node:root",
                    idx=-1,
                    payload={"content": f"🤖 {domain_id} 피드백 #{bot_tag}\n" + "\n".join(feedback_lines)},
                    bot_kind="domain_feedback",
                    marker=marker,
                )
                conn.execute(
                    "INSERT INTO domain_state(domain_id,file_id,source_hash,feedback_json,status,last_error,updated_at)"
                    " VALUES(?,?,?,?,'processed',NULL,?) ON CONFLICT(domain_id,file_id) DO UPDATE SET "
                    "source_hash=excluded.source_hash,feedback_json=excluded.feedback_json,"
                    "status='processed',last_error=NULL,updated_at=excluded.updated_at",
                    (domain_id, target_id, source_hash, json.dumps(result, ensure_ascii=False), clock.now_iso()),
                )
                for task_file_id, task_node_id, task_hash in pending_task_hashes:
                    conn.execute(
                        "UPDATE personal_task SET analyzed_hash=? WHERE file_id=? AND node_id=?",
                        (task_hash, task_file_id, task_node_id),
                    )
                conn.execute(
                    "INSERT INTO domain_run(run_id,domain_id,file_id,status,source_hash,error,created_at)"
                    " VALUES(?,?,?, ?,?,?,?)",
                    (
                        run_id, domain_id, target_id,
                        "warning" if source_warnings else "processed",
                        source_hash,
                        "; ".join(source_warnings) if source_warnings else None,
                        clock.now_iso(),
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            report.processed += 1
    return report
