"""Feedback execution and transactional outbox planning."""
from __future__ import annotations

import json
import logging
import os
import socket
import tempfile
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field, fields
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import clock
from ..llm.client import LLMError
from ..llm.schemas import FeedbackRuleOut
from ..parse.classify import Classifier
from . import output, persona, prompt, schedule, sources, store
from .models import BotWriteMark, NodeRef, TargetSnapshot, UnitPlan, UnitSpec
from .timefilter import effective_modified_ms, floor_to_second, own_text_hash

log = logging.getLogger(__name__)


@dataclass
class FeedbackStageReport:
    evaluated: int = 0
    ran: int = 0
    skipped: int = 0
    skipped_error: int = 0
    failed: int = 0
    not_due: int = 0
    already_done: int = 0
    already_running: int = 0
    not_started_docs: int = 0
    not_started_persona: int = 0
    deferred_budget: int = 0
    awaiting_approval: int = 0
    preview_unapproved: int = 0
    invalid: int = 0
    units_ok: int = 0
    units_failed: int = 0
    llm_calls: int = 0
    run_row_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def not_started(self):
        return self.not_started_docs + self.not_started_persona + self.deferred_budget

    def summary(self):
        return {**{f.name: getattr(self, f.name) for f in fields(self)
                   if f.name not in ("run_row_ids", "errors")}, "not_started": self.not_started}


def _preflight(task, indices):
    refs = [(s.file_id, s.parent_node_id) for s in (*task.selection, *task.references)]
    if task.output.placement == "under_parent":
        refs.append((task.output.file_id, task.output.parent_node_id))
    for file_id, parent in refs:
        if file_id not in indices:
            return "FR-R001"
        if indices[file_id].get(parent) is None:
            return "FR-R002"
    return None


def _budget_exhausted(llm, budget):
    stats = getattr(llm, "stats", {})
    return bool(budget and stats.get("prompt_tokens", 0) + stats.get("completion_tokens", 0) >= budget)


def run_feedback_stage(conn, settings, llm, indices, *, T, run_id, dry_run,
                       bot_tag="tutorbot", only=None, force=False, interactive=False, token_budget=None):
    rep = FeedbackStageReport()
    cfg = getattr(settings, "feedback_rules", None)
    if cfg is None:
        return rep
    issues = list(cfg.issues) + [issue for task in cfg.tasks if only is None or task.id in only
                                 for issue in task.issues if issue.disables_task]
    for issue in issues:
        if issue.severity == "error":
            rep.invalid += 1
            rep.errors.append(f"feedback config: {issue.code}")
            log.error("feedback_config_error code=%s", issue.code)
    if not cfg.enabled:
        return rep
    T = floor_to_second(T)
    store.sweep_interrupted(conn, current_run_id=run_id, now_iso=clock.now_iso())
    flags = {}
    for fid, index in indices.items():
        registry = dict(conn.execute("SELECT node_id,kind FROM bot_node WHERE file_id=? AND deleted_at IS NULL",
                                     (fid,)).fetchall())
        flags[fid] = sources.compute_bot_flags(index, Classifier(index, getattr(settings, "tags", None), registry))
    marks = store.load_bot_write_marks(conn, indices.keys())
    calls_left = cfg.max_llm_calls_per_run
    for task in cfg.valid_tasks():
        if only is not None and task.id not in only:
            continue
        rep.evaluated += 1
        decision = (schedule.ScheduleDecision("run", f"manual:{T.isoformat()}:{uuid.uuid4().hex}", None, None)
                    if force else schedule.decide(task, T, lambda key: store.has_cycle(conn, task.id, key),
                                                  run_id=run_id))
        if decision.action in ("not_due", "already_done"):
            setattr(rep, decision.action, getattr(rep, decision.action) + 1)
            continue
        prev = store.prev_started_at(conn, task.id)
        first = schedule.first_run_lower_bound(task, T).isoformat()
        base = dict(task_id=task.id, cycle_key=decision.cycle_key,
                    trigger_kind="manual" if force else ("each_run" if task.schedule.kind == "each_run" else "scheduled"),
                    schedule=task.schedule.kind,
                    scheduled_at=decision.scheduled_at.isoformat() if decision.scheduled_at else None,
                    started_at=T.isoformat(), prev_started_at=prev, window_lower=prev or first,
                    run_id=run_id, config_hash=task.config_hash())
        if decision.action == "skip":
            code = "FR-R010" if decision.skip_reason == "invalid_schedule_quarter_anchor" else "FR-R011"
            log.error("feedback_task_skipped code=%s task_index=%d", code, task.index)
            rep.errors.append(f"feedback task index {task.index}: {code}")
            row = store.insert_run(conn, **base, status="skipped", skip_reason=decision.skip_reason,
                                   finished_at=clock.now_iso())
            if row is None:
                rep.already_done += 1
            else:
                rep.skipped += 1
                rep.skipped_error += 1
            continue
        code = _preflight(task, indices)
        if code:
            log.error("feedback_gate_failed code=%s task_index=%d", code, task.index)
            rep.not_started_docs += 1
            continue
        person = persona.resolve_persona(task)
        if person.error:
            log.error("feedback_gate_failed code=FR-R003 task_index=%d", task.index)
            rep.not_started_persona += 1
            continue
        gate = persona.approval_gate(conn, task, person, interactive=interactive, dry_run=dry_run)
        if gate.preview:
            rep.preview_unapproved += 1
            print(f"⚠️ approval required (preview only): task index {task.index}")
            log.warning("feedback_gate_failed code=FR-R004 preview=1 task_index=%d", task.index)
        elif not gate.ok:
            log.warning("feedback_gate_failed code=FR-R004 reason=approval_required")
            rep.awaiting_approval += 1
            continue
        if _budget_exhausted(llm, token_budget):
            log.warning("feedback_budget_deferred code=FR-R006 reason=daily_token_budget")
            rep.deferred_budget += 1
            continue
        lock = store.try_lock(conn, task.id, now_iso=clock.now_iso(), stale_minutes=cfg.stale_lock_minutes,
                              host=socket.gethostname(), pid=os.getpid())
        if not lock.acquired:
            log.info("feedback_already_running code=FR-R005 count=1")
            rep.already_running += 1
            continue
        try:
            prev = store.prev_started_at(conn, task.id)
            base["prev_started_at"] = prev
            base["window_lower"] = prev or first
            uses_success = any(s.time.endswith("_since_last_success") for s in (*task.selection, *task.references))
            lower = store.success_window_lower(conn, task.id, task.config_hash(), first) if uses_success else None
            if lower is not None:
                base["window_lower"] = lower
            selected = sources.resolve_selection(task, indices, flags, marks, T, prev, first, success_lower=lower)
            base["persona_hash"] = person.hash
            if not selected:
                row = store.insert_run(conn, **base, status="skipped", skip_reason="empty_selection",
                                       finished_at=clock.now_iso())
                if row is None:
                    rep.already_done += 1
                else:
                    rep.skipped += 1
                continue
            if uses_success and len(selected) > cfg.max_units_per_task:
                log.warning("feedback_selection_deferred reason=success_window_cap count=%d",
                            len(selected) - cfg.max_units_per_task)
                rep.deferred_budget += 1
                continue
            plan = build_units(task, selected, cfg.max_units_per_task)
            if len(plan.items) > calls_left:
                log.warning("feedback_budget_deferred code=FR-R006 reason=call_limit")
                rep.deferred_budget += 1
                continue
            row = store.insert_run(conn, **base, status="started", selected_count=len(selected),
                                   units_total=len(plan.items), units_skipped_cap=plan.skipped_cap,
                                   consume_approval_id=None if dry_run or gate.preview else gate.approval_id)
            if row is None:
                rep.already_done += 1
                continue
            if plan.skipped_cap:
                log.warning("feedback_units_skipped code=FR-R024 count=%d", plan.skipped_cap)
            refs = sources.resolve_references(task, indices, flags, marks, T, prev, first, success_lower=lower)
            start_stats = dict(getattr(llm, "stats", {}))
            ok = failed = calls = enqueued = truncated = over = consecutive = 0
            for position, unit in enumerate(plan.items):
                if not store.heartbeat(conn, task.id, lock.token, clock.now_iso(), task_run_id=row):
                    failed += len(plan.items) - position
                    break
                if _budget_exhausted(llm, token_budget):
                    log.warning("feedback_budget_exhausted code=FR-R028 reason=daily_token_budget")
                    failed += len(plan.items) - position
                    break
                try:
                    targets = [sources.render_target(indices[s.ref.file_id], flags[s.ref.file_id], s,
                               getattr(getattr(settings, "tags", None), "goal", [])) for s in unit.inputs]
                    messages, fit = prompt.build_messages(task, person.text, unit, targets, refs, T, cfg.safety_limit(task))
                    truncated += int(fit.truncated)
                    over += int(fit.over_target)
                    if fit.over_target:
                        log.warning("feedback_input_over_target code=FR-R029 task_index=%d", task.index)
                    calls += 1
                    calls_left -= 1
                    result = llm.complete_json("feedback_rule", messages, FeedbackRuleOut.validate,
                                               ref=f"feedback:{task.id}:{unit.ref}")
                    snapshots = {}
                    with store._transaction(conn):
                        for target in unit.write_to:
                            node = indices[target.file_id].get(target.node_id)
                            snap = None if target.node_id == "root" else TargetSnapshot(
                                own_text_hash(node), effective_modified_ms(node, marks.get((target.file_id, target.node_id))))
                            output.enqueue_result(conn, task=task, task_run_id=row, run_id=run_id,
                                unit=unit, write_to=target, out=result,
                                local_date=T.astimezone(ZoneInfo(task.timezone)).date().isoformat(),
                                bot_tag=bot_tag, truncated=fit.truncated, over_target=fit.over_target,
                                snapshot=snap, selected_target=targets[0] if task.unit == "per_node" else None)
                            snapshots[target] = snap
                    enqueued += len(snapshots)
                    for target, snap in snapshots.items():
                        if snap:
                            marks[(target.file_id, target.node_id)] = BotWriteMark(snap.own_hash, snap.effective_modified_ms)
                    ok += 1
                    consecutive = 0
                except Exception as exc:
                    failed += 1
                    if isinstance(exc, prompt.InputTooLarge):
                        log.warning("feedback_input_too_large code=FR-R020 task_index=%d", task.index)
                    if isinstance(exc, LLMError):
                        log.warning("feedback_llm_failed code=FR-R021 reason=llm_error")
                        consecutive += 1
                    else:
                        consecutive = 0
                    log.warning("feedback_unit_failed code=unit_failed task_index=%d", task.index)
                    if consecutive >= 3:
                        log.error("feedback_llm_aborted code=FR-R023 reason=consecutive_llm_errors")
                        failed += len(plan.items) - position - 1
                        break
            end_stats = getattr(llm, "stats", {})
            update = dict(units_ok=ok, units_failed=failed, llm_calls=calls, outputs_enqueued=enqueued,
                          input_truncated_units=truncated, input_over_target_units=over,
                          prompt_tokens=end_stats.get("prompt_tokens", 0) - start_stats.get("prompt_tokens", 0),
                          completion_tokens=end_stats.get("completion_tokens", 0) - start_stats.get("completion_tokens", 0))
            rep.units_ok += ok
            rep.units_failed += failed
            rep.llm_calls += calls
            if failed:
                update.update(status="failed", error="unit_failed", finished_at=clock.now_iso())
                rep.failed += 1
            elif dry_run:
                update.update(status="succeeded", finished_at=clock.now_iso())
                rep.ran += 1
            else:
                rep.run_row_ids.append(row)
                rep.ran += 1
            store.update_run(conn, row, **update)
        finally:
            store.release_lock(conn, task.id, lock.token)
    return rep


def finalize_after_flush(conn, run_id, *, now_iso):
    store.sync_output_status(conn, now_iso=now_iso)
    return store.finalize_runs(conn, run_id, now_iso=now_iso)


@dataclass
class FeedbackCommandResult:
    exit_code: int
    run_id: int | None = None
    report: FeedbackStageReport | None = None
    message: str | None = None


def command_reasons(result):
    """Return only fixed counter names and numbers, never task or error text."""
    names = ("failed", "invalid", "not_started_docs", "not_started_persona",
             "skipped_error", "awaiting_approval", "deferred_budget", "already_running")
    values = result.report.summary() if result.report else {}
    reasons = [f"{name}:{values[name]}" for name in names if values.get(name, 0)]
    if result.exit_code == 1 and not values.get("failed"):
        reasons.insert(0, "execution_failed:1")
    elif result.exit_code and not reasons:
        fallback = {1: "execution_failed", 2: "invalid_options", 3: "invalid",
                    4: "awaiting_approval", 75: "lock_unavailable"}
        reasons.append(f"{fallback.get(result.exit_code, 'error')}:1")
    return ",".join(reasons) or "none"


def run_feedback_command(settings, *, tasks=None, dry_run=False, fake_llm=False, force=False,
                         at=None, interactive=False, dynalist=None, llm=None, conn=None, lock_path=None):
    """Run only feedback stages. Dry runs use an isolated SQLite backup."""
    from .. import db
    from ..dynalist.client import DynalistClient
    from ..dynalist.tree import NodeIndex
    from ..llm.client import LLMClient
    from ..pipeline import FileLock, backup_sqlite_db
    from .flush_adapter import flush_feedback_safe

    cfg = getattr(settings, "feedback_rules", None)
    if cfg is None:
        return FeedbackCommandResult(0, message="feedback_rules is disabled; nothing to do")
    valid = {task.id: task for task in cfg.valid_tasks()}
    if tasks:
        invalid = sorted(tasks - valid.keys())
        if invalid:
            return FeedbackCommandResult(3, message="unknown or invalid task")
    if any(issue.severity == "error" for issue in cfg.issues):
        return FeedbackCommandResult(3, message="feedback configuration invalid")
    if not cfg.enabled:
        return FeedbackCommandResult(0, message="feedback_rules is disabled; nothing to do")
    if (fake_llm or at is not None) and not dry_run or force and not tasks:
        return FeedbackCommandResult(2, message="invalid option combination")

    run_id = None
    report = None
    with ExitStack() as stack:
        try:
            stack.enter_context(FileLock(lock_path or Path(settings.db_path).parent / "tutorbot.lock"))
        except RuntimeError as exc:
            if isinstance(exc.__cause__, BlockingIOError):
                log.warning("tutorbot is already running")
                return FeedbackCommandResult(75, message="tutorbot is already running")
            raise
        if dry_run:
            temp = stack.enter_context(tempfile.TemporaryDirectory())
            dest = db.connect(Path(temp) / "preview.sqlite3")
            stack.callback(dest.close)
            if conn is not None:
                conn.backup(dest)
            elif Path(settings.db_path).exists():
                import sqlite3
                source = sqlite3.connect(Path(settings.db_path).resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    source.backup(dest)
                finally:
                    source.close()
            connection = dest
        elif conn is None:
            connection = db.connect(settings.db_path)
            stack.callback(connection.close)
        else:
            connection = conn
        db.migrate(connection)
        if dynalist is None:
            dynalist = DynalistClient(settings.dynalist_token())
        if llm is None:
            backend = None
            if fake_llm:
                from ..cli import _DefaultFakeLLMBackend
                backend = _DefaultFakeLLMBackend()
            llm = LLMClient(provider=settings.llm.provider, api_key="" if fake_llm else settings.llm_api_key(),
                            model_fast=settings.llm.model_fast, model_smart=settings.llm.model_smart,
                            backend=backend, requests_per_minute=settings.llm.requests_per_minute)
        run_date = clock.local_date(getattr(settings, "timezone", "Asia/Seoul")).isoformat()
        if not dry_run:
            backup_sqlite_db(settings.db_path, run_date)
        run_id = connection.execute("INSERT INTO run_log(run_date,started_at,status) VALUES(?,?,'running')",
                                    (run_date, clock.now_iso())).lastrowid
        llm.conn, llm.run_id = connection, run_id
        errors = []
        write_failure = False
        pre = None
        indices = {}
        try:
            if not dry_run:
                pre = flush_feedback_safe(connection, dynalist, redact_all=True)
                store.sync_output_status(connection, now_iso=clock.now_iso())
                if pre.failed or pre.skipped or pre.errors:
                    errors.append("pre_flush_failed")
                    write_failure = True
            T = floor_to_second(at or clock.now())
            selected_tasks = [task for task in valid.values() if tasks is None or task.id in tasks]
            file_ids = []
            for task in selected_tasks:
                file_ids.extend(s.file_id for s in (*task.selection, *task.references))
                if task.output.file_id:
                    file_ids.append(task.output.file_id)
            for fid in dict.fromkeys(file_ids):
                try:
                    indices[fid] = NodeIndex.from_doc_read(fid, dynalist.doc_read(fid))
                except Exception:
                    errors.append("doc_read_failed")
            report = run_feedback_stage(connection, settings, llm, indices, T=T, run_id=run_id,
                dry_run=dry_run, only=tasks, force=force, interactive=interactive and not dry_run,
                token_budget=getattr(settings.llm, "daily_token_budget", None),
                bot_tag=getattr(getattr(settings, "tags", None), "bot", "tutorbot"))
            if report.errors:
                errors.append("feedback_stage_reported_error")
            stats = {"feedback": report.summary()}
            if dry_run:
                stats["dry_run_pending_ops"] = connection.execute(
                    "SELECT count(*) FROM write_op WHERE status='pending'").fetchone()[0]
            else:
                written = flush_feedback_safe(connection, dynalist, indices=indices, redact_all=True)
                result = finalize_after_flush(connection, run_id, now_iso=clock.now_iso())
                if written.failed or written.skipped or written.errors:
                    errors.append("write_failed")
                    write_failure = True
                if result["failed"]:
                    report.failed += result["failed"]
                    report.ran = max(0, report.ran - result["failed"])
                stats["feedback"] = report.summary()
            status = "partial" if errors or report.failed or report.invalid or report.skipped_error else "ok"
            connection.execute(
                "UPDATE run_log SET finished_at=?,status=?,stats_json=?,error_summary=?,llm_calls=?,"
                "llm_prompt_tokens=?,llm_completion_tokens=?,dynalist_reads=?,dynalist_changes=? WHERE id=?",
                (clock.now_iso(), status, json.dumps(stats), "; ".join(errors) or None,
                 llm.stats.get("calls", 0), llm.stats.get("prompt_tokens", 0),
                 llm.stats.get("completion_tokens", 0), dynalist.stats.get("reads", 0),
                 dynalist.stats.get("changes", 0), run_id))
            code = (1 if report.failed or write_failure else 3 if report.invalid or report.not_started_docs
                    or report.not_started_persona or report.skipped_error else 4 if report.awaiting_approval else 0)
            return FeedbackCommandResult(code, run_id, report, f"Run {run_id} ({run_date}): {status}")
        except Exception:
            store.interrupt_current_runs(connection, run_id, now_iso=clock.now_iso())
            if not dry_run:
                try:
                    flush_feedback_safe(connection, dynalist, indices=indices, redact_all=True)
                except Exception:  # noqa: BLE001 - cleanup must not expose remote errors
                    log.error("feedback_cleanup_flush_failed")
                finally:
                    finalize_after_flush(connection, run_id, now_iso=clock.now_iso())
            connection.execute("UPDATE run_log SET finished_at=?,status='failed',error_summary=? WHERE id=?",
                               (clock.now_iso(), "feedback_execution_failed", run_id))
            log.error("feedback_execution_failed")
            return FeedbackCommandResult(1, run_id, report, "feedback execution failed")


def build_units(task, selected, max_units: int) -> UnitPlan:
    """Plan calls and destinations without reading or writing external state."""
    if max_units < 1:
        raise ValueError("max_units must be positive")
    if not selected:
        return UnitPlan((), 0)
    under_parent = task.output.placement == "under_parent"
    parent = NodeRef(task.output.file_id, task.output.parent_node_id) if under_parent else None
    if task.unit == "per_node":
        items = tuple(
            UnitSpec(f"{node.ref.file_id}.{node.ref.node_id}", (node,),
                     (parent if under_parent else node.ref,))
            for node in selected[:max_units]
        )
        return UnitPlan(items, max(0, len(selected) - max_units))
    if task.unit == "aggregate":
        destinations = (parent,) if under_parent else tuple(node.ref for node in selected[:max_units])
        skipped = 0 if under_parent else max(0, len(selected) - max_units)
        return UnitPlan((UnitSpec("agg", tuple(selected), destinations),), skipped)
    raise ValueError("unknown feedback unit")
