from dataclasses import replace

import pytest

from tests.fakes import FakeDynalist, FakeLLM
from tests.test_feedback_foundations import task
from tests.test_feedback_reviewer_round6 import NOW
from tests.test_m5 import make_settings
from tutorbot import clock, db
from tutorbot.dynalist.client import DynalistClient
from tutorbot.dynalist.tree import NodeIndex
from tutorbot.feedback import prompt, service, sources, store
from tutorbot.feedback.models import (
    FeedbackRulesCfg,
    NodeRef,
    ReferenceBlock,
    SourceSpec,
)
from tutorbot.feedback.tokens import estimate_tokens
from tutorbot.llm.client import LLMClient
from tutorbot.pipeline import run_pipeline


def test_aggregate_uses_rendered_weight_not_raw_tokens():
    fake = FakeDynalist()
    fake.add_doc("doc")
    a = fake.add("doc", "root", "한" * 1000 + "x" * 50000 + "한" * 1000)
    fake.add("doc", a, "한" * 1000 + "x" * 50000 + "한" * 1000)
    b = fake.add("doc", "root", "한" * 2000)
    fake.add("doc", b, "한" * 2000)
    b_last = fake.add("doc", b, "한" * 2000)
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    index = NodeIndex.from_doc_read("doc", client.doc_read("doc"))
    targets = [
        sources.render_target(index, {}, sources.SelectedNode(NodeRef("doc", node), 0,
                                                               "self_and_descendants"), ())
        for node in (a, b)
    ]
    raw = [sum(estimate_tokens(line.text + (line.note or "")) for line in block.lines)
           for block in targets]
    assert raw[0] > raw[1]
    assert targets[0].tokens < targets[1].tokens
    rendered = [sum(prompt._line_weight(line) for line in block.lines) / 7 for block in targets]
    assert 4000 < rendered[0] < 4200 and 6000 < rendered[1] < 6200
    spec = replace(task(), unit="aggregate", input_token_range=(1, 20000))
    messages, _ = prompt.build_messages(spec, None, None, targets, [], NOW, 100000)
    limit = estimate_tokens("".join(m["content"] for m in messages)) - 100
    messages, fit = prompt.build_messages(replace(spec, input_token_range=(1, limit)),
                                          None, None, targets, [], NOW, 100000)
    text = messages[1]["content"]
    assert fit.truncated and not fit.over_target
    assert f"[doc/{targets[0].lines[-1].ref.node_id}]" in text
    assert f"[doc/{b_last}]" not in text
    assert f"[doc/{targets[1].lines[1].ref.node_id}]" in text


def test_reference_headers_empty_and_nonempty_and_target_scope():
    fake = FakeDynalist()
    fake.add_doc("doc")
    node = fake.add("doc", "root", "present")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    index = NodeIndex.from_doc_read("doc", client.doc_read("doc"))
    selected = sources.SelectedNode(NodeRef("doc", node), 0, "self_and_children")
    target = sources.render_target(index, {}, selected, ())
    empty_source = SourceSpec("empty-doc", "parent", "descendants", "created_last_7d")
    full_source = SourceSpec("doc", "root", "self_and_descendants", "fixed_context")
    refs = [ReferenceBlock(0, empty_source, (), 0),
            sources.render_reference(index, {}, 1, full_source, [(index.get(node), 0)])]
    spec = replace(task(), input_token_range=(1, 10000))
    messages, fit = prompt.build_messages(spec, None, None, [target], refs, NOW, 40000)
    text = messages[1]["content"]
    assert not fit.truncated
    assert "(context_scope=self_and_children)" in text
    assert "### 참조 1: empty-doc/parent (scope=descendants, time=created_last_7d)\n해당 노드 없음" in text
    assert "### 참조 2: doc/root (scope=self_and_descendants, time=fixed_context)" in text
    assert f"[doc/{node}] present" in text
    assert fit.tokens == estimate_tokens("".join(m["content"] for m in messages))


@pytest.mark.parametrize("entry", ["pipeline", "command"])
@pytest.mark.parametrize("failure", ["unit", "stage"])
def test_partial_stage_flushes_and_terminates_run(tmp_path, monkeypatch, entry, failure):
    settings = make_settings(tmp_path)
    settings.domains = {}
    settings.feedback_rules = FeedbackRulesCfg(tasks=[task()])
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    fake = FakeDynalist()
    fake.add_doc("doc")
    fake.add("doc", "root", "first")
    fake.add("doc", "root", "second")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None)
    backend = FakeLLM()
    backend.set("feedback_rule", {"summary": "example", "feedback": ["advice"], "evidence": []})
    llm = LLMClient(backend=backend, sleep=lambda _: None)
    if failure == "unit":
        original = sources.render_target
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("synthetic")
            return original(*args, **kwargs)

        monkeypatch.setattr(sources, "render_target", fail_second)
    else:
        original = store.heartbeat
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("synthetic")
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "heartbeat", fail_second)
    clock.set_now(NOW)
    try:
        if entry == "pipeline":
            result = run_pipeline(conn, settings, client, llm, run_date=NOW.date().isoformat())
            assert result.status == "partial"
        else:
            result = service.run_feedback_command(settings, dynalist=client, llm=llm, conn=conn)
            assert result.exit_code == 1
        run = conn.execute("SELECT status,error FROM feedback_task_run").fetchone()
        assert tuple(run) == ("failed", "unit_failed" if failure == "unit" else "interrupted")
        assert conn.execute("SELECT status FROM feedback_output").fetchone()[0] == "written"
        assert conn.execute("SELECT count(*) FROM write_op WHERE status!='done'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM feedback_task_lock").fetchone()[0] == 0
    finally:
        clock.set_now(None)
        conn.close()
