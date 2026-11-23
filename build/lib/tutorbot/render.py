"""Bot node text rendering (spec §3.4).

All bot node formatting lives in this single module.
"""
from __future__ import annotations


STATE_ICON = {
    "unknown": "⬜",
    "partial": "🟡",
    "understood": "✅",
    "mastered": "⭐",
}


def marker(kind: str, ref_id: int | str) -> str:
    return f"tb:{kind}:{ref_id}"


def curriculum_root(progress_pct: int, bot_tag: str = "tutorbot") -> str:
    return f"🤖 커리큘럼 (진도 {progress_pct}%) #{bot_tag}"


def concept_leaf(name: str, state: str = "unknown") -> str:
    icon = STATE_ICON.get(state, "⬜")
    return f"[{icon}] {name}"


def concept_branch(name: str, done_leaves: int, total_leaves: int) -> str:
    return f"📘 {name} ({done_leaves}/{total_leaves})"


def progress_report_header(date_str: str, bot_tag: str = "tutorbot") -> str:
    return f"🤖 진도 리포트 ({date_str}) #{bot_tag}"


def progress_next_line(concept_names: list[str]) -> str:
    return "➡️ 다음 추천: " + (", ".join(concept_names[:5]) if concept_names else "없음")


def quiz_root(bot_tag: str = "tutorbot") -> str:
    return f"🤖 복습 퀴즈 #{bot_tag}"


def quiz_day(date_str: str) -> str:
    return f"📅 {date_str}"


def quiz_item(attempt_id: int, question: str) -> str:
    return f"[Q{attempt_id}] {question}"


def grade_node(score: int, comment: str, answer_key: str) -> str:
    return f"🤖 {score}/5 · {comment} · 모범답안: {answer_key}"


def expire_title(existing_content: str) -> str:
    if existing_content.endswith("⌛"):
        return existing_content
    return f"{existing_content} ⌛"


def feedback_header(date_str: str, bot_tag: str = "tutorbot") -> str:
    return f"🤖 피드백 ({date_str}) #{bot_tag}"


def dump_feedback_header(date_str: str, bot_tag: str = "tutorbot") -> str:
    return f"🤖 지식 덤프 평가 ({date_str}) #{bot_tag}"


def dump_feedback_summary(summary: str) -> str:
    return f"🧾 평가 요약: {summary.strip()}"


def dump_issue_lines(verdict: str, concept_name: str, claim: str, correction: str, reason: str) -> list[str]:
    if verdict == "incorrect":
        return [
            f"❌ 오개념 ({concept_name}): {claim}",
            f"✅ 정정: {correction}",
            f"💡 이유: {reason}",
        ]
    return [
        f"🔎 확인 필요 ({concept_name}): {claim}",
        f"💡 이유: {reason}",
    ]


def dump_curriculum_change(concept_name: str, old_state: str, new_state: str, reason: str) -> str:
    return (
        f"📘 커리큘럼 상태: {concept_name} "
        f"{STATE_ICON.get(old_state, '⬜')}→{STATE_ICON.get(new_state, '⬜')} ({reason})"
    )


def dump_no_curriculum_change() -> str:
    return "📘 커리큘럼 상태 변경 없음"


def dump_gap_line(gap: str) -> str:
    return f"🔍 보완할 점: {gap}"


def dump_next_concepts(names: list[str]) -> str:
    return "➡️ 다음 학습 추천: " + (", ".join(names) if names else "없음")


def feedback_lines(
    corrections: list[str],
    missing: list[str],
    next_steps: list[str],
    mapped_concepts: list[str],
) -> list[str]:
    """Build up to 5 child lines (spec §3.4 & §9.2 T3):
    corrections + missing + next <= 4 lines (priority: corrections -> next -> missing),
    plus 1 mapping line if mapped_concepts is non-empty. Each line <= 120 chars.
    """
    corr = [f"✏️ 교정: {c.strip()}"[:120] for c in corrections[:2] if c and c.strip()]
    nxt = [f"➡️ 다음: {n.strip()}"[:120] for n in next_steps[:1] if n and n.strip()]
    miss = [f"🔑 빠진 핵심: {m.strip()}"[:120] for m in missing[:2] if m and m.strip()]

    budget = 4
    picked_corr = corr[:budget]
    budget -= len(picked_corr)
    picked_nxt = nxt[:budget]
    budget -= len(picked_nxt)
    picked_miss = miss[:budget]

    # Output in logical order: corrections, missing, next, mapping
    lines = picked_corr + picked_miss + picked_nxt
    if mapped_concepts:
        lines.append(f"🧩 매핑: {', '.join(mapped_concepts)}"[:120])
    return lines[:5]


def weekly_root(bot_tag: str = "tutorbot") -> str:
    return f"🤖 주간 리포트 #{bot_tag}"


def weekly_report_header(iso_week: str, progress_pct: int, study_count: int, quiz_acc_pct: int) -> str:
    return f"📊 {iso_week} (진도 {progress_pct}% · 공부 {study_count}건 · 퀴즈 {quiz_acc_pct}%)"


def warning_node(message: str, bot_tag: str = "tutorbot") -> str:
    return f"🤖 ⚠️ {message} #{bot_tag}"
