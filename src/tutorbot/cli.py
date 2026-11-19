"""tutorbot CLI: init-db | doctor | run | stats (spec §11, §14)."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import db
from .config import load_settings
from .dynalist.client import DynalistClient, DynalistError
from .dynalist.tree import NodeIndex
from .llm.client import LLMClient, LLMError
from .pipeline import run_pipeline


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
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    handler.set_name("tutorbot-file")
    app_logger.addHandler(handler)
    return log_path


def _log_run_result(result) -> None:
    logger = logging.getLogger(__name__)
    logger.info("Run completed: id=%s date=%s status=%s", result.run_id, result.run_date, result.status)
    for stage, values in result.stats.items():
        if isinstance(values, dict):
            details = ", ".join(f"{key}={value}" for key, value in values.items())
        else:
            details = str(values)
        logger.info("%s: %s", stage, details)
    for error in result.errors:
        logger.error("%s", error)


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
        return {}


def _client(settings) -> DynalistClient:
    return DynalistClient(settings.dynalist_token())


def cmd_init_db(args, settings) -> int:
    conn = db.connect(settings.db_path)
    print(f"schema_version={db.migrate(conn)} path={settings.db_path}")
    return 0


def cmd_doctor(args, settings) -> int:
    """Read-only checks; with --write-test also insert->edit->delete a scratch node (M0 smoke test)."""
    cl = _client(settings)
    llm_client = None
    try:
        files = {f["id"]: f for f in cl.file_list()}
        for fid in settings.dynalist.file_ids:
            if fid not in files:
                print(f"! file {fid} not found in file/list")
                return 1
            idx = NodeIndex.from_doc_read(fid, cl.doc_read(fid))
            print(f"{fid} ({files[fid].get('title')}): {len(idx.by_id) - 1} nodes, version={idx.version}")
            if args.write_test:
                _write_test(cl, fid)
                llm_client = _llm_test(settings, llm_client)
    except DynalistError as e:
        print(f"Dynalist request failed: {e}", file=sys.stderr)
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
    log_file = _configure_logging(settings)
    logger = logging.getLogger(__name__)
    logger.info(
        "Run started: date=%s dry_run=%s fake_llm=%s",
        args.date or "today",
        args.dry_run,
        args.fake_llm,
    )
    if args.dry_run:
        src = Path(settings.db_path)
        tmp = Path(tempfile.mkdtemp()) / "dry.sqlite3"
        if src.exists():
            shutil.copy(src, tmp)
        conn = db.connect(tmp)
    else:
        conn = db.connect(settings.db_path)
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
        )
    except Exception:
        logger.exception("Run aborted")
        raise
    _log_run_result(res)
    print(f"Run {res.run_id} ({res.run_date}): {res.status}; log: {log_file}")
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
    sub.add_parser("stats")
    try:
        args = p.parse_args(command_line)
    except SystemExit as exc:
        if is_run_invocation and exc.code != 0:
            logger.error("Command argument parsing failed: exit_code=%s", exc.code)
        raise
    if is_run_invocation:
        logger.info("Command started: %s", args.cmd)
    try:
        settings = load_settings(args.config if Path(args.config).exists() else None)
    except (OSError, ValueError, TypeError):
        if is_run_invocation:
            logger.exception("Command initialization failed")
        raise
    if args.cmd == "doctor":
        _configure_logging(settings)
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
