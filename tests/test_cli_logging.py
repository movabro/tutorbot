import logging
from types import SimpleNamespace

import pytest

from tutorbot import cli
from tutorbot.engine.domains import _validate_feedback


def test_default_fake_llm_supports_domain_feedback():
    response = cli._DefaultFakeLLMBackend().complete_json("domain_feedback", [])

    assert _validate_feedback(response) == {
        "summary": "기록을 검토했습니다.",
        "feedback": [],
        "evidence": [],
    }


def test_main_logs_configuration_failure(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[domains.career]\nsource_domains = ["personal_tasks"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    app_logger = logging.getLogger("tutorbot")
    old_level = app_logger.level
    old_propagate = app_logger.propagate
    try:
        with pytest.raises(ValueError, match="invalid career source domain"):
            cli.main(["-c", str(config_path), "run"])

        log_text = (tmp_path / "logs" / "tutorbot.log").read_text(encoding="utf-8")
        assert "CLI invocation started" in log_text
        assert "Command started: run" in log_text
        assert "Command initialization failed" in log_text
        assert "invalid career source domain: personal_tasks" in log_text
    finally:
        for handler in list(app_logger.handlers):
            if handler.get_name() == "tutorbot-file":
                app_logger.removeHandler(handler)
                handler.close()
        app_logger.setLevel(old_level)
        app_logger.propagate = old_propagate


def test_main_logs_run_before_argument_parsing_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    app_logger = logging.getLogger("tutorbot")
    old_level = app_logger.level
    old_propagate = app_logger.propagate
    try:
        with pytest.raises(SystemExit):
            cli.main(["run", "-c", "config.toml"])

        log_text = (tmp_path / "logs" / "tutorbot.log").read_text(encoding="utf-8")
        assert "CLI invocation started" in log_text
        assert "Command argument parsing failed: exit_code=2" in log_text
    finally:
        for handler in list(app_logger.handlers):
            if handler.get_name() == "tutorbot-file":
                app_logger.removeHandler(handler)
                handler.close()
        app_logger.setLevel(old_level)
        app_logger.propagate = old_propagate


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
