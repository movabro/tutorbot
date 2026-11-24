"""tutorbot CLI: init-db | doctor | run | stats (spec §11, §14)."""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import load_settings
from .dynalist.client import DynalistClient, DynalistError
from .dynalist.tree import NodeIndex
from .llm.client import LLMClient, LLMError
from .pipeline import run_pipeline, save_devlog_run_log


class _SafeLogFilter(logging.Filter):
    """Keep legacy component messages from exposing remote or model content."""

    def filter(self, record):
        if record.name.startswith(("tutorbot.parse", "tutorbot.engine", "tutorbot.dynalist",
                                   "tutorbot.llm")):
            record.msg = "Component event"
            record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def _configure_logging(settings=None) -> Path:
    log_path = Path(settings.logging.log_file) if settings else Path("logs/tutorbot.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    app_logger = logging.getLogger("tutorbot")
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    for existing in list(app_logger.handlers):
        if existing.get_name() == "tutorbot-file":
            app_logger.removeHandler(existing)
            existing.close()

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.addFilter(_SafeLogFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    handler.set_name("tutorbot-file")
    app_logger.addHandler(handler)
    return log_path


def _log_run_result(result) -> None:
    logger = logging.getLogger(__name__)
    logger.info("Run completed: id=%s date=%s status=%s", result.run_id, result.run_date, result.status)
    for stage, values in result.stats.items():
        if stage == "dry_run_pending_ops":
            details = f"count={len(values)}"
        elif isinstance(values, dict):
            details = ", ".join(f"{key}={value}" for key, value in values.items()
                                if isinstance(value, (int, float, bool)))
        else:
            details = "available"
        logger.info("%s: %s", stage, details)
    if result.errors:
        logger.error("Run reported %d error(s)", len(result.errors))


class _DefaultFakeLLMBackend:
    """Built-in deterministic backend for `tutorbot run --fake-llm`."""

    def complete_json(self, task: str, messages: Any, schema: Any = None) -> Any:
        if task == "t1_curriculum":
            return {
                "goal": {"title": "기본 학습 목표", "level": "실무", "deadline": None},
                "concepts": [
                    {"key": "c1", "parent_key": None, "name": "기초 개념", "description": "핵심 기초", "prereq_keys": [], "order": 1},
                    {"key": "c2", "parent_key": "c1", "name": "세부 개념 A", "description": "세부 A", "prereq_keys": [], "order": 1},
                    {"key": "c3", "parent_key": "c1", "name": "세부 개념 B", "description": "세부 B", "prereq_keys": ["c2"], "order": 2},
                ],
            }
        if task == "t2_dump":
            return {
                "summary": "아직 지식 덤프에 근거가 없어 기초부터 학습을 권장합니다.",
                "assessments": [],
                "issues": [],
                "gap_summary": ["기초부터 학습 권장"],
                "next_concepts": [],
            }
        if task == "t3_study":
            return {
                "concepts": [],
                "feedback": {"corrections": [], "missing": [], "next": ["다음 개념 진행"]},
                "cards": [],
                "off_curriculum": None,
            }
        if task == "t4_grade":
            return []
        if task == "t5_weekly":
            return {"summary": "주간 학습 요약입니다.", "weak_points": [], "next_week": ["복습 진행"]}
        if task == "t6_profile":
            return {"summary": "꾸준히 학습 중입니다."}
        if task == "domain_feedback":
            return {
                "summary": "기록을 검토했습니다.",
                "feedback": [],
                "evidence": [],
            }
        if task == "feedback_rule":
            return {"summary": "가짜 피드백 요약", "feedback": ["가짜 피드백 항목"], "evidence": []}
        return {}


def _client(settings) -> DynalistClient:
    return DynalistClient(settings.dynalist_token())


def cmd_init_db(args, settings) -> int:
    """Validate configured Dynalist documents before changing the local schema."""
    cl = _client(settings)
    try:
        _files, missing = _configured_files(cl, settings)
    except DynalistError:
        print("Dynalist request failed", file=sys.stderr)
        return 1
    if missing:
        print(f"! configured documents unavailable: {len(missing)}", file=sys.stderr)
        return 1

    conn = db.connect(settings.db_path)
    print(f"schema_version={db.migrate(conn)}")
    return 0


def _configured_file_ids(settings) -> list[str]:
    profiles = getattr(settings, "domains", {})
    if not isinstance(profiles, dict):
        return []
    file_ids: list[str] = []
    for profile in profiles.values():
        if getattr(profile, "enabled", False):
            file_ids.extend(getattr(profile, "file_ids", []))
    career = profiles.get("career")
    if career is not None and getattr(career, "enabled", False):
        for source_domain in getattr(career, "source_domains", []):
            source_profile = profiles.get(source_domain)
            if source_profile is not None and getattr(source_profile, "enabled", False):
                file_ids.extend(getattr(source_profile, "file_ids", []))
    return list(dict.fromkeys(file_ids))


def _configured_files(cl: DynalistClient, settings, ids=None) -> tuple[dict[str, dict], list[str]]:
    """Fetch Dynalist's file list and identify configured IDs that are absent."""
    files = {f["id"]: f for f in cl.file_list()}
    missing = [fid for fid in (ids if ids is not None else _configured_file_ids(settings)) if fid not in files]
    return files, missing


def _doctor_file_ids(settings):
    rules = getattr(settings, "feedback_rules", None)
    return list(dict.fromkeys(_configured_file_ids(settings) + (rules.file_ids() if rules else [])))


def cmd_doctor(args, settings) -> int:
    """Read-only checks; with --write-test also insert->edit->delete a scratch node (M0 smoke test)."""
    llm_client = None
    try:
        cl = _client(settings)
        ids = _doctor_file_ids(settings)
        _files, missing = _configured_files(cl, settings, ids)
        if missing:
            print(f"! configured documents unavailable: {len(missing)}")
            return 1
        rules = getattr(settings, "feedback_rules", None)
        writable = set(_configured_file_ids(settings) + (rules.write_file_ids() if rules else []))
        for fid in ids:
            idx = NodeIndex.from_doc_read(fid, cl.doc_read(fid))
            print(f"document: {len(idx.by_id) - 1} nodes")
            if args.write_test and fid in writable:
                _write_test(cl, fid)
                llm_client = _llm_test(settings, llm_client)
    except Exception:  # noqa: BLE001 - diagnostics must not expose remote exceptions
        print("Dynalist request failed", file=sys.stderr)
        return 1
    return 0


def _write_test(cl: DynalistClient, fid: str) -> None:
    from .dynalist.client import F_PARENT, ch_delete, ch_edit, ch_insert
    from .parse.tags import extract_tags
    first = cl.doc_edit(fid, [ch_insert("root", "tutorbot doctor tmp A", index=-1)])[0]
    second = cl.doc_edit(fid, [ch_insert("root", "tutorbot doctor tmp B", index=-1)])[0]
    idx1 = NodeIndex.from_doc_read(fid, cl.doc_read(fid))
    root_children = idx1.by_id["root"].children
    print(f"parent field name ({F_PARENT}) works:", True)
    print("index:-1 appends last:", root_children[-2:] == [first, second])
    cl.doc_edit(fid, [ch_edit(first, content="#공부 tutorbot doctor tag test")])
    idx2 = NodeIndex.from_doc_read(fid, cl.doc_read(fid))
    edited_node = idx2.by_id.get(first)
    print("korean tag #공부 stored & parsed:", extract_tags(edited_node.content if edited_node else "") == ["공부"])
    print("edit ok; delete ok:", cl.doc_edit(fid, [ch_delete(first), ch_delete(second)]) == [])


def _llm_test(settings, client=None):
    if client is False:
        print("llm_test: API key unavailable")
        return False
    configured_fast_model = settings.llm.model_fast or "gemini-3.5-flash-lite"

    def valid_test_response(value: Any) -> Any:
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise ValueError("invalid test response")
        return value

    if client is None:
        try:
            key = settings.llm_api_key()
        except RuntimeError:
            print("llm_test: API key unavailable")
            return False
        client = LLMClient(
            provider=settings.llm.provider,
            api_key=key,
            model_fast=configured_fast_model,
            model_smart=settings.llm.model_smart,
            requests_per_minute=getattr(settings.llm, "requests_per_minute", 10),
        )
    model = client.model_for_task("doctor_test")

    try:
        client.complete_json(
            "doctor_test",
            [{"role": "user", "content": 'Return JSON: {"ok": true}'}],
            validator=valid_test_response,
        )
        print(f"gemini ({model}) ok")
    except LLMError:
        # LLMError summaries are safe, but keep this diagnostic deliberately generic too.
        print(f"gemini ({model}) request failed")
    return client


def cmd_run(args, settings) -> int:
    if args.dry_run and not getattr(args, "fake_llm", False):
        budget = getattr(getattr(settings, "llm", None), "daily_token_budget", None)
        rules = getattr(settings, "feedback_rules", None)
        print("주의: dry-run은 Dynalist에 쓰지 않지만 학습·도메인 및 피드백의 선택·참조 "
              "문서 본문과 노트, 페르소나를 실제 외부 LLM에 전송할 수 있으며 비용이 발생합니다. "
              f"설정된 일일 토큰 예산: {budget if budget is not None else '없음'}, "
              f"피드백 실행별 호출 상한: {getattr(rules, 'max_llm_calls_per_run', '없음')}. "
              "다른 단계의 호출·비용도 확인하세요.")
        if not getattr(args, "allow_external_llm", False):
            print("전송에 동의하려면 --allow-external-llm을 지정하세요.", file=sys.stderr)
            return 2
    if args.dry_run:
        with closing(db.connect(":memory:")) as conn:
            if Path(settings.db_path).exists():
                with closing(sqlite3.connect(Path(settings.db_path).resolve().as_uri() + "?mode=ro", uri=True)) as src:
                    src.backup(conn)
            return _cmd_run(args, settings, conn)
    with closing(db.connect(settings.db_path)) as conn:
        return _cmd_run(args, settings, conn)


def _cmd_run(args, settings, conn) -> int:
    log_file = _configure_logging(settings)
    try:
        log_start = log_file.stat().st_size
    except OSError:
        log_start = 0
    logger = logging.getLogger(__name__)
    logger.info(
        "Run started: dry_run=%s fake_llm=%s",
        args.dry_run,
        args.fake_llm,
    )
    db.migrate(conn)

    cl = _client(settings)
    if args.fake_llm:
        llm = LLMClient(
            provider=settings.llm.provider,
            model_smart=settings.llm.model_smart,
            model_fast=settings.llm.model_fast,
            backend=_DefaultFakeLLMBackend(),
            conn=conn,
            requests_per_minute=getattr(settings.llm, "requests_per_minute", 10),
        )
    else:
        llm = LLMClient(
            provider=settings.llm.provider,
            api_key=settings.llm_api_key(),
            model_smart=settings.llm.model_smart,
            model_fast=settings.llm.model_fast,
            conn=conn,
            requests_per_minute=getattr(settings.llm, "requests_per_minute", 10),
        )

    try:
        res = run_pipeline(
            conn,
            settings,
            cl,
            llm,
            run_date=args.date,
            dry_run=bool(args.dry_run),
            feedback=not getattr(args, "no_feedback", False),
        )
    except RuntimeError as exc:
        if isinstance(exc.__cause__, BlockingIOError):
            logger.warning("Run lock unavailable")
            print("tutorbot is already running", file=sys.stderr)
            return 75
        logger.error("Run aborted")
        print("Run aborted", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - runtime exceptions can contain private content
        logger.error("Run aborted")
        print("Run aborted", file=sys.stderr)
        return 1
    _log_run_result(res)
    if not args.dry_run:
        for handler in logging.getLogger("tutorbot").handlers:
            handler.flush()
        try:
            with log_file.open("rb") as stream:
                stream.seek(log_start)
                run_log = stream.read().decode("utf-8")
        except (OSError, UnicodeError):
            run_log = ""
            logger.warning("Could not read the current run's log segment for devlog")
        if not run_log:
            logger.warning("Current run log segment is empty; devlog run log was not saved")
        elif getattr(settings, "domains", {}).get("devlog") is not None:
            now = datetime.now(ZoneInfo("Asia/Seoul"))
            try:
                devlog_write = save_devlog_run_log(
                    conn,
                    settings,
                    cl,
                    run_id=res.run_id,
                    run_date=res.run_date,
                    run_time=now.strftime("%H:%M:%S"),
                    log_text=run_log,
                )
                res.stats["devlog_run_log"] = {
                    "done": devlog_write.done,
                    "reconciled": devlog_write.reconciled,
                    "failed": devlog_write.failed,
                }
                if devlog_write.errors or devlog_write.failed:
                    res.errors.append("devlog run log write failed")
                    res.status = "partial"
                conn.execute(
                    "UPDATE run_log SET status=?,error_summary=?,dynalist_changes=dynalist_changes+? WHERE id=?",
                    (
                        res.status,
                        "; ".join(res.errors) if res.errors else None,
                        devlog_write.changes_sent,
                        res.run_id,
                    ),
                )
            except Exception:  # noqa: BLE001 - remote exceptions can contain private content
                logger.error("Devlog run log write failed")
                res.errors.append("devlog run log write failed")
                res.status = "partial"
                conn.execute(
                    "UPDATE run_log SET status='partial',error_summary=? WHERE id=?",
                    ("; ".join(res.errors), res.run_id),
                )
    print(f"Run {res.run_id} ({res.run_date}): {res.status}")
    if "feedback" in res.stats:
        print("feedback: " + json.dumps(
            {key: value for key, value in res.stats["feedback"].items()
             if isinstance(value, int)}, ensure_ascii=False))
    return 0 if res.status in ("ok", "partial") else 1


def cmd_stats(args, settings) -> int:
    """Print daily run_log and per-task llm_call token statistics (spec §9.3.7)."""
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    runs = conn.execute(
        "SELECT id, run_date, status, llm_calls, llm_prompt_tokens, llm_completion_tokens,"
        " dynalist_reads, dynalist_changes FROM run_log ORDER BY id DESC LIMIT 14"
    ).fetchall()
    by_task = conn.execute(
        "SELECT task, COUNT(*) AS calls, SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS ok_calls,"
        " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens"
        " FROM llm_call GROUP BY task ORDER BY task ASC"
    ).fetchall()
    out = {
        "recent_runs": [dict(r) for r in runs],
        "llm_by_task": [dict(r) for r in by_task],
        "feedback_task_runs": [dict(r) for r in conn.execute(
            "SELECT id,task_id,cycle_key,status,started_at,units_ok,units_failed,llm_calls,"
            "prompt_tokens,completion_tokens FROM feedback_task_run ORDER BY id DESC LIMIT 20")],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    command_line = list(sys.argv[1:] if argv is None else argv)
    is_run_invocation = "run" in command_line
    logger = logging.getLogger(__name__)
    if is_run_invocation:
        _configure_logging()
        logger.info("CLI invocation started")

    p = argparse.ArgumentParser(prog="tutorbot")
    p.add_argument("-c", "--config", default="config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    d = sub.add_parser("doctor")
    d.add_argument("--write-test", action="store_true", help="insert/edit/delete a scratch node")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--date")
    r.add_argument("--fake-llm", action="store_true")
    r.add_argument("--allow-external-llm", action="store_true",
                   help="consent to sending document content to a real LLM during preview")
    r.add_argument("--no-feedback", action="store_true")
    sub.add_parser("stats")
    fb = sub.add_parser("feedback")
    fb_sub = fb.add_subparsers(dest="feedback_cmd", required=True)
    for name in ("validate", "list", "run", "approve", "status"):
        command = fb_sub.add_parser(name)
        if name != "status":
            command.add_argument("--rules-file")
            command.add_argument("--base-dir")
        if name in ("validate", "list", "status"):
            command.add_argument("--json", action="store_true")
        if name == "validate":
            command.add_argument("--online", action="store_true")
            command.add_argument("--allow-placeholders", action="store_true")
        if name in ("list", "run"):
            command.add_argument("--at")
        if name == "run":
            command.add_argument("--task", action="append")
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--fake-llm", action="store_true")
            command.add_argument("--force", action="store_true")
            command.add_argument("--allow-external-llm", action="store_true",
                                 help="consent to sending selected and reference content for a real LLM preview")
        if name == "approve":
            group = command.add_mutually_exclusive_group(required=True)
            group.add_argument("--task")
            group.add_argument("--all", action="store_true")
            command.add_argument("--yes", action="store_true")
            command.add_argument("--hash")
            command.add_argument("--revoke", action="store_true")
        if name == "status":
            command.add_argument("--task")
            command.add_argument("--limit", type=int, choices=range(1, 201), default=20)
    try:
        args = p.parse_args(command_line)
    except SystemExit as exc:
        if is_run_invocation and exc.code != 0:
            logger.error("Command argument parsing failed: exit_code=%s", exc.code)
        raise
    if is_run_invocation:
        logger.info("Command started: %s", args.cmd)
    try:
        if args.cmd == "feedback" and args.feedback_cmd == "status":
            from types import SimpleNamespace

            from .config import _parse_toml_bytes
            config_path = Path(args.config)
            prefix = []
            if config_path.exists():
                for line in config_path.read_text(encoding="utf-8").splitlines():
                    if line.lstrip().startswith("["):
                        break
                    prefix.append(line)
            root = _parse_toml_bytes("\n".join(prefix).encode())
            settings = SimpleNamespace(db_path=root.get("db_path", "data/tutorbot.sqlite3"))
        elif args.cmd == "feedback":
            settings = load_settings(args.config if Path(args.config).exists() else None,
                                     rules_file=getattr(args, "rules_file", None),
                                     base_dir=getattr(args, "base_dir", None),
                                     allow_placeholders=getattr(args, "allow_placeholders", False))
        else:
            settings = load_settings(args.config if Path(args.config).exists() else None)
    except (OSError, ValueError, TypeError):
        if is_run_invocation:
            logger.error("Command initialization failed")
        print("Command initialization failed", file=sys.stderr)
        return 1
    if args.cmd == "doctor":
        _configure_logging(settings)
    if args.cmd == "feedback":
        from .feedback.commands import dispatch
        safe_filter = _SafeLogFilter()
        protected_logs = [logging.getLogger(name) for name in (
            "tutorbot.parse.classify", "tutorbot.dynalist.writer", "tutorbot.llm.client")]
        for component_log in protected_logs:
            component_log.addFilter(safe_filter)
        try:
            return dispatch(args, settings)
        except Exception:  # noqa: BLE001 - CLI errors must not expose private content
            print("Feedback command failed", file=sys.stderr)
            if args.feedback_cmd == "run":
                print("feedback exit=1 reasons=execution_failed:1", file=sys.stderr)
            return 1
        finally:
            for component_log in protected_logs:
                component_log.removeFilter(safe_filter)
    return {
        "init-db": cmd_init_db,
        "doctor": cmd_doctor,
        "run": cmd_run,
        "stats": cmd_stats,
    }[args.cmd](args, settings)


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path
    _src = str(_Path(__file__).resolve().parent.parent)
    if _src not in _sys.path:
        _sys.path.insert(0, _src)
    sys.exit(main())
