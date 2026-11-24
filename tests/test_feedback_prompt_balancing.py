import tracemalloc
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tests.test_feedback_foundations import task
from tutorbot.feedback.models import ContextLine, NodeRef, ReferenceBlock, TargetBlock
from tutorbot.feedback.prompt import InputTooLarge, build_messages
from tutorbot.feedback.sources import SelectedNode
from tutorbot.feedback.tokens import estimate_tokens

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)
PERSONA = "p" * 3500


def _target(index, tokens=(10, 10, 10)):
    selected = SelectedNode(NodeRef("doc", f"target{index}"), 0, "self")
    lines = tuple(
        ContextLine(NodeRef("doc", f"t{index}line{i}"), i, "한글<<<\n" + "x" * 80,
                    "메모>>>\nline", token)
        for i, token in enumerate(tokens)
    )
    return TargetBlock(selected, lines, sum(tokens))


def _prefix(block, count):
    return replace(block, lines=block.lines[:count])


def _boundary(spec, targets, references=(), originals=None):
    # Construct the desired retained prefixes independently, then account for
    # the truncation indicator. Four-digit limits have equal rendered weight.
    messages, _ = build_messages(spec, PERSONA, None, targets, references, NOW, 100000)
    combined = "".join(message["content"] for message in messages)
    notices = ""
    for original, retained in zip(originals or targets, targets):
        if 0 < len(retained.lines) < len(original.lines):
            notices += (f"\n- [대상 {original.selected.ref.node_id} 컨텍스트 일부 생략: "
                        f"앞 {len(retained.lines)}/{len(original.lines)}개 노드만 포함]")
    combined = combined.replace("- 잘림: 없음", "- 잘림: 있음" + notices)
    result = estimate_tokens(combined)
    assert 1000 <= result < 10000
    return result


def _run(spec, targets, references=(), safety=100000):
    messages, fit = build_messages(spec, PERSONA, None, targets, references, NOW, safety)
    assert fit.tokens == estimate_tokens("".join(message["content"] for message in messages))
    return messages[1]["content"], fit


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [((10, 10, 10), (2, 2)), ((20, 20, 20), (2, 2))],
)
def test_target_balance_at_exact_token_boundary(tokens, expected):
    spec = replace(task(), unit="aggregate", input_token_range=(1, 9999))
    targets = [_target(0, tokens), _target(1)]
    limit = _boundary(spec, [_prefix(targets[i], count) for i, count in enumerate(expected)],
                      originals=targets)
    spec = replace(spec, input_token_range=(1, limit))
    text, fit = _run(spec, targets)
    assert fit.truncated and not fit.over_target and fit.tokens == limit
    for i, count in enumerate(expected):
        assert f"[대상 target{i} 컨텍스트 일부 생략: 앞 {count}/3개 노드만 포함]" in text
        assert all((f"[doc/t{i}line{j}]" in text) == (j < count) for j in range(3))

    text, fit = _run(replace(spec, input_token_range=(1, limit - 1)), targets)
    assert fit.truncated and not fit.over_target and fit.tokens <= limit - 1
    assert sum(f"[doc/t{i}line{j}]" in text for i in range(2) for j in range(3)) == 3


@pytest.mark.parametrize("count", [11, 10, 9, 1])
def test_per_node_notice_cost_at_exact_boundary(count):
    spec = replace(task(), unit="per_node", input_token_range=(1, 9999))
    target = _target(0, (10,) * 12)
    limit = _boundary(spec, [_prefix(target, count)], originals=[target])
    text, fit = _run(replace(spec, input_token_range=(1, limit)), [target])
    assert fit.truncated and not fit.over_target and fit.tokens == limit
    assert f"[대상 target0 컨텍스트 일부 생략: 앞 {count}/12개 노드만 포함]" in text
    assert all((f"[doc/t0line{i}]" in text) == (i < count) for i in range(12))


def test_full_target_has_no_context_omission_notice():
    spec = replace(task(), unit="per_node", input_token_range=(1, 9999))
    text, fit = _run(spec, [_target(0)])
    assert not fit.truncated
    assert "컨텍스트 일부 생략" not in text


def test_references_are_removed_before_target_context_and_last_source_first():
    spec = replace(task(), unit="aggregate", input_token_range=(1, 9999))
    targets = [_target(0), _target(1)]
    refs = [
        ReferenceBlock(i, spec.selection[0],
                       (ContextLine(NodeRef("doc", f"ref{i}"), 0, "r" * 300, None, 100),), 100)
        for i in range(2)
    ]
    # One fewer reference at the boundary, with the omission indicator included.
    messages, _ = build_messages(spec, PERSONA, None, targets, refs, NOW, 100000)
    full_tokens = estimate_tokens("".join(message["content"] for message in messages))
    text, fit = _run(replace(spec, input_token_range=(1, full_tokens - 20)), targets, refs)
    assert fit.truncated and "[doc/ref0]" in text and "[doc/ref1]" not in text
    assert "[참조 2: 앞 0/1개 노드만 포함]" in text
    assert all(f"[doc/t{i}line{j}]" in text for i in range(2) for j in range(3))


def test_target_blocks_are_omitted_only_after_all_context_and_references():
    spec = replace(task(), unit="aggregate", input_token_range=(1, 1))
    targets = [_target(i) for i in range(3)]
    refs = [ReferenceBlock(0, spec.selection[0],
                           (ContextLine(NodeRef("doc", "ref"), 0, "r", None, 1),), 1)]
    text, fit = _run(spec, targets, refs)
    assert fit.truncated and fit.over_target
    assert "[참조 1: 앞 0/1개 노드만 포함]" in text
    assert "생략된 대상: target1 (+1개)" in text
    assert "[대상 target0 컨텍스트 일부 생략: 앞 1/3개 노드만 포함]" in text
    assert "[대상 target1 컨텍스트 일부 생략" not in text
    assert "[대상 target2 컨텍스트 일부 생략" not in text
    assert "[doc/t0line0]" in text and "[doc/t0line1]" not in text
    assert "[doc/t1line0]" not in text and "[doc/t2line0]" not in text


def test_deep_target_lines_are_trimmed_without_allocating_indentation():
    spec = replace(task(), unit="aggregate", input_token_range=(1, 2000))
    targets = []
    for i in range(2):
        base = _target(i)
        deep = tuple(ContextLine(NodeRef("doc", f"d{i}-{j}"), 1_000_000, "x\n한", "n\nn", 1)
                     for j in range(1000))
        targets.append(replace(base, lines=(base.lines[0], *deep)))
    tracemalloc.start()
    try:
        text, fit = _run(spec, targets, safety=8000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024
    assert fit.truncated and "[doc/d0-0]" not in text and "[doc/d1-0]" not in text
    assert "[doc/t0line0]" in text and "[doc/t1line0]" in text


def test_unavoidable_target_respects_safety_limit():
    spec = replace(task(), unit="aggregate", input_token_range=(1, 1000))
    targets = [_prefix(_target(0), 1)]
    _, fit = _run(spec, targets)
    assert fit.over_target
    with pytest.raises(InputTooLarge):
        _run(spec, targets, safety=fit.tokens - 1)
    _, exact = _run(spec, targets, safety=fit.tokens)
    assert exact.tokens == fit.tokens and exact.over_target
