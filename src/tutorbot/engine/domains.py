"""Profile-driven processing for non-study domains."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
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

logger = logging.getLogger(__name__)


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


def _is_life_incremental_source(
    domain_id: str,
    file_id: str,
    life_source_ids: set[str],
) -> bool:
    return domain_id == "life" or (
        domain_id == "career" and file_id in life_source_ids
    )


def _source_log_label(
    domain_id: str,
    file_id: str,
    profile: DomainProfileCfg,
    profiles: dict[str, DomainProfileCfg],
    life_source_ids: set[str],
) -> str:
    if domain_id == "life":
        return domain_id
    if domain_id != "career":
        return domain_id
    if _is_life_incremental_source(domain_id, file_id, life_source_ids):
        return "life"
    for source_domain in profile.source_domains:
        source_profile = profiles.get(source_domain)
        if (
            source_profile is not None
            and source_profile.enabled
            and file_id in source_profile.file_ids
        ):
            return source_domain
    return domain_id


def _source_log_labels(
    domain_id: str,
    file_ids: list[str],
    profile: DomainProfileCfg,
    profiles: dict[str, DomainProfileCfg],
    life_source_ids: set[str],
) -> list[str]:
    return list(dict.fromkeys(
        _source_log_label(domain_id, file_id, profile, profiles, life_source_ids)
        for file_id in file_ids
    ))


def _source_content(
    conn: sqlite3.Connection,
    domain_id: str,
    file_id: str,
    index: NodeIndex,
    profile: DomainProfileCfg,
    settings: Settings,
    *,
    life_incremental: bool = False,
    cutoff: datetime | None = None,
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
    if life_incremental and cutoff is not None:
        return _life_incremental_text(index, classifier.is_bot, cutoff), [], []
    return index.text_of("root", skip=classifier.is_bot), [], []


def _life_incremental_text(
    index: NodeIndex,
    is_bot: Callable[[Node], bool],
    cutoff: datetime,
) -> str:
    """Render changed life subtrees plus only their non-bot ancestor paths."""
    cutoff_ms = int(cutoff.timestamp() * 1000)
    bot_subtree: set[str] = set()

    def mark_bot(node_id: str) -> None:
        node = index.get(node_id)
        if node is None:
            return
        bot_subtree.add(node_id)
        for child_id in node.children:
            mark_bot(child_id)

    for node in index.walk():
        if node.id not in bot_subtree and is_bot(node):
            mark_bot(node.id)

    changed = {
        node.id for node in index.walk()
        if node.id not in bot_subtree
        and (node.created >= cutoff_ms or node.modified >= cutoff_ms)
    }
    if not changed:
        return ""
    top_changed = [
        node_id for node_id in changed
        if not any(ancestor.id in changed for ancestor in index.ancestors(node_id))
    ]
    relevant: set[str] = set()
    for node_id in top_changed:
        relevant.add(node_id)
        relevant.update(a.id for a in index.ancestors(node_id) if a.id not in bot_subtree)
    lines: list[str] = []

    def render(node_id: str, depth: int, subtree: bool) -> None:
        node = index.get(node_id)
        if node is None or node_id in bot_subtree:
            return
        if node.content:
            lines.append("  " * depth + node.content)
        if node.note:
            lines.extend("  " * depth + "  > " + line for line in node.note.splitlines())
        for child_id in node.children:
            if subtree or child_id in relevant:
                render(child_id, depth + 1, subtree or child_id in top_changed)

    # Render relevant ancestor chains independently, but changed roots once.
    roots = [n.id for n in index.children_of("root") if n.id in relevant]
    for root_id in roots:
        render(root_id, 0, root_id in top_changed)
    return "\n".join(lines)


def _recent_user_nodes(
    index: NodeIndex,
    is_bot: Callable[[Node], bool],
    cutoff: datetime,
    *,
    direct_only: bool = False,
) -> list[Node]:
    """Return top-level recent user nodes (or direct children of root for career)."""
    cutoff_ms = int(cutoff.timestamp() * 1000)
    changed: list[Node] = [
        node for node in index.walk()
        if not is_bot(node)
        and (node.created >= cutoff_ms or node.modified >= cutoff_ms)
    ]
    if direct_only:
        return [
            node for node in changed
            if index.parent_of.get(node.id) == "root"
        ]
    return changed


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
    run_date: str | None = None,
    bot_tag: str = "tutorbot",
) -> DomainReport:
    """Analyze changed domain inputs and enqueue feedback in the profile's own document."""
    report = DomainReport()
    run_date = run_date or clock.local_date("Asia/Seoul").isoformat()
    try:
        if date.fromisoformat(run_date).isoformat() != run_date:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("run_date must be YYYY-MM-DD") from None
    profiles = settings.domains
    cutoff = datetime.combine(
        date.fromisoformat(run_date), time.min, ZoneInfo("Asia/Seoul"),
    ) - timedelta(days=1)
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
        analyzed_source_ids: list[str] = []
        source_warnings: list[str] = []
        source_failures: list[str] = []
        failed_source_ids: list[str] = []
        pending_task_hashes: list[tuple[str, str, str]] = []
        life_profile = profiles.get("life")
        life_source_ids = set(life_profile.file_ids) if life_profile and life_profile.enabled else set()
        life_counts: dict[str, int | None] = {
            file_id: None for file_id in source_ids
            if _is_life_incremental_source(domain_id, file_id, life_source_ids)
        }
        life_contributing_sources: set[str] = set()

        def log_life_source_result(target_id: str, result: str) -> None:
            for source_id, count in life_counts.items():
                source_result = (
                    "skipped"
                    if result != "failed" and source_id not in life_contributing_sources
                    else result
                )
                logger.info(
                    "life source domain_id=%s source_id=%s target_id=%s cutoff=%s changed_nodes=%s result=%s",
                    domain_id,
                    _source_log_label(domain_id, source_id, profile, profiles, life_source_ids),
                    domain_id, cutoff.isoformat(),
                    count if count is not None else "unknown", source_result,
                )

        for file_id in source_ids:
            index = indices.get(file_id)
            if index is None:
                source_failures.append(f"{domain_id}/{file_id}: source document was not read")
                failed_source_ids.append(file_id)
                continue
            try:
                life_incremental = _is_life_incremental_source(
                    domain_id, file_id, life_source_ids,
                )
                text, warnings, task_hashes = _source_content(
                    conn, domain_id, file_id, index, profile, settings,
                    life_incremental=life_incremental, cutoff=cutoff,
                )
                if life_incremental:
                    registry_ids = {
                        row["node_id"] for row in conn.execute(
                            "SELECT node_id FROM bot_node WHERE file_id=? AND deleted_at IS NULL",
                            (file_id,),
                        )
                    }
                    classifier = Classifier(index, settings.tags, {
                        node_id: "domain_feedback" for node_id in registry_ids
                    })
                    life_counts[file_id] = sum(
                        1 for node in index.walk()
                        if not classifier.is_bot(node)
                        if node.created >= int(cutoff.timestamp() * 1000)
                        or node.modified >= int(cutoff.timestamp() * 1000)
                    )
                    if not text.strip():
                        warnings.append(
                            f"{domain_id}/{file_id}: no life changes since {cutoff.isoformat()}"
                        )
                if text.strip():
                    source_parts.append(f"## {file_id}\n{text}")
                    analyzed_source_ids.append(file_id)
                    if life_incremental:
                        life_contributing_sources.add(file_id)
                source_warnings.extend(warnings)
                pending_task_hashes.extend(task_hashes)
            except Exception as exc:
                source_failures.append(f"{domain_id}/{file_id}: {exc}")
                failed_source_ids.append(file_id)

        if source_failures:
            report.errors.extend(source_failures)
            # Include only sources whose content was successfully read and contributed
            # to this analysis; failed IDs are reported separately below.
            for file_id in target_ids:
                log_life_source_result(file_id, "failed")
                logger.warning(
                    "domain analysis failed domain_id=%s source_ids=%s failed_source_ids=%s target_id=%s reason=source_read_failed",
                    domain_id,
                    _source_log_labels(domain_id, analyzed_source_ids, profile, profiles, life_source_ids),
                    _source_log_labels(domain_id, failed_source_ids, profile, profiles, life_source_ids),
                    domain_id,
                )
                conn.execute(
                    "INSERT INTO domain_run(run_id,domain_id,file_id,status,error,created_at)"
                    " VALUES(?,?,?,'error',?,?)",
                    (run_id, domain_id, file_id, "; ".join(source_failures), clock.now_iso()),
                )
            continue

        source_text = "\n\n".join(source_parts).strip()
        # A document counts as an analysis source only when it contributes text.
        actual_source_ids = analyzed_source_ids
        report.warnings.extend(source_warnings)
        for target_id in target_ids:
            target_index = indices.get(target_id)
            if target_index is None:
                message = f"{domain_id}/{target_id}: feedback document was not read"
                report.errors.append(message)
                log_life_source_result(target_id, "failed")
                logger.warning(
                    "domain analysis failed domain_id=%s source_ids=%s target_id=%s reason=target_not_read",
                    domain_id,
                    _source_log_labels(domain_id, actual_source_ids, profile, profiles, life_source_ids),
                    domain_id,
                )
                conn.execute(
                    "INSERT INTO domain_run(run_id,domain_id,file_id,status,error,created_at)"
                    " VALUES(?,?,?,'error',?,?)",
                    (run_id, domain_id, target_id, message, clock.now_iso()),
                )
                continue
            # Empty life increments are not a source hash: otherwise the same
            # empty input is needlessly reconsidered as a normal domain run.
            if not source_text:
                message = "; ".join(source_warnings) if source_warnings else "no analyzable user records"
                report.warnings.append(f"{domain_id}/{target_id}: no analyzable user records")
                log_life_source_result(target_id, "skipped")
                logger.info(
                    "domain analysis skipped domain_id=%s source_ids=%s target_id=%s reason=no_analyzable_user_records",
                    domain_id,
                    _source_log_labels(domain_id, actual_source_ids, profile, profiles, life_source_ids),
                    domain_id,
                )
                conn.execute(
                    "INSERT INTO domain_run(run_id,domain_id,file_id,status,error,created_at)"
                    " VALUES(?,?,?,'warning',?,?)",
                    (run_id, domain_id, target_id, message, clock.now_iso()),
                )
                report.skipped += 1
                continue
            source_hash = "v1:" + hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            logger.info(
                "domain analysis started domain_id=%s source_ids=%s target_id=%s",
                domain_id,
                _source_log_labels(domain_id, actual_source_ids, profile, profiles, life_source_ids),
                domain_id,
            )
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
                log_life_source_result(target_id, "skipped")
                logger.info(
                    "domain analysis skipped domain_id=%s source_ids=%s target_id=%s reason=unchanged_source_hash",
                    domain_id,
                    _source_log_labels(domain_id, actual_source_ids, profile, profiles, life_source_ids),
                    domain_id,
                )
                continue
            try:
                result = llm.complete_json(
                    "domain_feedback",
                    _messages(domain_id, source_text, profile),
                    _validate_feedback,
                    ref=f"{domain_id}:{target_id}",
                )
            except Exception as exc:
                log_life_source_result(target_id, "failed")
                logger.warning(
                    "domain analysis failed domain_id=%s source_ids=%s target_id=%s",
                    domain_id,
                    _source_log_labels(domain_id, actual_source_ids, profile, profiles, life_source_ids),
                    domain_id,
                )
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

            logger.info(
                "domain analysis succeeded domain_id=%s source_ids=%s target_id=%s",
                domain_id,
                _source_log_labels(domain_id, actual_source_ids, profile, profiles, life_source_ids),
                domain_id,
            )
            feedback_lines = [result["summary"], *result["feedback"]]
            if result["evidence"]:
                feedback_lines.append("근거: " + "; ".join(result["evidence"]))
            marker = f"tb:domain_feedback:{domain_id}:{source_hash[3:19]}"
            target_nodes: list[Node] = []
            if domain_id == "career":
                target_nodes = _recent_user_nodes(target_index, Classifier(
                    target_index,
                    settings.tags,
                    {
                        row["node_id"]: row["kind"]
                        for row in conn.execute(
                            "SELECT node_id,kind FROM bot_node WHERE file_id=? AND deleted_at IS NULL",
                            (target_id,),
                        )
                    },
                ).is_bot, cutoff, direct_only=True)
            elif domain_id in ("work", "life"):
                source_index = indices.get(target_id)
                if source_index is not None:
                    registry = {
                        row["node_id"]: row["kind"]
                        for row in conn.execute(
                            "SELECT node_id,kind FROM bot_node WHERE file_id=? AND deleted_at IS NULL",
                            (target_id,),
                        )
                    }
                    target_nodes = _recent_user_nodes(
                        source_index,
                        Classifier(source_index, settings.tags, registry).is_bot,
                        cutoff,
                    )
            conn.execute("BEGIN")
            try:
                feedback_parents = (
                    [("node:" + node.id, node.id) for node in target_nodes]
                    if target_nodes
                    else [("node:root", None)]
                )
                summary = str(result["summary"]).strip() or f"{domain_id} 피드백"
                if len(summary) > 60:
                    summary = summary[:57].rstrip() + "..."
                concrete_lines = [str(line).strip() for line in result["feedback"] if str(line).strip()]
                if result["evidence"]:
                    concrete_lines.append("근거: " + "; ".join(str(v) for v in result["evidence"]))
                note = "\n".join(concrete_lines)
                for parent_ref, node_id in feedback_parents:
                    parent_suffix = f":{node_id}" if node_id is not None else ""
                    op_key = f"domain-feedback:{domain_id}:{target_id}:{node_id or 'root'}:{source_hash}"
                    if node_id is None:
                        # Retain the pre-existing root op-key shape for fallback feedback.
                        op_key = f"domain-feedback:{domain_id}:{target_id}:{source_hash}"
                    node_marker = (
                        f"{marker}:node:{node_id}" if node_id is not None else marker
                    )
                    enqueue_op(
                        conn,
                        run_id=run_id,
                        op_key=op_key,
                        file_id=target_id,
                        action="insert",
                        parent_ref=parent_ref,
                        idx=-1,
                        payload={
                            "content": f"🤖 {domain_id} 피드백 - {summary} !({run_date}) #{bot_tag}",
                            "note": note,
                        },
                        bot_kind="domain_feedback",
                        marker=node_marker,
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
            log_life_source_result(target_id, "succeeded")
            report.processed += 1
    return report
