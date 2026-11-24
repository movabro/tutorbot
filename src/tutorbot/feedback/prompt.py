"""Build bounded feedback prompts without treating document text as instructions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from heapq import heappop, heappush
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class FitResult:
    truncated: bool
    over_target: bool
    too_large: bool
    tokens: int


class InputTooLarge(ValueError):
    pass


def _text(value, limit, indent):
    value = (value or "").replace("<<<", "‹‹‹").replace(">>>", "›››")
    if len(value) > limit:
        half = (limit - 6) // 2
        value = value[:half] + "…(중략)…" + value[-half:]
    return value.replace("\n", "\n" + indent)


def _render_line(line, add, indent):
    indent(line.depth)
    add(f"- [{line.ref.file_id}/{line.ref.node_id}] ")
    pieces = _text(line.text, 2000, "").split("\n")
    add(pieces[0])
    for piece in pieces[1:]:
        add("\n")
        indent(line.depth + 1)
        add(piece)
    if line.note:
        add("\n")
        indent(line.depth)
        add("  > ")
        pieces = _text(line.note, 4000, "").split("\n")
        add(pieces[0])
        for piece in pieces[1:]:
            add("\n")
            indent(line.depth + 1)
            add("> ")
            add(piece)
    add("\n")


def _line_weight(line):
    weight = 0

    def add(text):
        nonlocal weight
        wide = sum(ord(char) >= 0x1100 for char in text)
        weight += 7 * wide + 2 * (len(text) - wide)

    def indent(depth):
        nonlocal weight
        weight += 4 * depth

    _render_line(line, add, indent)
    return weight


def _target_header(block, index):
    return (f"### 대상 {index + 1}: [{block.selected.ref.file_id}/{block.selected.ref.node_id}] "
            f"(context_scope={block.selected.context_scope})\n")


def _string_weight(text):
    wide = sum(ord(char) >= 0x1100 for char in text)
    return 7 * wide + 2 * (len(text) - wide)


def _target_context_notice(block, count):
    if 0 < count < len(block.lines):
        return (f"\n- [대상 {block.selected.ref.node_id} 컨텍스트 일부 생략: "
                f"앞 {count}/{len(block.lines)}개 노드만 포함]")
    return ""


def build_messages(task, persona_text, unit, targets, references, T, safety_limit):
    system = (Path(__file__).parent.parent / "llm/prompts/feedback_rule.md").read_text(encoding="utf-8")
    if persona_text:
        system += "\n## 페르소나(사용자 제공)\n" + persona_text
    system += '\n출력은 JSON 하나: {"summary": str, "feedback": [str], "evidence": [str]}'
    target_lines = [list(block.lines) for block in targets]
    ref_lines = [list(block.lines) for block in references]
    truncated = any(len(line.text or "") > 2000 or len(line.note or "") > 4000
                    for block in (*targets, *references) for line in block.lines)

    def render(materialize=False, raw_weight=False):
        # Count indentation arithmetically: never allocate deep prefixes while
        # deciding which lines to discard. Token weights are 7 for wide and
        # 2 for narrow characters, corresponding to estimate_tokens.
        parts = [] if materialize else None
        weight = 0

        def add(text):
            nonlocal weight
            wide = sum(ord(char) >= 0x1100 for char in text)
            weight += 7 * wide + 2 * (len(text) - wide)
            if parts is not None:
                parts.append(text)

        def indent(depth):
            nonlocal weight
            weight += 4 * depth
            if parts is not None:
                parts.append("  " * depth)

        add(system)
        local = T.astimezone(ZoneInfo(task.timezone))
        period = ""
        if task.schedule.kind == "weekly" and task.schedule.week_end:
            end = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
            period = f"\n- 종료된 주: [{(end - timedelta(days=7)).isoformat()}, {end.isoformat()})"
        add(f"## 작업\n- 작업 ID: {task.id}\n- 목적: {task.purpose}\n- 분야: {task.domain}"
                f"\n- 평가 기준 시각: {local.isoformat()} ({task.timezone}){period}"
                f"\n- 단위: {task.unit}\n- 목표 분량: 입력 {task.input_token_range[0]}~{task.input_token_range[1]}, "
                f"출력 {task.output_token_range[0]}~{task.output_token_range[1]} 토큰"
                f"\n## 근거 표기 지침\n{task.evidence}"
                f"\n## 입력 상태\n- 잘림: {'있음' if truncated else '없음'}"
                )
        for i, original in enumerate(references):
            if len(ref_lines[i]) < len(original.lines):
                add(f"\n- [참조 {original.source_index + 1}: 앞 {len(ref_lines[i])}/{len(original.lines)}개 노드만 포함]")
        for i, original in enumerate(targets):
            add(_target_context_notice(original, len(target_lines[i])))
        omitted = [targets[i].selected.ref.node_id for i, lines in enumerate(target_lines) if not lines]
        if omitted:
            add(f"\n- 생략된 대상: {omitted[0]}" + (f" (+{len(omitted) - 1}개)" if len(omitted) > 1 else ""))
        add("\n## 피드백 대상\n<<<TARGETS\n")
        for i, lines in enumerate(target_lines):
            if lines:
                add(_target_header(targets[i], i))
                for line in lines:
                    _render_line(line, add, indent)
        add("TARGETS>>>\n## 참조 자료 (피드백 대상 아님)\n<<<REFERENCES\n")
        for i, lines in enumerate(ref_lines):
            if lines or not references[i].lines:
                source = references[i].source
                add(f"### 참조 {references[i].source_index + 1}: {source.file_id}/{source.parent_node_id} "
                    f"(scope={source.scope}, time={source.time})\n")
                if not lines:
                    add("해당 노드 없음\n")
                for line in lines:
                    _render_line(line, add, indent)
        add("REFERENCES>>>")
        if raw_weight:
            return weight
        tokens = (weight + 6) // 7
        if parts is None:
            return tokens
        # The first fragment is the system message.
        return [{"role": "system", "content": parts[0]}, {"role": "user", "content": "".join(parts[1:])}]

    maximum = task.input_token_range[1]

    def shrink(lines, minimum):
        nonlocal truncated
        original = lines[:]
        truncated = True
        # Find the longest retained prefix that fits; if even the minimum
        # cannot fit, keep that minimum and continue with the next block.
        lo, hi = minimum, len(original) - 1
        lines[:] = original[:minimum]
        if render() > maximum:
            return
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lines[:] = original[:mid]
            if render() <= maximum:
                lo = mid
            else:
                hi = mid - 1
        lines[:] = original[:lo]

    while render() > maximum:
        if any(ref_lines):
            shrink(next(lines for lines in reversed(ref_lines) if lines), 0)
        elif any(len(lines) > 1 for lines in target_lines):
            truncated = True
            weight = render(raw_weight=True)
            heap = []
            for i, lines in enumerate(target_lines):
                if len(lines) > 1:
                    block_weight = _string_weight(_target_header(targets[i], i))
                    block_weight += sum(_line_weight(line) for line in lines)
                    heappush(heap, (-block_weight, i))
            # Re-select after every removal; ties go to the earlier block.
            # Count deep indentation arithmetically rather than materializing it.
            while heap and weight > 7 * maximum:
                neg_weight, i = heappop(heap)
                lines = target_lines[i]
                old_notice = _target_context_notice(targets[i], len(lines))
                removed = lines.pop()
                removed_weight = _line_weight(removed)
                weight -= removed_weight
                weight += _string_weight(_target_context_notice(targets[i], len(lines)))
                weight -= _string_weight(old_notice)
                if len(lines) > 1:
                    heappush(heap, (neg_weight + removed_weight, i))
        elif sum(bool(lines) for lines in target_lines) > 1:
            shrink(next(lines for lines in reversed(target_lines) if lines), 0)
        else:
            break
        truncated = True
    tokens = render()
    fit = FitResult(truncated, tokens > maximum, tokens > safety_limit, tokens)
    if fit.too_large:
        raise InputTooLarge("input_too_large")
    return render(materialize=True), fit
