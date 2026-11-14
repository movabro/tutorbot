import logging
from types import SimpleNamespace

from tutorbot import cli


def test_run_logs_readable_summary_instead_of_json(tmp_path, monkeypatch, capsys):
    log_path = tmp_path / "logs" / "tutorbot.log"
    settings = SimpleNamespace(
        db_path=str(tmp_path / "tutorbot.sqlite3"),
        logging=SimpleNamespace(log_file=str(log_path)),
        llm=SimpleNamespace(provider="gemini", model_smart="test", model_fast="test"),
    )
    conn = object()
    result = SimpleNamespace(
        run_id=7,
        run_date="2026-11-14",
        status="ok",
        stats={"ingest": {"dumps_changed": 0}, "write": {"changes_sent": 0}},
        errors=[],
    )
    monkeypatch.setattr(cli.db, "connect", lambda _: conn)
    monkeypatch.setattr(cli.db, "migrate", lambda _: 1)
    monkeypatch.setattr(cli, "_client", lambda _: object())
    monkeypatch.setattr(cli, "run_pipeline", lambda *args, **kwargs: result)

    app_logger = logging.getLogger("tutorbot")
    old_level = app_logger.level
    old_propagate = app_logger.propagate
    try:
        exit_code = cli.cmd_run(
            SimpleNamespace(dry_run=False, date=None, fake_llm=True),
            settings,
        )
        output = capsys.readouterr().out.strip()
        log_text = log_path.read_text(encoding="utf-8")
        assert exit_code == 0
        assert output == f"Run 7 (2026-11-14): ok; log: {log_path}"
        assert not output.startswith("{")
        assert "Run started:" in log_text
        assert "Run completed: id=7 date=2026-11-14 status=ok" in log_text
        assert "ingest: dumps_changed=0" in log_text
        assert "write: changes_sent=0" in log_text
    finally:
        for handler in list(app_logger.handlers):
            if handler.get_name() == "tutorbot-file":
                app_logger.removeHandler(handler)
                handler.close()
        app_logger.setLevel(old_level)
        app_logger.propagate = old_propagate
