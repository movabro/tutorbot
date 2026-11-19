from __future__ import annotations

import io
import json
import logging
import urllib.error
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tutorbot import cli
from tutorbot.db import connect, migrate
from tutorbot.llm.client import (
    LLMClient,
    LLMError,
    _gemini_retry_delay,
    _retry_after_seconds,
)


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def utcnow(self):
        return datetime(2026, 11, 18, tzinfo=timezone.utc)


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        if isinstance(self.value, bytes):
            return self.value
        return json.dumps(
            {"candidates": [{"content": {"parts": [{"text": self.value}]}}],
             "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3}}
        ).encode()


def http_error(status, body=b"", headers=None):
    return urllib.error.HTTPError(
        "https://example.invalid/?key=NEVER_LOG_THIS",
        status,
        "secret response text",
        headers or {},
        io.BytesIO(body),
    )


def retry_body(value):
    return json.dumps({
        "error": {
            "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": value}
            ]
        }
    }).encode()


def make_client(responses, fake_time=None, **kwargs):
    fake_time = fake_time or FakeTime()
    started = []
    pending = list(responses)

    def transport(req, timeout):
        started.append((fake_time.monotonic(), req.full_url, req.get_method(), timeout))
        value = pending.pop(0)
        if isinstance(value, BaseException):
            raise value
        return Response(value)

    client = LLMClient(
        api_key="API_KEY_MUST_NOT_LEAK",
        monotonic=fake_time.monotonic,
        sleep=fake_time.sleep,
        wall_clock=fake_time.utcnow,
        http_transport=transport,
        **kwargs,
    )
    return client, fake_time, started


def complete(client, task="t1", validator=None):
    return client.complete_json(task, [{"role": "user", "content": "private prompt"}], validator=validator)


def test_models_and_tasks_share_one_client_limiter_and_fake_backend_bypasses_it():
    client, _time, started = make_client(
        ['{"ok":true}', '{"ok":true}'],
        model_smart="gemini-2.5-pro",
        model_fast="gemini-2.5-flash",
    )
    assert complete(client, "t1") == {"ok": True}
    assert complete(client, "t4_grade") == {"ok": True}
    assert [row[0] for row in started] == [0, 6]
    assert "/models/gemini-2.5-pro:generateContent" in started[0][1]
    assert "/models/gemini-2.5-flash:generateContent" in started[1][1]

    class Backend:
        def complete_json(self, task, messages, schema):
            return {"ok": True}

    backend_time = FakeTime()
    fake = LLMClient(backend=Backend(), monotonic=backend_time.monotonic,
                     sleep=backend_time.sleep)
    assert complete(fake) == {"ok": True}
    assert complete(fake) == {"ok": True}
    assert backend_time.sleeps == []


def test_gemini_success_without_optional_usage_metadata_is_valid():
    client, _, _ = make_client([
        b'{"candidates":[{"content":{"parts":[{"text":"{\\"ok\\":true}"}]}}]}'
    ])
    assert complete(client) == {"ok": True}
    assert client.stats["prompt_tokens"] == 0
    assert client.stats["completion_tokens"] == 0


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'{"candidates":[]}',
        b'{"candidates":[{"content":{"parts":[]}}]}',
        b'{"candidates":[{"content":{"parts":[{"text":7}]}}]}',
        b'{"candidates":[{"content":{"parts":[{"text":"{}"}]}}],"usageMetadata":null}',
        b'{"candidates":[{"content":{"parts":[{"text":"{}"}]}}],"usageMetadata":[]}',
        b'{"candidates":[{"content":{"parts":[{"text":"{}"}]}}],"usageMetadata":{}}',
        b'{"candidates":[{"content":{"parts":[{"text":"{}"}]}}],"usageMetadata":{"promptTokenCount":true}}',
        b'{"candidates":[{"content":{"parts":[{"text":"{}"}]}}],"usageMetadata":{"candidatesTokenCount":-1}}',
    ],
)
def test_malformed_success_response_is_permanent_safe_category(body, tmp_path):
    conn = connect(":memory:")
    migrate(conn)
    client, _, starts = make_client([body], conn=conn)
    with pytest.raises(LLMError, match="gemini_invalid_response"):
        complete(client)
    assert len(starts) == 1
    logged = conn.execute("SELECT error FROM llm_call").fetchone()[0]
    assert logged == "gemini_invalid_response"
    assert "backend_error" not in logged
    assert "[]" not in logged


def test_429_header_precedes_body_and_limiter_still_applies():
    client, time, starts = make_client([
        http_error(429, retry_body("8s"), {"Retry-After": "5"}),
        '{"ok":true}',
    ])
    assert complete(client) == {"ok": True}
    assert time.sleeps == [5, 1]
    assert [row[0] for row in starts] == [0, 6]


def test_retry_after_date_past_and_gemini_delay_parser():
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)

    def wall():
        return now
    assert _retry_after_seconds("3", wall_clock=wall) == 3
    assert _retry_after_seconds("Wed, 18 Nov 2026 00:00:02 GMT", wall_clock=wall) == 2
    assert _retry_after_seconds("Tue, 17 Nov 2026 00:00:00 GMT", wall_clock=wall) == 0
    assert _retry_after_seconds("not a date", wall_clock=wall) is None
    assert _gemini_retry_delay(retry_body("1.25s")) == 1.25
    assert _gemini_retry_delay(retry_body("bad")) is None


def test_429_body_delay_and_fallback_order_without_consuming_fallback_on_server_delay():
    client, time, _ = make_client([
        http_error(429, retry_body("4s")),
        http_error(429, b"{}", {"Retry-After": "invalid"}),
        '{"ok":true}',
    ])
    assert complete(client) == {"ok": True}
    assert time.sleeps == [4, 2, 2, 4]


@pytest.mark.parametrize(
    "failures",
    [
        [http_error(503), http_error(502)],
        [urllib.error.URLError("private URL/key"), urllib.error.URLError("private URL/key")],
    ],
)
def test_network_and_5xx_retry_twice_then_succeed(failures):
    client, time, starts = make_client([*failures, '{"ok":true}'])
    assert complete(client) == {"ok": True}
    assert len(starts) == 3
    assert time.sleeps == [2, 4, 8]


def test_fatal_4xx_is_not_retried_or_delayed():
    client, time, starts = make_client([http_error(403, b"secret"), '{"ok":true}'])
    with pytest.raises(LLMError, match="http_error HTTP 403"):
        complete(client)
    assert len(starts) == 1
    assert time.sleeps == []


def test_transient_retry_cap_has_no_wait_after_final_attempt():
    client, time, starts = make_client([
        http_error(429),
        http_error(429),
        http_error(429),
        '{"ok":true}',
    ])
    with pytest.raises(LLMError, match="rate_limited HTTP 429"):
        complete(client)
    assert len(starts) == 3
    # Only the two retries are delayed; limiter also spaces their starts.
    assert time.sleeps == [2, 4, 8]


def test_limiter_internal_error_is_explicit_and_does_not_retry():
    time = FakeTime()
    starts = []

    def transport(req, timeout):
        starts.append(req.full_url)
        return Response('{"ok":true}')

    client = LLMClient(
        api_key="private",
        monotonic=time.monotonic,
        sleep=lambda _: None,
        http_transport=transport,
    )
    assert complete(client) == {"ok": True}
    with pytest.raises(LLMError, match="request_limiter_error"):
        complete(client)
    assert len(starts) == 1


def test_repair_is_once_and_uses_its_own_three_attempt_budget_with_six_call_cap():
    client, time, starts = make_client([
        http_error(503),
        urllib.error.URLError("secret"),
        "not-json",
        http_error(503),
        http_error(503),
        http_error(503),
        '{"ok":true}',
    ])
    with pytest.raises(LLMError, match="server_error HTTP 503"):
        complete(client)
    assert len(starts) == 6
    assert time.sleeps == [2, 4, 8, 6, 2, 4, 8]


def test_repair_success_uses_limiter_and_repair_validation_failure_is_explicit():
    client, time, starts = make_client(["not-json", '{"ok":true}'])

    def validator(value):
        if not value.get("ok"):
            raise ValueError("not ok")
        return value

    assert complete(client, validator=validator)
    assert len(starts) == 2
    assert time.sleeps == [6]

    client, _, starts = make_client(["not-json", "still-not-json"])
    with pytest.raises(LLMError, match="json_validation_failed_after_repair"):
        complete(client)
    assert len(starts) == 2


def test_llm_call_error_log_contains_only_safe_summary(tmp_path):
    conn = connect(":memory:")
    migrate(conn)
    client, _, _ = make_client(
        [http_error(403, b"BODY_SECRET")],
        conn=conn,
        run_id=None,
    )
    with pytest.raises(LLMError):
        complete(client)
    logged = conn.execute("SELECT error FROM llm_call").fetchone()[0]
    assert "HTTP 403" in logged
    assert all(secret not in logged for secret in (
        "API_KEY_MUST_NOT_LEAK", "NEVER_LOG_THIS", "BODY_SECRET", "private prompt"
    ))


def test_doctor_llm_test_uses_client_boundary_and_hides_api_failures(monkeypatch, capsys):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def complete_json(self, *args, **kwargs):
            raise LLMError("safe error")

        def model_for_task(self, task):
            assert task == "doctor_test"
            return "fast"

    monkeypatch.setattr(cli, "LLMClient", FakeClient)
    settings = SimpleNamespace(
        llm=SimpleNamespace(provider="gemini", model_fast="fast", model_smart="smart",
                            requests_per_minute=7),
        llm_api_key=lambda: "private-key",
    )
    cli._llm_test(settings)
    output = capsys.readouterr().out
    assert captured["requests_per_minute"] == 7
    assert "request failed" in output
    assert "private-key" not in output


def test_doctor_llm_test_never_prints_transport_url_or_error_body(monkeypatch, capsys):
    sent = []

    def fake_urlopen(req, timeout):
        sent.append((req, timeout))
        raise http_error(403, b"BODY_SECRET")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    settings = SimpleNamespace(
        llm=SimpleNamespace(provider="gemini", model_fast="fast", model_smart="smart",
                            requests_per_minute=10),
        llm_api_key=lambda: "API_SECRET",
    )
    cli._llm_test(settings)
    output = capsys.readouterr().out
    assert len(sent) == 1
    assert "request failed" in output
    assert all(secret not in output for secret in (
        "API_SECRET", "NEVER_LOG_THIS", "BODY_SECRET", "private prompt"
    ))


def test_doctor_write_test_reuses_one_client_and_limiter_across_files(monkeypatch, capsys):
    fake_time = FakeTime()
    starts = []
    clients = []
    real_client = LLMClient

    def transport(req, timeout):
        starts.append((fake_time.monotonic(), req.full_url))
        return Response('{"ok":true}')

    def make_doctor_client(**kwargs):
        kwargs.update(
            monotonic=fake_time.monotonic,
            sleep=fake_time.sleep,
            http_transport=transport,
        )
        client = real_client(**kwargs)
        clients.append(client)
        return client

    class FakeDynalist:
        def file_list(self):
            return [{"id": "file-a", "title": "A"}, {"id": "file-b", "title": "B"}]

        def doc_read(self, fid):
            return {}

    monkeypatch.setattr(cli, "_client", lambda settings: FakeDynalist())
    monkeypatch.setattr(cli, "LLMClient", make_doctor_client)
    monkeypatch.setattr(
        cli.NodeIndex,
        "from_doc_read",
        lambda fid, doc: SimpleNamespace(by_id={"root": {}}, version=1),
    )
    monkeypatch.setattr(cli, "_write_test", lambda client, fid: None)
    settings = SimpleNamespace(
        domains={
            "study": SimpleNamespace(enabled=True, file_ids=["file-a", "file-b"]),
        },
        llm=SimpleNamespace(
            provider="gemini",
            model_fast="gemini-2.5-flash",
            model_smart="gemini-2.5-pro",
            requests_per_minute=10,
        ),
        llm_api_key=lambda: "private-key",
    )

    assert cli.cmd_doctor(SimpleNamespace(write_test=True), settings) == 0

    assert len(clients) == 1
    assert clients[0]._limiter is not None
    assert [started for started, _ in starts] == [0, 6]
    assert all("/models/gemini-2.5-flash:generateContent" in url for _, url in starts)
    output = capsys.readouterr().out
    assert output.count("gemini (gemini-2.5-flash) ok") == 2


def test_gemini_limiter_scope_is_logged_once_and_fake_backend_is_not_mislabeled(caplog):
    client, _, _ = make_client(['{"ok":true}', '{"ok":true}'], requests_per_minute=7)
    with caplog.at_level(logging.INFO, logger="tutorbot.llm.client"):
        complete(client)
        complete(client)
    scope_records = [record.getMessage() for record in caplog.records
                     if "gemini_limiter" in record.getMessage()]
    assert scope_records == [
        (
            "gemini_limiter requests_per_minute=7 scope=process_local "
            "coordination=none_across_processes_or_hosts"
        )
    ]

    class Backend:
        def complete_json(self, task, messages, schema):
            return {"ok": True}

    with caplog.at_level(logging.INFO, logger="tutorbot.llm.client"):
        complete(LLMClient(backend=Backend()), task="t1")
    assert len([record for record in caplog.records
                if "gemini_limiter" in record.getMessage()]) == 1
