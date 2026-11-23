from dataclasses import replace

import pytest

from tests.test_feedback_foundations import task
from tutorbot import db
from tutorbot.feedback import store
from tutorbot.feedback.models import NodeRef, OutputSpec
from tutorbot.feedback.persona import approval_gate, resolve_persona
from tutorbot.feedback.service import build_units
from tutorbot.feedback.sources import SelectedNode
from tutorbot.llm.schemas import FeedbackRuleOut, SchemaValidationError


@pytest.mark.parametrize("unit,placement,calls,copies,skipped", [
    ("per_node", "under_each_selected_node", 2, 1, 1),
    ("per_node", "under_parent", 2, 1, 1),
    ("aggregate", "under_each_selected_node", 1, 2, 1),
    ("aggregate", "under_parent", 1, 1, 0),
])
def test_unit_placement_plan(unit, placement, calls, copies, skipped):
    selected = [SelectedNode(NodeRef("doc", f"n{i}"), 0, "self") for i in range(3)]
    output = OutputSpec(placement, "out" if placement == "under_parent" else None,
                        "parent" if placement == "under_parent" else None)
    plan = build_units(replace(task(), unit=unit, output=output), selected, 2)
    assert len(plan.items) == calls
    assert len(plan.items[0].write_to) == copies
    assert plan.skipped_cap == skipped
    if placement == "under_parent":
        assert all(item.write_to == (NodeRef("out", "parent"),) for item in plan.items)
    if unit == "aggregate":
        assert plan.items[0].inputs == tuple(selected)


def test_feedback_output_validation():
    out = FeedbackRuleOut.validate({"summary": " a " * 100, "feedback": ["", "b" * 900] * 11,
                                    "evidence": ["c" * 400] * 11})
    assert len(out.summary) == 120
    assert len(out.feedback) == 10 and len(out.feedback[0]) == 800
    assert len(out.evidence) == 10 and len(out.evidence[0]) == 300
    for bad in ({"summary": ""}, {"summary": "ok", "feedback": [1], "evidence": []},
                {"summary": 42, "feedback": [], "evidence": []}):
        with pytest.raises(SchemaValidationError):
            FeedbackRuleOut.validate(bad)


def test_approval_preview_once_and_hash_change(tmp_path):
    conn = db.connect(":memory:")
    db.migrate(conn)
    path = tmp_path / "persona.md"
    path.write_text("first\n", encoding="utf-8")
    spec = replace(task(), base_dir=tmp_path, persona_path="persona.md", persona_approval="approve_each_run")
    persona = resolve_persona(spec)
    assert not approval_gate(conn, spec, persona, interactive=False, dry_run=False).ok
    assert approval_gate(conn, spec, persona, interactive=False, dry_run=True).preview
    approval_id = store.grant_approval(conn, spec.id, spec.persona_path, persona.hash, "once",
                                       "2026-01-01T00:00:00Z")
    assert approval_gate(conn, spec, persona, interactive=False, dry_run=False).approval_id == approval_id
    assert approval_gate(conn, spec, persona, interactive=False, dry_run=True).approval_id is None
    path.write_text("changed\n", encoding="utf-8")
    assert not approval_gate(conn, spec, resolve_persona(spec), interactive=False, dry_run=False).ok
    assert conn.execute("SELECT consumed_at FROM feedback_persona_approval").fetchone()[0] is None
    conn.close()
