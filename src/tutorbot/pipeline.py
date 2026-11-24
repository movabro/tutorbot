"""Daily batch pipeline orchestration (spec §6.2, §6.4)."""
from __future__ import annotations

import fcntl
import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import clock, ingest, render
from .dynalist.client import DynalistClient
from .dynalist.tree import NodeIndex
from .dynalist.writer import WriteReport, enqueue_op
from .engine.domains import process_domains
from .engine.dumps import process_dumps
from .engine.goals import process_goals
from .engine.progress import update_progress
from .engine.quiz import expire_attempts, grade_open_attempts, issue_daily_quizzes
from .engine.study import process_study_entries
from .engine.weekly import process_weekly_reports
from .llm.client import LLMClient


@dataclass
class RunResult:
    run_id: int
    run_date: str
    status: str  # ok | partial | failed
    stats: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class FileLock:
    def __init__(self, path: str | Path = "data/tutorbot.lock") -> None:
        self.path = Path(path)
        self._fd: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self.path, "w")
        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self._fd.close()
            self._fd = None
            raise RuntimeError(f"tutorbot is already running (lock: {self.path})") from e
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            finally:
                self._fd.close()
                self._fd = None


def _enqueue_ingest_warnings(
    conn: sqlite3.Connection,
    file_id: str,
    warnings: list[tuple[str, str, str]],
    run_id: int,
    bot_tag: str = "tutorbot",
) -> int:
    count = 0
    conn.execute("BEGIN")
    try:
        for kind, nid, reason in warnings:
            op_key = f"warn:{kind}:{file_id}:{nid}:{reason[:24]}"
            if enqueue_op(
                conn,
                run_id=run_id,
                op_key=op_key,
                file_id=file_id,
                action="insert",
                parent_ref=f"node:{nid}",
                idx=-1,
                payload={"content": render.warning_node(reason, bot_tag)},
                bot_kind="warning",
                bot_ref_id=None,
                marker=render.marker("warning", f"{kind}:{nid}"),
            ):
                count += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return count


def backup_sqlite_db(db_path: str | Path, run_date: str, keep_days: int = 7) -> Path | None:
    """Copy SQLite DB to `<db_dir>/backup/tutorbot-YYYYMMDD.sqlite3` before batch run, keeping `keep_days` (spec §13)."""
    src = Path(db_path)
    if str(db_path) == ":memory:" or not src.exists():
        return None
    backup_dir = src.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ymd = run_date.replace("-", "")
    dest = backup_dir / f"tutorbot-{ymd}.sqlite3"
    shutil.copy2(src, dest)
    backups = sorted(backup_dir.glob("tutorbot-*.sqlite3"))
    if len(backups) > keep_days:
        for old in backups[: len(backups) - keep_days]:
            try:
                old.unlink()
            except OSError:
                pass
    return dest


class _DomainErrorConnection:
    """Redact domain error values before SQLite receives them.

    The legacy domain engine binds the diagnostic immediately before its
    timestamp in its error INSERTs. Fail closed if that contract changes.
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith(("update domain_run ", "update domain_state ",
                                  "replace into domain_run", "replace into domain_state")):
            raise RuntimeError("unsupported domain persistence")
        if normalized.startswith(("insert into domain_run(", "insert into domain_state(")):
            if "'error'" in normalized:
                columns = normalized.split("(", 1)[1].split(")", 1)[0].split(",")
                error_column = "last_error" if "domain_state(" in normalized else "error"
                compact = normalized.replace(" ", "")
                shapes = ("values(?,?,?,'error',?,?)", "values(?,'devlog',?,'error',?,?)",
                          "values(?,?,?,'error',?,?,?)", "values(?,'devlog',?,'error',?,?,?)")
                if (columns[-2:] not in ([error_column, "updated_at"], [error_column, "created_at"])
                        or not any(shape in compact for shape in shapes)
                        or not isinstance(parameters, (tuple, list)) or len(parameters) < 2):
                    raise RuntimeError("unsupported domain error persistence")
                parameters = (*parameters[:-2], "domain_processing_failed", parameters[-1])
            elif "'processed'" not in normalized and "'warning'" not in normalized:
                # The success path may also retain ordinary source warnings.
                success_sql = ("insertintodomain_run(run_id,domain_id,file_id,status,source_hash,error,created_at)"
                               "values(?,?,?,?,?,?,?)")
                if (normalized.replace(" ", "") != success_sql or len(parameters) != 7
                        or parameters[3] not in ("processed", "warning")
                        or parameters[3] == "processed" and parameters[5] is not None):
                    raise RuntimeError("unsupported domain persistence")
        return self._conn.execute(sql, parameters)

    def executemany(self, sql, parameters):
        normalized = " ".join(sql.lower().split())
        if "domain_run" in normalized or "domain_state" in normalized:
            raise RuntimeError("unsupported domain persistence")
        return self._conn.executemany(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def run_pipeline(
    conn: sqlite3.Connection,
    settings: Any,
    dynalist: DynalistClient,
    llm: LLMClient,
    *,
    run_date: str | None = None,
    dry_run: bool = False,
    lock_path: str | Path | None = None,
    feedback: bool = True,
) -> RunResult:
    """Execute the daily batch pipeline (stages 0..14)."""
    if dry_run:
        # A caller may supply the live connection directly. Never let a preview
        # mutate it, including on early return or exception.
        if conn.in_transaction:
            raise ValueError("dry-run requires a connection without an open transaction")
        from . import db
        preview = db.connect(":memory:")
        old_conn, old_run_id = llm.conn, llm.run_id
        old_stats = dict(llm.stats)
        try:
            conn.backup(preview)
            return _run_pipeline(preview, settings, dynalist, llm, run_date=run_date,
                                 dry_run=True, lock_path=lock_path, feedback=feedback)
        finally:
            llm.conn, llm.run_id = old_conn, old_run_id
            llm.stats.clear()
            llm.stats.update(old_stats)
            preview.close()
    return _run_pipeline(conn, settings, dynalist, llm, run_date=run_date,
                         dry_run=False, lock_path=lock_path, feedback=feedback)


def _run_pipeline(conn, settings, dynalist, llm, *, run_date, dry_run, lock_path, feedback):
    tz_name = getattr(settings, "timezone", "Asia/Seoul")
    r_date = run_date or clock.local_date(tz_name).isoformat()
    bot_tag = getattr(getattr(settings, "tags", None), "bot", "tutorbot")
    profiles = getattr(settings, "domains", {})
    study_profile = profiles.get("study") if isinstance(profiles, dict) else None
    configured_study_ids = list(getattr(study_profile, "file_ids", []))
    study_file_ids = configured_study_ids if getattr(study_profile, "enabled", True) else []
    file_ids = list(study_file_ids)
    if isinstance(profiles, dict):
        for domain_id, profile in profiles.items():
            if domain_id == "study" or not getattr(profile, "enabled", False):
                continue
            file_ids.extend(getattr(profile, "file_ids", []))
            if domain_id == "career":
                for source_domain in getattr(profile, "source_domains", []):
                    source_profile = profiles.get(source_domain)
                    if source_profile is not None and getattr(source_profile, "enabled", False):
                        file_ids.extend(getattr(source_profile, "file_ids", []))
    base_file_ids = list(dict.fromkeys(file_ids))
    rules = getattr(settings, "feedback_rules", None) if feedback else None
    fallback_error = bool(rules and any(issue.code == "FR-G009" and issue.severity == "error"
                                        for issue in rules.issues))
    feedback_enabled = bool(rules and not fallback_error and rules.enabled and (rules.tasks or rules.issues))
    if feedback_enabled:
        file_ids.extend(rules.file_ids())
    file_ids = list(dict.fromkeys(file_ids))

    lpath = lock_path or (Path(getattr(settings, "db_path", "data/tutorbot.sqlite3")).parent / "tutorbot.lock")
    with FileLock(lpath):
        if not dry_run:
            backup_sqlite_db(getattr(settings, "db_path", "data/tutorbot.sqlite3"), r_date)

        started_at = clock.now_iso()
        cur = conn.execute(
            "INSERT INTO run_log(run_date,started_at,status) VALUES(?,?,'running')",
            (r_date, started_at),
        )
        run_id = int(cur.lastrowid)
        llm.conn = conn
        llm.run_id = run_id

        errors: list[str] = []
        stats: dict[str, Any] = {}
        if fallback_error:
            logging.getLogger(__name__).error("feedback_config_error code=FR-G009")
            errors.append("feedback_config_error:FR-G009")
        from .feedback import store as feedback_store
        from .feedback.flush_adapter import flush_feedback_safe

        # Stage 1: Flush residual write_ops from previous runs
        pre_flush = WriteReport()
        if not dry_run:
            sensitive_flush = feedback_enabled or bool(conn.execute(
                "SELECT 1 FROM write_op WHERE bot_kind IN ('feedback_rule','feedback_rule_item') "
                "AND status IN ('pending','failed') LIMIT 1").fetchone())
            pre_flush = flush_feedback_safe(conn, dynalist, redact_all=sensitive_flush)
            feedback_store.sync_output_status(conn, now_iso=clock.now_iso())
            stats["pre_flush"] = {
                "done": pre_flush.done,
                "reconciled": pre_flush.reconciled,
                "skipped": pre_flush.skipped,
                "failed": pre_flush.failed,
            }
            if pre_flush.errors:
                errors.append("pre_flush_failed")

        # Stage 2: Read documents -> NodeIndex
        if feedback_enabled:
            from .feedback.timefilter import floor_to_second
            feedback_T = floor_to_second(clock.now())
        indices: dict[str, NodeIndex] = {}
        for fid in file_ids:
            try:
                indices[fid] = NodeIndex.from_doc_read(fid, dynalist.doc_read(fid))
            except Exception:  # noqa: BLE001 - remote errors may contain private content
                errors.append("doc_read_failed")
        study_indices = {fid: indices[fid] for fid in study_file_ids if fid in indices}

        if base_file_ids and not any(fid in indices for fid in base_file_ids):
            # All documents failed to read -> run=failed
            finished_at = clock.now_iso()
            conn.execute(
                "UPDATE run_log SET finished_at=?, status='failed', error_summary=?, dynalist_reads=? WHERE id=?",
                (finished_at, "; ".join(errors), dynalist.stats["reads"], run_id),
            )
            return RunResult(run_id=run_id, run_date=r_date, status="failed", stats=stats, errors=errors)

        # Stages 3 & 4: Registry reconciliation + Parsing/Ingest
        ingest_rep = ingest.IngestReport()
        for fid, idx_obj in study_indices.items():
            conn.execute("BEGIN")
            try:
                before_w = len(ingest_rep.warnings)
                ingest.ingest_index(conn, idx_obj, settings, ingest_rep)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            new_warnings = ingest_rep.warnings[before_w:]
            if new_warnings:
                _enqueue_ingest_warnings(conn, fid, new_warnings, run_id, bot_tag)
        stats["ingest"] = ingest_rep.summary()

        # Stage 5: Goals & Curriculum (T1)
        goal_stats = process_goals(conn, study_indices, llm, r_date, run_id=run_id, bot_tag=bot_tag)
        stats["goals"] = goal_stats
        if goal_stats["errors"]:
            errors.append(f"goal curriculum errors: {goal_stats['errors']}")

        proc_cfg = getattr(settings, "processing", None)
        priority_quiz = bool(getattr(proc_cfg, "priority_quiz", True))
        llm_cfg = getattr(settings, "llm", None)
        idx_thresh = int(getattr(llm_cfg, "index_full_threshold", 150))
        token_budget = int(getattr(llm_cfg, "daily_token_budget", 300000))

        def _run_quiz_stages() -> tuple[dict, dict, dict]:
            g_st = grade_open_attempts(conn, llm, settings, r_date, run_id=run_id)
            e_st = expire_attempts(conn, study_indices, r_date, run_id=run_id)
            i_st = issue_daily_quizzes(conn, settings, r_date, run_id=run_id)
            stats["quiz_grade"] = g_st
            stats["quiz_expire"] = e_st
            stats["quiz_issue"] = i_st
            if g_st["errors"]:
                errors.append(f"quiz grade errors: {g_st['errors']}")
            return g_st, e_st, i_st

        def _run_dump_and_study_stages() -> tuple[dict, dict]:
            d_st = process_dumps(
                conn,
                llm,
                run_id=run_id,
                index_full_threshold=idx_thresh,
                daily_token_budget=token_budget,
                indices=study_indices,
                run_date=r_date,
                bot_tag=bot_tag,
            )
            stats["dumps"] = d_st
            if d_st["errors"]:
                errors.append(f"dump evaluation errors: {d_st['errors']}")

            s_st = process_study_entries(conn, study_indices, llm, settings, r_date, run_id=run_id)
            stats["study"] = s_st
            if s_st["errors"]:
                errors.append(f"study entry errors: {s_st['errors']}")
            return d_st, s_st

        if priority_quiz:
            # Default order (spec §6.4): 5.goals -> 8.grade -> 9.expire -> 10.issue -> 6.dumps -> 7.study
            grade_stats, _, _ = _run_quiz_stages()
            dump_stats, study_stats = _run_dump_and_study_stages()
        else:
            dump_stats, study_stats = _run_dump_and_study_stages()
            grade_stats, _, _ = _run_quiz_stages()

        # Stage 11: Progress update (concept node rendering, curriculum_root title, progress_report)
        prog_stats = update_progress(conn, study_indices, r_date, run_id=run_id, bot_tag=bot_tag)
        stats["progress"] = prog_stats

        # Stage 12: Weekly report (T5) & Learner profile update (T6)
        weekly_stats = process_weekly_reports(
            conn,
            llm,
            r_date,
            run_id=run_id,
            bot_tag=bot_tag,
            daily_token_budget=token_budget,
        )
        stats["weekly"] = weekly_stats
        if weekly_stats["errors"]:
            errors.append(f"weekly report errors: {weekly_stats['errors']}")

        domain_stats = {"processed": 0, "skipped": 0, "warnings": 0, "errors": 0}
        has_configured_domains = isinstance(profiles, dict) and any(
            domain_id != "study"
            and getattr(profile, "enabled", False)
            and getattr(profile, "file_ids", [])
            for domain_id, profile in profiles.items()
        )
        if has_configured_domains:
            domain_report = process_domains(
                _DomainErrorConnection(conn), indices, settings, llm,
                run_id=run_id, run_date=r_date, bot_tag=bot_tag,
            )
            domain_stats = domain_report.summary()
            stats["domains"] = domain_stats
            if domain_report.errors:
                errors.append("domain_processing_failed")

        feedback_rep = None
        if feedback_enabled:
            try:
                from .feedback.service import run_feedback_stage
                feedback_rep = run_feedback_stage(conn, settings, llm, indices, T=feedback_T,
                    run_id=run_id, dry_run=dry_run, bot_tag=bot_tag, token_budget=token_budget)
                stats["feedback"] = feedback_rep.summary()
                if feedback_rep.errors:
                    errors.append("feedback_stage_reported_error")
            except Exception:  # noqa: BLE001 - isolate the feedback stage
                logging.getLogger(__name__).error("feedback_stage_failed")
                errors.append("feedback_stage_failed")
                feedback_store.interrupt_current_runs(conn, run_id, now_iso=clock.now_iso())

        # Stage 13: Write (Outbox execution)
        if dry_run:
            pending_ops = conn.execute(
                "SELECT op_key, action, parent_ref, target_ref, payload_json FROM write_op WHERE status='pending' ORDER BY seq ASC"
            ).fetchall()
            stats["dry_run_pending_ops"] = [dict(r) for r in pending_ops]
            write_rep = WriteReport()
        else:
            sensitive_flush = feedback_enabled or bool(conn.execute(
                "SELECT 1 FROM write_op WHERE bot_kind IN ('feedback_rule','feedback_rule_item') "
                "AND status IN ('pending','failed') LIMIT 1").fetchone())
            from .feedback.service import finalize_after_flush
            try:
                write_rep = flush_feedback_safe(conn, dynalist, indices=indices, redact_all=sensitive_flush)
            except Exception:
                feedback_store.interrupt_current_runs(conn, run_id, now_iso=clock.now_iso())
                raise
            finally:
                finalized = finalize_after_flush(conn, run_id, now_iso=clock.now_iso())
            stats["write"] = {
                "done": write_rep.done,
                "reconciled": write_rep.reconciled,
                "skipped": write_rep.skipped,
                "failed": write_rep.failed,
                "changes_sent": write_rep.changes_sent,
            }
            if write_rep.errors:
                errors.append("write_failed")
            if feedback_rep is not None:
                feedback_rep.failed += finalized["failed"]
                feedback_rep.ran = max(0, feedback_rep.ran - finalized["failed"])
                stats["feedback"] = feedback_rep.summary()

        # Stage 14: Finish run_log
        has_partial = (
            bool(errors)
            or goal_stats["errors"] > 0
            or dump_stats["errors"] > 0
            or study_stats["errors"] > 0
            or grade_stats["errors"] > 0
            or weekly_stats["errors"] > 0
            or domain_stats["errors"] > 0
            or pre_flush.skipped > 0
            or pre_flush.failed > 0
            or write_rep.skipped > 0
            or write_rep.failed > 0
            or bool(feedback_rep and (feedback_rep.failed or feedback_rep.invalid or feedback_rep.skipped_error))
        )
        final_status = "partial" if has_partial else "ok"
        finished_at = clock.now_iso()
        conn.execute(
            "UPDATE run_log SET finished_at=?, status=?, stats_json=?, error_summary=?,"
            " llm_calls=?, llm_prompt_tokens=?, llm_completion_tokens=?,"
            " dynalist_reads=?, dynalist_changes=? WHERE id=?",
            (
                finished_at,
                final_status,
                json.dumps(stats, ensure_ascii=False),
                "; ".join(errors) if errors else None,
                llm.stats["calls"],
                llm.stats["prompt_tokens"],
                llm.stats["completion_tokens"],
                dynalist.stats["reads"],
                dynalist.stats["changes"],
                run_id,
            ),
        )
        return RunResult(run_id=run_id, run_date=r_date, status=final_status, stats=stats, errors=errors)


def save_devlog_run_log(
    conn: sqlite3.Connection,
    settings: Any,
    dynalist: DynalistClient,
    *,
    run_id: int,
    run_date: str,
    run_time: str,
    log_text: str,
) -> WriteReport:
    """Enqueue and flush the log under the same global lock as the pipeline."""
    lpath = Path(getattr(settings, "db_path", "data/tutorbot.sqlite3")).parent / "tutorbot.lock"
    with FileLock(lpath):
        return _save_devlog_run_log_locked(conn, settings, dynalist, run_id=run_id,
                                           run_date=run_date, run_time=run_time, log_text=log_text)


def _save_devlog_run_log_locked(conn, settings, dynalist, *, run_id, run_date, run_time, log_text):
    from .dynalist.writer import enqueue_op
    from .feedback.flush_adapter import flush_feedback_safe

    profile = getattr(settings, "domains", {}).get("devlog")
    if profile is None or not getattr(profile, "enabled", False):
        return WriteReport()

    for file_id in dict.fromkeys(getattr(profile, "file_ids", [])):
        index = NodeIndex.from_doc_read(file_id, dynalist.doc_read(file_id))
        date_content = f"!({run_date})"
        date_key = f"devlog-run-date:{file_id}:{run_date}"
        date_marker = f"tb:devlog_log_date:{file_id}:{run_date}"
        registered = {
            row["node_id"]: row["kind"]
            for row in conn.execute(
                "SELECT node_id,kind FROM bot_node WHERE file_id=? AND deleted_at IS NULL",
                (file_id,),
            )
        }
        date_node = next(
            (
                node for node in index.walk()
                if node.content == date_content
                and (
                    registered.get(node.id) == "devlog_log_date"
                    or ((node.note or "").lstrip().splitlines() or [""])[0].strip() == date_marker
                )
            ),
            None,
        )
        conn.execute("BEGIN")
        try:
            if date_node is not None:
                date_parent_ref = f"node:{date_node.id}"
            else:
                enqueue_op(
                    conn,
                    run_id=run_id,
                    op_key=date_key,
                    file_id=file_id,
                    action="insert",
                    parent_ref="node:root",
                    idx=-1,
                    payload={"content": date_content},
                    bot_kind="devlog_log_date",
                    marker=date_marker,
                )
                date_parent_ref = f"op:{date_key}"
            run_marker = f"tb:devlog_log_time:{file_id}:{run_date}:{run_time}"
            enqueue_op(
                conn,
                run_id=run_id,
                op_key=f"devlog-run-log:{file_id}:{run_date}:{run_time}",
                file_id=file_id,
                action="insert",
                parent_ref=date_parent_ref,
                idx=-1,
                payload={"content": run_time, "note": log_text},
                bot_kind="devlog_log_time",
                marker=run_marker,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return flush_feedback_safe(conn, dynalist, redact_all=True)
