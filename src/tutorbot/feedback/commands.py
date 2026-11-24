"""Feedback CLI handlers; history reads never migrate the database."""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import clock, db
from . import persona, schedule, store
from .service import command_reasons, run_feedback_command


def _online_validate(settings, tasks, *, allow_placeholders=False):
    """Read documents and history only; emit counts and generic diagnostics."""
    from ..cli import _client
    from ..dynalist.tree import NodeIndex
    from ..parse.classify import Classifier
    from . import sources

    def placeholder(value):
        return not value or "DESIGN_ONLY" in value or "<" in value or ">" in value

    conn = _history(settings)
    history_available = conn is not None
    snapshot = None
    try:
        if conn:
            snapshot = db.connect(":memory:")
            conn.backup(snapshot)
            db.migrate(snapshot)
        client = _client(settings)
        indices = {}
        failed = set()
        for task in tasks:
            if not task.valid:
                continue
            refs = [(s.file_id, s.parent_node_id) for s in (*task.selection, *task.references)]
            if task.output.placement == "under_parent":
                refs.append((task.output.file_id, task.output.parent_node_id))
            for fid, _ in refs:
                if fid in indices or fid in failed:
                    continue
                # Placeholder values are structural examples, never remote IDs.
                if placeholder(fid):
                    failed.add(fid)
                    continue
                try:
                    indices[fid] = NodeIndex.from_doc_read(fid, client.doc_read(fid))
                except Exception:  # noqa: BLE001 - remote errors may contain private content
                    failed.add(fid)
        T = clock.now()
        result = {}
        for task in tasks:
            if not task.valid:
                continue
            refs = [(s.file_id, s.parent_node_id) for s in (*task.selection, *task.references)]
            if task.output.placement == "under_parent":
                refs.append((task.output.file_id, task.output.parent_node_id))
            errors = []
            placeholders = {fid for fid, _ in refs if placeholder(fid)}
            has_placeholders = any(placeholder(fid) or placeholder(parent) for fid, parent in refs)
            if any(fid in failed and not (allow_placeholders and fid in placeholders) for fid, _ in refs):
                errors.append("document unavailable")
            if any(fid in indices and indices[fid].get(parent) is None
                   and not (allow_placeholders and placeholder(parent)) for fid, parent in refs):
                errors.append("parent node unavailable")
            counts = []
            if not errors and not has_placeholders:
                flags = {}
                for fid, index in indices.items():
                    registry = {}
                    if snapshot:
                        registry = dict(snapshot.execute(
                            "SELECT node_id,kind FROM bot_node WHERE file_id=? AND deleted_at IS NULL",
                            (fid,)).fetchall())
                    flags[fid] = sources.compute_bot_flags(index, Classifier(index, settings.tags, registry))
                marks = store.load_bot_write_marks(snapshot, indices.keys()) if snapshot else {}
                prev = store.prev_started_at(snapshot, task.id) if snapshot else None
                first = schedule.first_run_lower_bound(task, T).isoformat()
                lower = None
                if any(s.time.endswith("_since_last_success") for s in task.selection):
                    lower = (store.success_window_lower(snapshot, task.id, task.config_hash(), first)
                             if snapshot else first)
                for source in task.selection:
                    selected = sources.resolve_selection(replace(task, selection=(source,)), indices, flags,
                                                         marks, T, prev, first, success_lower=lower)
                    counts.append(len(selected))
            result[task.index] = {"selection_counts": counts, "errors": errors}
            if has_placeholders and allow_placeholders:
                result[task.index]["warnings"] = ["placeholder documents not checked"]
        return result, history_available
    finally:
        if snapshot:
            snapshot.close()
        if conn:
            conn.close()


def _at(value, tz):
    if not value:
        return clock.now()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz))
    return parsed.astimezone(timezone.utc)


def _history(settings):
    conn = store.open_readonly(settings.db_path)
    if conn is not None and not store.has_feedback_tables(conn):
        conn.close()
        return None
    return conn


def dispatch(args, settings):
    if args.feedback_cmd == "status":
        conn = _history(settings)
        try:
            rows = store.recent_runs(conn, args.task, args.limit) if conn else []
            data = [{k: row[k] for k in ("task_id", "cycle_key", "status", "skip_reason", "started_at",
                                         "selected_count", "units_ok", "units_failed", "error")} for row in rows]
            for item in data:
                if item["error"]:
                    item["error"] = "recorded error"
            print(json.dumps(data, ensure_ascii=False) if args.json else
                  "\n".join(str(row) for row in data) if data else "no history")
            return 0
        finally:
            if conn:
                conn.close()
    cfg = settings.feedback_rules
    if args.feedback_cmd == "validate":
        def safe_issue(issue):
            return {"code": issue.code, "severity": issue.severity}

        global_issues = [safe_issue(issue) for issue in cfg.issues]
        tasks = []
        for task in cfg.tasks:
            issues = [safe_issue(issue) for issue in task.issues]
            resolved = persona.resolve_persona(task)
            if resolved.error:
                issues.append({"code": "FR-R003", "severity": "error", "path": "persona_path",
                               "message": resolved.error})
            elif resolved.text and "<작성>" in resolved.text:
                issues.append({"code": "FR-W002", "severity": "warning", "path": "persona_path",
                               "message": "persona contains a placeholder"})
            tasks.append({"index": task.index, "id": task.id, "valid": task.valid and not resolved.error,
                          "issues": issues})
        online_errors = 0
        if args.online:
            try:
                online, history_available = _online_validate(
                    settings, cfg.tasks, allow_placeholders=getattr(args, "allow_placeholders", False))
                for task in tasks:
                    if task["index"] in online:
                        task["online"] = online[task["index"]]
                        online_errors += len(task["online"]["errors"])
            except (OSError, RuntimeError, sqlite3.Error):
                online, history_available = {}, False
                online_errors += 1
            history_note = "history available" if history_available else "DB 기록 없음 → 첫 실행 하한 사용"
        issues = global_issues + [issue for task in tasks for issue in task["issues"]]
        data = {"global_issues": global_issues,
                "tasks": tasks, "summary": {"tasks": len(tasks), "valid": sum(t["valid"] for t in tasks),
                "errors": sum(i["severity"] == "error" for i in issues) + online_errors,
                "warnings": sum(i["severity"] == "warning" for i in issues)}}
        if args.online:
            data["history"] = history_note
        print(json.dumps(data, ensure_ascii=False) if args.json else str(data))
        return 3 if data["summary"]["errors"] else 0
    if args.feedback_cmd == "list":
        conn = _history(settings)
        try:
            T = _at(args.at, settings.timezone)
            result = []
            for task in cfg.tasks:
                p = persona.resolve_persona(task)
                kind = "standing" if task.persona_approval == "user_approved_before_use" else "once"
                approval = (store.find_approval(conn, task.id, p.hash, kind) if conn and p.hash else None)
                state = ("persona error" if p.error else "not required" if not p.path or
                         task.persona_approval == "always_allowed" else "approved" if approval else "not approved")
                last = store.recent_runs(conn, task.id, 1) if conn else []
                due = schedule.next_due(task, T, lambda key, task_id=task.id:
                                        bool(conn and store.has_cycle(conn, task_id, key))) \
                    if task.valid else None
                result.append({"id": task.id, "domain": task.domain, "unit": task.unit,
                               "schedule": task.schedule.kind, "timezone": task.timezone, "approval": state,
                               "last_status": last[0]["status"] if last else None,
                               "last_started_at": last[0]["started_at"] if last else None,
                               "next_due": due.isoformat() if due else None})
            print(json.dumps(result, ensure_ascii=False) if args.json else "\n".join(str(x) for x in result))
            return 0
        finally:
            if conn:
                conn.close()
    valid = {task.id: task for task in cfg.valid_tasks()}
    if args.feedback_cmd == "run":
        if args.force and not args.task or (args.fake_llm or args.at) and not args.dry_run:
            print("feedback exit=2 reasons=invalid_options:1", file=sys.stderr)
            return 2
        if args.dry_run and not args.fake_llm:
            print("주의: 실제 LLM에 선택·참조 문서 본문과 노트 및 페르소나가 전송되며 비용이 발생할 수 있습니다. "
                  f"호출 상한: {cfg.max_llm_calls_per_run}, 단위/작업 상한: {cfg.max_units_per_task}.")
            if not getattr(args, "allow_external_llm", False):
                print("전송에 동의하려면 --allow-external-llm을 지정하세요.", file=sys.stderr)
                print("feedback exit=2 reasons=invalid_options:1", file=sys.stderr)
                return 2
        try:
            at = _at(args.at, settings.timezone) if args.at else None
        except ValueError:
            print("feedback exit=2 reasons=invalid_options:1", file=sys.stderr)
            return 2
        result = run_feedback_command(settings, tasks=set(args.task) if args.task else None, force=args.force,
                                      dry_run=args.dry_run, fake_llm=args.fake_llm, at=at,
                                      interactive=sys.stdin.isatty())
        if result.message:
            print(result.message, file=sys.stderr if result.exit_code == 75 else sys.stdout)
        if result.report is not None:
            print("feedback: " + json.dumps(result.report.summary(), ensure_ascii=False))
        print(f"feedback exit={result.exit_code} reasons={command_reasons(result)}", file=sys.stderr)
        return result.exit_code
    targets = list(valid.values()) if args.all else [valid.get(args.task)]
    if targets == [None]:
        print(f"unknown or invalid task: {args.task}")
        return 3
    if not targets:
        print("no tasks to approve")
        return 0
    if not Path(settings.db_path).exists():
        print("DB not found; run 'tutorbot init-db' first", file=sys.stderr)
        return 1
    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
        for task in targets:
            if args.revoke:
                count = store.revoke_approvals(conn, task.id, clock.now_iso())
                print(f"revoked {count} approval(s)")
                continue
            p = persona.resolve_persona(task)
            if p.error:
                return 3
            if not p.path or task.persona_approval == "always_allowed":
                print("approval not required")
                continue
            if args.yes or args.hash or not sys.stdin.isatty() or not persona.confirm_persona(task, p):
                return 4
            kind = "standing" if task.persona_approval == "user_approved_before_use" else "once"
            store.grant_approval(conn, task.id, p.path, p.hash, kind, clock.now_iso())
        return 0
    finally:
        conn.close()
