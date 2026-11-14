from types import SimpleNamespace

import pytest

from tutorbot import cli
from tests.fakes import FakeDynalist
from tutorbot.db import connect, migrate
from tutorbot.dynalist import client as C
from tutorbot.dynalist.client import DynalistClient, FatalError, NodeNotFound, TokenBucket


def mk(fk, **kw):
    sleeps = []
    return DynalistClient("tok", transport=fk, sleep=sleeps.append, **kw), sleeps


def test_retry_toomanyrequests_then_success():
    fk = FakeDynalist(); fk.add_doc("d")
    fk.fail("doc/read", "TooManyRequests", times=2)
    c, sleeps = mk(fk)
    assert c.doc_read("d")["_code"] == "Ok"
    assert sleeps == [2, 4]


def test_retry_exhausted():
    fk = FakeDynalist(); fk.add_doc("d")
    fk.fail("doc/read", "TooManyRequests", times=10)
    c, sleeps = mk(fk, max_retries=3)
    with pytest.raises(C.RetryableError):
        c.doc_read("d")
    assert sleeps == [2, 4, 8]


def test_fatal_not_retried():
    fk = FakeDynalist()
    c, sleeps = mk(fk)
    with pytest.raises(NodeNotFound):
        c.doc_read("nope")
    assert sleeps == []
    fk.fail("file/list", "InvalidToken")
    with pytest.raises(FatalError):
        c.file_list()


def test_non_json_error_response_is_preserved():
    body = "<html>Access denied</html>"
    assert C._decode_response(body) == {"_msg": body}
    assert C._decode_response("") == {}


def test_urllib_fallback_sets_user_agent(monkeypatch):
    import builtins
    import urllib.request

    original_import = builtins.__import__

    def import_without_httpx(name, *args, **kwargs):
        if name == "httpx":
            raise ModuleNotFoundError("No module named 'httpx'")
        return original_import(name, *args, **kwargs)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"_code":"Ok"}'

    captured = []

    def fake_urlopen(req, timeout):
        captured.append(req)
        return Response()

    monkeypatch.setattr(builtins, "__import__", import_without_httpx)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    status, data = C._httpx_transport()("file/list", {"token": "test-token"})

    assert status == 200
    assert data["_code"] == "Ok"
    assert captured[0].get_header("User-agent") == C.USER_AGENT


def test_doctor_reports_dynalist_error_without_traceback(monkeypatch, capsys):
    class FailingClient:
        def file_list(self):
            raise FatalError("HTTP403", "Access denied", 403)

    monkeypatch.setattr(cli, "_client", lambda settings: FailingClient())
    rc = cli.cmd_doctor(SimpleNamespace(write_test=False), object())

    assert rc == 1
    assert capsys.readouterr().err == "Dynalist request failed: HTTP403: Access denied\n"


def test_5xx_retried():
    fk = FakeDynalist(); fk.add_doc("d")
    fk.fail("doc/read", "Err", times=1, status=503)
    c, sleeps = mk(fk)
    assert c.doc_read("d")
    assert sleeps == [2]


def test_edit_roundtrip_insert_edit_delete_and_index_last():
    fk = FakeDynalist(); fk.add_doc("d")
    c, _ = mk(fk)
    a = fk.add("d", "root", "first")
    ids = c.doc_edit("d", [C.ch_insert("root", "x"), C.ch_insert("root", "y", note="n")])
    assert len(ids) == 2
    assert fk.docs["d"]["root"]["children"] == [a, *ids]
    c.doc_edit("d", [C.ch_edit(ids[0], content="X")])
    assert fk.docs["d"][ids[0]]["content"] == "X"
    c.doc_edit("d", [C.ch_delete(ids[0])])
    assert ids[0] not in fk.docs["d"]
    with pytest.raises(NodeNotFound):
        c.doc_edit("d", [C.ch_edit("zzz", content="q")])


def test_edit_chunks_at_100():
    fk = FakeDynalist(); fk.add_doc("d")
    c, _ = mk(fk, changes_per_minute=1e9)
    ids = c.doc_edit("d", [C.ch_insert("root", str(i)) for i in range(250)])
    assert len(ids) == 250
    assert [len(b["changes"]) for p, b in fk.calls if p == "doc/edit"] == [100, 100, 50]


def test_token_bucket_blocks():
    t = [0.0]; slept = []
    def sleep(s): slept.append(s); t[0] += s
    b = TokenBucket(60, 2, monotonic=lambda: t[0], sleep=sleep)
    b.acquire(); b.acquire()
    assert slept == []
    b.acquire()
    assert slept and abs(slept[0] - 1.0) < 1e-6


def test_crash_after_apply_is_unknown_outcome_and_not_retried():
    fk = FakeDynalist(); fk.add_doc("d")
    fk.crash_after_apply()
    c, sleeps = mk(fk)
    with pytest.raises(C.UnknownOutcome):
        c.doc_edit("d", [C.ch_insert("root", "x")])
    assert fk.count_nodes("d") == 1 and sleeps == []


def test_migrate_creates_schema_idempotently():
    conn = connect(":memory:")
    assert migrate(conn) == 1
    assert migrate(conn) == 1
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"goal", "concept", "study_entry", "quiz_card", "quiz_attempt", "bot_node",
            "write_op", "knowledge_dump", "run_log", "llm_call", "meta"} <= names
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    # FK / unique sanity
    conn.execute("INSERT INTO write_op(op_key,file_id,action,payload_json,seq,created_at) "
                 "VALUES('k','f','insert','{}',1,'t')")
    with pytest.raises(Exception):
        conn.execute("INSERT INTO write_op(op_key,file_id,action,payload_json,seq,created_at) "
                     "VALUES('k','f','insert','{}',2,'t')")
