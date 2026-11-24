import logging
from types import SimpleNamespace

import pytest

from tutorbot import cli
from tutorbot.dynalist.writer import WriteReport
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
        assert cli.main(["-c", str(config_path), "run"]) == 1

        log_text = (tmp_path / "logs" / "tutorbot.log").read_text(encoding="utf-8")
        assert "CLI invocation started" in log_text
        assert "Command started: run" in log_text
        assert "Command initialization failed" in log_text
        assert "invalid career source domain: personal_tasks" not in log_text
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
    conn = SimpleNamespace(close=lambda: None)
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
        assert output == "Run 7 (2026-11-14): ok"
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


def test_cmd_run_passes_only_current_run_log_segment_to_devlog(tmp_path, monkeypatch, capsys):
    log_path = tmp_path / "logs" / "tutorbot.log"
    log_path.parent.mkdir()
    previous_text = "previous unrelated log entry\n"
    log_path.write_text(previous_text, encoding="utf-8")
    settings = SimpleNamespace(
        db_path=str(tmp_path / "tutorbot.sqlite3"),
        logging=SimpleNamespace(log_file=str(log_path)),
        llm=SimpleNamespace(provider="gemini", model_smart="test", model_fast="test"),
        domains={"devlog": SimpleNamespace(enabled=True, file_ids=["devlog-file"])},
    )

    class Conn:
        def execute(self, *_args):
            return None

        def close(self):
            pass

    conn = Conn()
    result = SimpleNamespace(
        run_id=12,
        run_date="2026-11-20",
        status="ok",
        stats={},
        errors=[],
    )
    saved = {}
    monkeypatch.setattr(cli.db, "connect", lambda _: conn)
    monkeypatch.setattr(cli.db, "migrate", lambda _: 1)
    monkeypatch.setattr(cli, "_client", lambda _: object())

    def fake_pipeline(*_args, **_kwargs):
        logging.getLogger("tutorbot.cli").info("pipeline stage log record")
        return result

    def save_log(_conn, _settings, _client, **kwargs):
        saved.update(kwargs)
        return WriteReport(done=2, changes_sent=2)

    monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(cli, "save_devlog_run_log", save_log)
    app_logger = logging.getLogger("tutorbot")
    old_level = app_logger.level
    old_propagate = app_logger.propagate
    try:
        assert cli.cmd_run(
            SimpleNamespace(dry_run=False, date=None, fake_llm=True),
            settings,
        ) == 0
        capsys.readouterr()
        text = saved["log_text"]
        log_file_text = log_path.read_text(encoding="utf-8")
        current_segment = log_file_text[len(previous_text):]
        assert text == current_segment
        assert "previous unrelated log entry" not in text
        assert "Run started:" in text
        assert "pipeline stage log record" in text
        assert "Run completed: id=12 date=2026-11-20 status=ok" in text
        assert saved["run_date"] == "2026-11-20"
        assert saved["run_time"]
    finally:
        for handler in list(app_logger.handlers):
            if handler.get_name() == "tutorbot-file":
                app_logger.removeHandler(handler)
                handler.close()
        app_logger.setLevel(old_level)
        app_logger.propagate = old_propagate


def test_cmd_run_dry_run_does_not_save_devlog_log(tmp_path, monkeypatch, capsys):
    settings = SimpleNamespace(
        db_path=str(tmp_path / "tutorbot.sqlite3"),
        logging=SimpleNamespace(log_file=str(tmp_path / "logs" / "tutorbot.log")),
        llm=SimpleNamespace(provider="gemini", model_smart="test", model_fast="test"),
        domains={"devlog": SimpleNamespace(enabled=True, file_ids=["devlog-file"])},
    )
    conn = SimpleNamespace(close=lambda: None)
    result = SimpleNamespace(
        run_id=13, run_date="2026-11-20", status="ok", stats={}, errors=[],
    )
    monkeypatch.setattr(cli.db, "connect", lambda _: conn)
    monkeypatch.setattr(cli.db, "migrate", lambda _: 1)
    monkeypatch.setattr(cli, "_client", lambda _: object())
    monkeypatch.setattr(cli, "run_pipeline", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        cli, "save_devlog_run_log",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not write devlog"),
    )

    app_logger = logging.getLogger("tutorbot")
    old_level = app_logger.level
    old_propagate = app_logger.propagate
    try:
        assert cli.cmd_run(
            SimpleNamespace(dry_run=True, date=None, fake_llm=True),
            settings,
        ) == 0
        capsys.readouterr()
    finally:
        for handler in list(app_logger.handlers):
            if handler.get_name() == "tutorbot-file":
                app_logger.removeHandler(handler)
                handler.close()
        app_logger.setLevel(old_level)
        app_logger.propagate = old_propagate
