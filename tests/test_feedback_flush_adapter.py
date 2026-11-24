import pytest

from tests.fakes import FakeDynalist
from tutorbot.dynalist.client import DynalistClient, NodeNotFound, UnknownOutcome, ch_edit, ch_insert
from tutorbot.feedback.flush_adapter import FeedbackFlushAdapter


@pytest.fixture
def setup():
    fake = FakeDynalist()
    fake.add_doc("doc")
    client = DynalistClient("test", transport=fake, sleep=lambda _: None, changes_per_minute=1e9)
    return fake, FeedbackFlushAdapter(client)


def test_mixed_batch_reassembles_insert_ids(setup):
    fake, adapter = setup
    existing = fake.add("doc", "root", "old", "tb:feedback_rule:one")
    plain = fake.add("doc", "root", "plain")
    result = adapter.doc_edit("doc", [
        ch_insert("root", "existing", note="tb:feedback_rule:one"),
        ch_edit(plain, content="changed"),
        ch_insert("root", "normal"),
        ch_insert("root", "feedback", note="tb:feedback_rule_item:two"),
    ])
    assert result[0] == existing and len(result) == 3
    assert fake.docs["doc"][plain]["content"] == "changed"
    assert [fake.docs["doc"][node]["content"] for node in result[1:]] == ["normal", "feedback"]
    assert len(fake.children("doc", "root")) == 4


def test_read_failure_missing_parent_and_duplicate_marker_fail_closed(setup):
    fake, adapter = setup
    edit = [ch_insert("root", "feedback", note="tb:feedback_rule:one")]
    fake.fail("doc/read", "NotFound")
    with pytest.raises(UnknownOutcome):
        adapter.doc_edit("doc", edit)
    with pytest.raises(NodeNotFound):
        adapter.doc_edit("doc", [ch_insert("missing", "x", note="tb:feedback_rule:one")])
    fake.add("doc", "root", "a", "tb:feedback_rule:one")
    fake.add("doc", "root", "b", "tb:feedback_rule:one")
    with pytest.raises(UnknownOutcome):
        adapter.doc_edit("doc", edit)
    assert not [call for call in fake.calls if call[0] == "doc/edit"]


def test_lost_response_reconciles_on_retry(setup):
    fake, adapter = setup
    change = ch_insert("root", "feedback", note="tb:feedback_rule:one")
    fake.crash_after_apply()
    with pytest.raises(UnknownOutcome):
        adapter.doc_edit("doc", [change])
    result = adapter.doc_edit("doc", [change])
    assert len(fake.children("doc", "root")) == 1
    assert result == [fake.children("doc", "root")[0]["id"]]
