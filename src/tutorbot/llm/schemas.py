"""Validation schemas for LLM task outputs (spec §9.2).

Works without pydantic installed via explicit validation classes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class SchemaValidationError(ValueError):
    """Raised when LLM JSON output fails schema or constraint validation."""


@dataclass
class GoalMetaOut:
    title: str
    level: str | None = None
    deadline: str | None = None

    @classmethod
    def validate(cls, d: Any) -> "GoalMetaOut":
        if not isinstance(d, dict):
            raise SchemaValidationError("goal must be an object")
        title = str(d.get("title") or "").strip()
        if not title:
            raise SchemaValidationError("goal.title must be non-empty")
        level = d.get("level")
        if level is not None:
            level = str(level).strip() or None
        deadline = d.get("deadline")
        if deadline is not None:
            deadline = str(deadline).strip() or None
        return cls(title=title, level=level, deadline=deadline)


@dataclass
class ConceptItemOut:
    key: str
    parent_key: str | None
    name: str
    description: str
    prereq_keys: list[str] = field(default_factory=list)
    order: int = 1

    @classmethod
    def validate(cls, d: Any) -> "ConceptItemOut":
        if not isinstance(d, dict):
            raise SchemaValidationError("concept item must be an object")
        key = str(d.get("key") or "").strip()
        if not key:
            raise SchemaValidationError("concept.key is required")
        pk = d.get("parent_key")
        parent_key = str(pk).strip() if pk not in (None, "") else None
        name = str(d.get("name") or "").strip()
        if not name:
            raise SchemaValidationError(f"concept({key}).name is required")
        if len(name) > 25:
            name = name[:25]
        desc = str(d.get("description") or "").strip()
        if len(desc) > 80:
            desc = desc[:80]
        prereqs_raw = d.get("prereq_keys") or []
        if not isinstance(prereqs_raw, list):
            raise SchemaValidationError(f"concept({key}).prereq_keys must be a list")
        prereq_keys = [str(x).strip() for x in prereqs_raw if str(x).strip()]
        try:
            order = int(d.get("order", 1))
        except (TypeError, ValueError) as e:
            raise SchemaValidationError(f"concept({key}).order must be int") from e
        return cls(
            key=key,
            parent_key=parent_key,
            name=name,
            description=desc,
            prereq_keys=prereq_keys,
            order=order,
        )


@dataclass
class T1CurriculumOut:
    goal: GoalMetaOut
    concepts: list[ConceptItemOut]
    depths: dict[str, int] = field(default_factory=dict)
    is_leaf: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def validate(cls, data: Any, strict_leaf_count: bool = False) -> "T1CurriculumOut":
        if not isinstance(data, dict):
            raise SchemaValidationError("T1 root must be a JSON object")
        goal = GoalMetaOut.validate(data.get("goal"))
        raw_concepts = data.get("concepts")
        if not isinstance(raw_concepts, list) or not raw_concepts:
            raise SchemaValidationError("concepts must be a non-empty list")
        concepts = [ConceptItemOut.validate(c) for c in raw_concepts]

        by_key: dict[str, ConceptItemOut] = {}
        for c in concepts:
            if c.key in by_key:
                raise SchemaValidationError(f"duplicate concept key: {c.key}")
            by_key[c.key] = c

        # Verify parent_key references, duplicate sibling names, and depth <= 3
        siblings_names: dict[str | None, set[str]] = {}
        children_count: dict[str, int] = {c.key: 0 for c in concepts}
        for c in concepts:
            if c.parent_key is not None and c.parent_key not in by_key:
                raise SchemaValidationError(f"concept {c.key} references unknown parent_key {c.parent_key}")
            if c.parent_key is not None:
                children_count[c.parent_key] += 1
            sibs = siblings_names.setdefault(c.parent_key, set())
            norm_name = c.name.casefold()
            if norm_name in sibs:
                raise SchemaValidationError(f"duplicate concept name {c.name!r} under parent {c.parent_key!r}")
            sibs.add(norm_name)

        depths: dict[str, int] = {}

        def get_depth(k: str, visiting: set[str]) -> int:
            if k in depths:
                return depths[k]
            if k in visiting:
                raise SchemaValidationError(f"cycle in concept parent_key at {k}")
            visiting.add(k)
            pk = by_key[k].parent_key
            d = 1 if pk is None else get_depth(pk, visiting) + 1
            if d > 3:
                raise SchemaValidationError(f"concept {k} exceeds max depth 3 (depth={d})")
            visiting.remove(k)
            depths[k] = d
            return d

        for c in concepts:
            get_depth(c.key, set())
            for pq in c.prereq_keys:
                if pq not in by_key or pq == c.key:
                    raise SchemaValidationError(f"concept {c.key} has invalid prereq_key {pq!r}")

        is_leaf = {k: (cnt == 0) for k, cnt in children_count.items()}
        if strict_leaf_count:
            leaf_cnt = sum(1 for v in is_leaf.values() if v)
            if not (30 <= leaf_cnt <= 100):
                raise SchemaValidationError(f"leaf count must be 30..100, got {leaf_cnt}")

        return cls(goal=goal, concepts=concepts, depths=depths, is_leaf=is_leaf)


# ---------------- T2 Knowledge Dump Assessment ----------------
@dataclass
class DumpAssessmentItem:
    concept_id: int
    state: str  # unknown | partial | understood
    evidence: str

    @classmethod
    def validate(cls, d: Any) -> "DumpAssessmentItem":
        if not isinstance(d, dict):
            raise SchemaValidationError("assessment item must be an object")
        try:
            cid = int(d["concept_id"])
        except (KeyError, TypeError, ValueError) as e:
            raise SchemaValidationError("assessment.concept_id must be int") from e
        state = str(d.get("state") or "").strip()
        if state not in ("unknown", "partial", "understood"):
            raise SchemaValidationError(f"assessment.state must be unknown|partial|understood, got {state!r}")
        ev = str(d.get("evidence") or "").strip()[:100]
        return cls(concept_id=cid, state=state, evidence=ev)


@dataclass
class DumpIssueItem:
    concept_id: int
    verdict: str  # incorrect | uncertain
    claim: str
    correction: str
    reason: str

    @classmethod
    def validate(cls, d: Any) -> "DumpIssueItem":
        if not isinstance(d, dict):
            raise SchemaValidationError("issue item must be an object")
        try:
            cid = int(d["concept_id"])
        except (KeyError, TypeError, ValueError) as e:
            raise SchemaValidationError("issue.concept_id must be int") from e
        verdict = str(d.get("verdict") or "").strip()
        if verdict not in ("incorrect", "uncertain"):
            raise SchemaValidationError(f"issue.verdict must be incorrect|uncertain, got {verdict!r}")
        claim = str(d.get("claim") or "").strip()[:160]
        correction = str(d.get("correction") or "").strip()[:180]
        reason = str(d.get("reason") or "").strip()[:160]
        if not claim or not reason:
            raise SchemaValidationError("issue claim and reason must be non-empty")
        if verdict == "incorrect" and not correction:
            raise SchemaValidationError("incorrect issue must include a correction")
        return cls(concept_id=cid, verdict=verdict, claim=claim, correction=correction, reason=reason)


@dataclass
class T2DumpOut:
    summary: str
    assessments: list[DumpAssessmentItem] = field(default_factory=list)
    issues: list[DumpIssueItem] = field(default_factory=list)
    gap_summary: list[str] = field(default_factory=list)
    next_concepts: list[int] = field(default_factory=list)

    @classmethod
    def validate(cls, data: Any) -> "T2DumpOut":
        if not isinstance(data, dict):
            raise SchemaValidationError("T2 root must be a JSON object")
        summary = str(data.get("summary") or "").strip()[:240]
        if not summary:
            raise SchemaValidationError("T2 summary must be non-empty")
        raw_ass = data.get("assessments")
        if raw_ass is None or not isinstance(raw_ass, list):
            raise SchemaValidationError("T2 assessments must be a list")
        assessments = [DumpAssessmentItem.validate(x) for x in raw_ass]
        raw_issues = data.get("issues")
        if raw_issues is None or not isinstance(raw_issues, list):
            raise SchemaValidationError("T2 issues must be a list")
        if len(raw_issues) > 10:
            raise SchemaValidationError("T2 issues must contain at most 10 items")
        issues = [DumpIssueItem.validate(x) for x in raw_issues]
        raw_gap = data.get("gap_summary") or []
        if not isinstance(raw_gap, list):
            raise SchemaValidationError("T2 gap_summary must be a list")
        gap_summary = [str(g).strip()[:120] for g in raw_gap if str(g).strip()][:5]
        raw_next = data.get("next_concepts") or []
        if not isinstance(raw_next, list):
            raise SchemaValidationError("T2 next_concepts must be a list")
        next_concepts: list[int] = []
        for x in raw_next:
            try:
                next_concepts.append(int(x))
            except (TypeError, ValueError):
                continue
        return cls(
            summary=summary,
            assessments=assessments,
            issues=issues,
            gap_summary=gap_summary,
            next_concepts=next_concepts[:5],
        )


# ---------------- T3 Study Analysis (Mapping + Feedback + Cards) ----------------
@dataclass
class StudyConceptMapItem:
    concept_id: int
    coverage: str  # mention | partial | full
    state_suggestion: str | None = None  # partial | understood | None

    @classmethod
    def validate(cls, d: Any) -> "StudyConceptMapItem":
        if not isinstance(d, dict):
            raise SchemaValidationError("study concept item must be an object")
        try:
            cid = int(d["concept_id"])
        except (KeyError, TypeError, ValueError) as e:
            raise SchemaValidationError("concepts[].concept_id must be int") from e
        cov = str(d.get("coverage") or "").strip()
        if cov not in ("mention", "partial", "full"):
            raise SchemaValidationError(f"coverage must be mention|partial|full, got {cov!r}")
        sug = d.get("state_suggestion")
        if sug is not None:
            sug = str(sug).strip() or None
            if sug not in (None, "partial", "understood"):
                raise SchemaValidationError(f"state_suggestion must be partial|understood|null, got {sug!r}")
        return cls(concept_id=cid, coverage=cov, state_suggestion=sug)


@dataclass
class StudyFeedbackOut:
    corrections: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    next: list[str] = field(default_factory=list)

    @classmethod
    def validate(cls, d: Any) -> "StudyFeedbackOut":
        if not isinstance(d, dict):
            raise SchemaValidationError("feedback must be an object")
        corr = d.get("corrections") or []
        miss = d.get("missing") or []
        nxt = d.get("next") or []
        if not (isinstance(corr, list) and isinstance(miss, list) and isinstance(nxt, list)):
            raise SchemaValidationError("feedback fields must be lists")
        return cls(
            corrections=[str(x).strip()[:120] for x in corr if str(x).strip()][:2],
            missing=[str(x).strip()[:120] for x in miss if str(x).strip()][:2],
            next=[str(x).strip()[:120] for x in nxt if str(x).strip()][:1],
        )


@dataclass
class StudyCardOut:
    concept_id: int
    question: str
    answer_key: str

    @classmethod
    def validate(cls, d: Any) -> "StudyCardOut":
        if not isinstance(d, dict):
            raise SchemaValidationError("card item must be an object")
        try:
            cid = int(d["concept_id"])
        except (KeyError, TypeError, ValueError) as e:
            raise SchemaValidationError("card.concept_id must be int") from e
        q = str(d.get("question") or "").strip()[:150]
        a = str(d.get("answer_key") or "").strip()[:300]
        if not q or not a:
            raise SchemaValidationError("card question and answer_key must be non-empty")
        return cls(concept_id=cid, question=q, answer_key=a)


@dataclass
class T3StudyOut:
    concepts: list[StudyConceptMapItem]
    feedback: StudyFeedbackOut
    cards: list[StudyCardOut] = field(default_factory=list)
    off_curriculum: str | None = None

    @classmethod
    def validate(cls, data: Any) -> "T3StudyOut":
        if not isinstance(data, dict):
            raise SchemaValidationError("T3 root must be a JSON object")
        raw_concepts = data.get("concepts")
        if raw_concepts is None or not isinstance(raw_concepts, list):
            raise SchemaValidationError("T3 concepts must be a list")
        concepts = [StudyConceptMapItem.validate(x) for x in raw_concepts]
        feedback = StudyFeedbackOut.validate(data.get("feedback"))
        raw_cards = data.get("cards") or []
        if not isinstance(raw_cards, list):
            raise SchemaValidationError("T3 cards must be a list")
        cards = [StudyCardOut.validate(x) for x in raw_cards][:3]
        off = data.get("off_curriculum")
        if off is not None:
            off = str(off).strip() or None
        return cls(concepts=concepts, feedback=feedback, cards=cards, off_curriculum=off)


# ---------------- T4 Quiz Grading ----------------
@dataclass
class GradeItemOut:
    attempt_id: int
    score: int  # 0..5
    comment: str

    @classmethod
    def validate(cls, d: Any) -> "GradeItemOut":
        if not isinstance(d, dict):
            raise SchemaValidationError("grade item must be an object")
        try:
            aid = int(d["attempt_id"])
            score = int(d["score"])
        except (KeyError, TypeError, ValueError) as e:
            raise SchemaValidationError("grade attempt_id and score must be int") from e
        if not (0 <= score <= 5):
            raise SchemaValidationError(f"grade score must be 0..5, got {score}")
        comment = str(d.get("comment") or "").strip()[:80]
        return cls(attempt_id=aid, score=score, comment=comment)


@dataclass
class T4GradeOut:
    items: list[GradeItemOut] = field(default_factory=list)

    @classmethod
    def validate(cls, data: Any) -> "T4GradeOut":
        if isinstance(data, dict) and "items" in data:
            data = data["items"]
        if not isinstance(data, list):
            raise SchemaValidationError("T4 grade output must be a JSON array")
        seen: set[int] = set()
        items: list[GradeItemOut] = []
        for x in data:
            item = GradeItemOut.validate(x)
            if item.attempt_id in seen:
                raise SchemaValidationError(f"duplicate attempt_id {item.attempt_id} in T4 output")
            seen.add(item.attempt_id)
            items.append(item)
        return cls(items=items)


# ---------------- T5 Weekly Report ----------------
@dataclass
class T5WeeklyOut:
    summary: str
    weak_points: list[str] = field(default_factory=list)
    next_week: list[str] = field(default_factory=list)

    @classmethod
    def validate(cls, data: Any) -> "T5WeeklyOut":
        if not isinstance(data, dict):
            raise SchemaValidationError("T5 weekly output must be a JSON object")
        raw_sum = data.get("summary")
        if isinstance(raw_sum, list):
            summary = "\n".join(str(x).strip() for x in raw_sum if str(x).strip())
        else:
            summary = str(raw_sum or "").strip()
        if not summary:
            raise SchemaValidationError("T5 summary must be non-empty")
        wp = data.get("weak_points") or []
        nw = data.get("next_week") or []
        if not isinstance(wp, list) or not isinstance(nw, list):
            raise SchemaValidationError("T5 weak_points and next_week must be lists")
        return cls(
            summary=summary,
            weak_points=[str(x).strip()[:120] for x in wp if str(x).strip()][:3],
            next_week=[str(x).strip()[:120] for x in nw if str(x).strip()][:3],
        )


# ---------------- T6 Learner Profile ----------------
@dataclass
class T6ProfileOut:
    summary: str

    @classmethod
    def validate(cls, data: Any) -> "T6ProfileOut":
        if not isinstance(data, dict):
            raise SchemaValidationError("T6 profile output must be a JSON object")
        summary = str(data.get("summary") or "").strip()[:600]
        if not summary:
            raise SchemaValidationError("T6 summary must be non-empty")
        return cls(summary=summary)



