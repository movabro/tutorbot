import hashlib
import json
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tutorbot.config import Settings, _simple_toml_parse
from tutorbot.dynalist.tree import Node, NodeIndex
from tutorbot.feedback.loader import parse_feedback_rules
from tutorbot.feedback.models import BotWriteMark, RulesParseFailure, SourceSpec
from tutorbot.feedback.persona import resolve_persona
from tutorbot.feedback.schedule import decide, first_run_lower_bound
from tutorbot.feedback.sources import (
    compute_bot_flags,
    render_target,
    resolve_references,
    resolve_selection,
)
from tutorbot.feedback.timefilter import (
    effective_modified_ms,
    matches_time,
    own_text_hash,
    resolve_time_bounds,
)
from tutorbot.parse.classify import Classifier
from tutorbot.parse.tags import node_tag_set, tags_match


def task(**updates):
    raw = {"id": "test", "purpose": "test", "unit": "per_node", "schedule": "daily",
           "selection": [{"file_id": "doc", "parent_node_id": "root",
                          "scope": "descendants", "time": "fixed_context"}], "output": {}}
    raw.update(updates)
    return parse_feedback_rules({"tasks": [raw]}, base_dir=Path(".")).tasks[0]


def test_template_fallback_matches_standard_parser():
    template = Path("docs/templates/feedback-rules.example.toml")
    data = template.read_bytes()
    golden = json.loads(Path("tests/data/feedback_rules_example.golden.json").read_text())
    assert golden["_meta"]["template_sha256"] == hashlib.sha256(data).hexdigest(), "golden stale"
    parsed = _simple_toml_parse(data.decode("utf-8"))
    assert parsed == golden["data"]
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            tomllib = None
    if tomllib:
        assert parsed == tomllib.loads(data.decode("utf-8"))
    cfg = parse_feedback_rules(parsed["feedback_rules"], base_dir=Path("."), allow_placeholders=True)
    assert len(cfg.tasks) == 4
    assert all(t.valid for t in cfg.tasks)


def test_fallback_isolates_unsupported_and_rejects_unclosed_array():
    data = '[feedback_rules]\nx = {a = 1}\n[llm]\nrequests_per_minute = 7\n'
    parsed = _simple_toml_parse(data)
    assert parsed["feedback_rules"] == RulesParseFailure(2, "inline table")
    assert parsed["llm"]["requests_per_minute"] == 7
    assert parse_feedback_rules(parsed["feedback_rules"], base_dir=Path(".")).issues[0].code == "FR-G009"
    with pytest.raises(ValueError, match="line 2"):
        _simple_toml_parse("[feedback_rules]\nx = [1, 2")
    assert _simple_toml_parse("a = [true, false]\n")["a"] == [True, False]


def test_settings_isolates_bad_task_and_preserves_domains():
    good = {"id": "good", "purpose": "ok", "unit": "per_node", "schedule": "daily",
            "selection": [{"file_id": "doc", "parent_node_id": "root", "scope": "descendants",
                           "time": "fixed_context"}], "output": {}}
    bad = {**good, "id": "bad", "schedule_weekday": "monday"}
    settings = Settings.model_validate({"feedback_rules": {"tasks": [bad, good]}})
    assert [t.id for t in settings.feedback_rules.valid_tasks()] == ["good"]
    assert settings.feedback_rules.file_ids() == ["doc"]
    with pytest.raises(TypeError):
        Settings.model_validate({"domains": {"work": {"persona_path": "p.md"}}})


def test_dst_calendar_and_skip():
    daily = task(schedule_time="02:30", timezone="America/New_York")
    before = datetime(2026, 3, 8, 7, 29, tzinfo=timezone.utc)
    after = before.replace(minute=30)
    assert decide(daily, before, lambda _: False).action == "not_due"
    assert decide(daily, after, lambda _: False).action == "run"
    yearly = task(schedule="yearly", schedule_month=2, schedule_day=29)
    assert decide(yearly, before, lambda _: False).skip_reason == "yearly_date_not_exist"
    quarterly = task(schedule="quarterly", schedule_quarter_anchor="middle")
    assert quarterly.valid
    assert decide(quarterly, before, lambda _: False).action == "skip"
    assert decide(quarterly, before, lambda _: True).action == "already_done"


def test_first_window_and_bot_write_correction():
    t = task(schedule="monthly")
    T = datetime.fromisoformat("2026-03-31T10:00:00+09:00")
    assert first_run_lower_bound(t, T).isoformat() == "2026-02-28T01:00:00+00:00"
    node = Node("n", content="same", modified=3000, created=1000)
    mark = BotWriteMark(own_text_hash(node), 1000)
    assert effective_modified_ms(node, mark) == 1000
    node.content = "edited"
    assert effective_modified_ms(node, mark) == 3000
    bounds = resolve_time_bounds("modified_last_24h", datetime.fromtimestamp(3, timezone.utc), "UTC", None, t)
    assert not matches_time(node, bounds)
    node.modified = 2999
    assert matches_time(node, bounds)


def test_deep_bot_flags_and_unicode_tags():
    nodes = [Node("root", children=["0"])]
    nodes.extend(Node(str(i), content="#tutorbot" if i == 1500 else "",
                      children=[str(i + 1)] if i < 3000 else []) for i in range(3001))
    index = NodeIndex("doc", nodes)
    flags = compute_bot_flags(index, Classifier(index))
    assert not flags["1499"] and flags["1500"] and flags["3000"]
    tags = node_tag_set("abc#goal ##goal", "#" + unicodedata.normalize("NFD", "한글"))
    assert tags_match(tags, frozenset({"한글"}))
    assert not tags_match(tags, frozenset({"goal"}))


def test_persona_normalization_and_symlink_escape(tmp_path):
    persona = tmp_path / "p.md"
    persona.write_bytes(b"\xef\xbb\xbfhello  \r\n\r\n")
    spec = replace(task(), base_dir=tmp_path, persona_path="p.md")
    first = resolve_persona(spec)
    persona.write_text("hello\n", encoding="utf-8")
    assert resolve_persona(spec).hash == first.hash
    assert first.text == "hello\n"
    outside = tmp_path.parent / (tmp_path.name + "-outside.md")
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(outside)
    assert resolve_persona(replace(spec, persona_path="link.md")).error == "outside_base_dir"


def test_sources_union_nested_context_and_references():
    index = NodeIndex("doc", [
        Node("root", children=["a", "bot"]),
        Node("a", "#goal A", children=["inner", "plain"]),
        Node("inner", "#goal B", children=["detail"]),
        Node("detail", "detail"),
        Node("plain", "plain"),
        Node("bot", "#tutorbot", children=["bot-child"]),
        Node("bot-child", "#goal hidden"),
    ])
    flags = compute_bot_flags(index, Classifier(index))
    source = SourceSpec("doc", "root", "descendants", "fixed_context", ("goal",),
                        "self_excluding_nested_goal_subtrees")
    ref = SourceSpec("doc", "root", "descendants", "fixed_context", ("goal",))
    spec = replace(task(), selection=(source, source), references=(ref,))
    now = datetime.now(timezone.utc)
    selected = resolve_selection(spec, {"doc": index}, {"doc": flags}, {}, now, None, now)
    assert [s.ref.node_id for s in selected] == ["a", "inner"]
    assert [line.ref.node_id for line in render_target(index, flags, selected[0], ("goal",)).lines] == ["a", "plain"]
    blocks = resolve_references(spec, {"doc": index}, {"doc": flags}, {}, now, None, now)
    assert [line.ref.node_id for line in blocks[0].lines] == ["a", "inner"]
