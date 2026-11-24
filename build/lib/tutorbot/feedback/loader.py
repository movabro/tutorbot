"""Parse and validate feedback rules without accessing user documents."""
from __future__ import annotations

import calendar
import re
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    ConfigIssue,
    FeedbackRulesCfg,
    OutputSpec,
    RulesParseFailure,
    ScheduleSpec,
    SourceSpec,
    TaskSpec,
)

EVIDENCE = "전달된 문서·노드 ID를 근거로 제시하고, 없거나 잘린 내용은 모름 또는 잘림으로 표시"
TASK_KEYS = {
    "id", "purpose", "domain", "persona_path", "persona_approval", "unit",
    "schedule", "schedule_time", "schedule_weekday", "schedule_day",
    "schedule_month", "schedule_quarter_anchor", "schedule_week_end", "timezone", "input_token_range",
    "output_token_range", "evidence", "selection", "references", "output",
}
GLOBAL_KEYS = {
    "enabled", "max_llm_calls_per_run", "max_units_per_task",
    "stale_lock_minutes", "safety_input_token_limit", "tasks",
}
TIMES = {
    "fixed_context", "created_last_24h", "created_today", "created_last_7d",
    "created_since_last_run", "modified_today", "modified_last_24h",
    "modified_last_7d", "modified_since_last_run",
    "created_since_last_success", "modified_since_last_success",
}
SCOPES = {"self", "descendants", "self_and_descendants"}
CONTEXTS = {"self", "self_and_descendants", "self_and_children", "self_excluding_nested_goal_subtrees"}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_feedback_rules(raw, *, base_dir: Path, source: str = "config.toml",
                         default_timezone: str = "Asia/Seoul", is_rules_file: bool = False,
                         allow_placeholders: bool = False) -> FeedbackRulesCfg:
    cfg = FeedbackRulesCfg(source=source, base_dir=Path(base_dir))

    def global_issue(code, message, path="feedback_rules"):
        cfg.issues.append(ConfigIssue(code, "error", path, message))

    if isinstance(raw, RulesParseFailure):
        global_issue("FR-G007" if is_rules_file else "FR-G009",
                     f"unsupported TOML construct in fallback parser (feedback_rules): line {raw.line} "
                     f"({raw.construct}); install tomli")
        return cfg
    if raw is None:
        return cfg
    if not isinstance(raw, dict):
        global_issue("FR-G001", "feedback_rules must be a table")
        return cfg
    for key in raw.keys() - GLOBAL_KEYS:
        global_issue("FR-G003", f"unknown key '{key}'", f"feedback_rules.{key}")
    for key in GLOBAL_KEYS - {"tasks"}:
        if key not in raw:
            continue
        if is_rules_file:
            global_issue("FR-G006", f"scalar key '{key}' is only allowed in config.toml")
            continue
        value = raw[key]
        limits = {"max_llm_calls_per_run": (1, 500), "max_units_per_task": (1, 200),
                  "stale_lock_minutes": (10, 10080), "safety_input_token_limit": (1000, 2000000)}
        if key == "enabled":
            valid = type(value) is bool
        else:
            lo, hi = limits[key]
            valid = type(value) is int and lo <= value <= hi
        if valid:
            setattr(cfg, key, value)
        else:
            global_issue("FR-G004", f"invalid {key}", f"feedback_rules.{key}")
    if cfg.max_units_per_task > cfg.max_llm_calls_per_run:
        global_issue("FR-G005", "max_units_per_task must be <= max_llm_calls_per_run")
    tasks = raw.get("tasks", [])
    if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks):
        global_issue("FR-G002", "feedback_rules.tasks must be an array of tables")
        return cfg
    if len(tasks) > 50:
        global_issue("FR-G008", f"at most 50 tasks are allowed, got {len(tasks)}")
    counts = Counter(t.get("id") for t in tasks if isinstance(t.get("id"), str))

    for index, data in enumerate(tasks):
        issues: list[ConfigIssue] = []
        prefix = f"feedback_rules.tasks[{index}]"

        def issue(code, field, message, severity="error", disable=True, *, issues=issues, prefix=prefix):
            issues.append(ConfigIssue(code, severity, f"{prefix}.{field}", message, disable))

        for key in data.keys() - TASK_KEYS:
            issue("FR-T099", key, f"unknown key '{key}'")
        task_id = data.get("id")
        if not isinstance(task_id, str) or not task_id:
            issue("FR-T010", "id", "id is required")
            task_id = ""
        elif not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", task_id):
            issue("FR-T011", "id", "invalid id")
        if task_id and counts[task_id] > 1:
            issue("FR-T012", "id", f"duplicate id '{task_id}'")
        purpose = data.get("purpose")
        if not isinstance(purpose, str) or not 1 <= len(purpose.strip()) <= 500:
            issue("FR-T020", "purpose", "purpose is required (1..500 characters)")
            purpose = ""
        domain = data.get("domain", "general")
        if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", domain):
            issue("FR-T030", "domain", "invalid domain")
            domain = "general"
        persona = data.get("persona_path")
        if persona is not None:
            unsafe = (not isinstance(persona, str) or not persona or persona.startswith(("~", ".."))
                      or "\\" in persona or "\0" in persona or Path(persona).is_absolute())
            if not unsafe:
                try:
                    unsafe = not (Path(base_dir) / persona).resolve().is_relative_to(Path(base_dir).resolve())
                except (OSError, RuntimeError):
                    unsafe = True
            if unsafe:
                issue("FR-T040", "persona_path", "persona path must be relative and inside base_dir")
            elif Path(persona).suffix not in (".md", ".txt"):
                issue("FR-T041", "persona_path", "persona path must have .md or .txt extension")
        approval = data.get("persona_approval", "user_approved_before_use" if persona else None)
        if approval is not None and approval not in ("user_approved_before_use", "always_allowed", "approve_each_run"):
            issue("FR-T042", "persona_approval", "invalid persona approval policy")
        if "persona_approval" in data and not persona:
            issue("FR-T043", "persona_approval", "persona_approval requires persona_path")
        unit = data.get("unit")
        if unit not in ("per_node", "aggregate"):
            issue("FR-T050", "unit", "unit must be per_node or aggregate")
        kind = data.get("schedule")
        if kind not in ("each_run", "daily", "weekly", "monthly", "quarterly", "yearly", "work_slots"):
            issue("FR-T060", "schedule", "invalid schedule")
        applicable = {"schedule_time": {"daily", "weekly", "monthly", "quarterly", "yearly"},
                      "schedule_weekday": {"weekly"}, "schedule_day": {"monthly", "yearly"},
                      "schedule_month": {"yearly"}, "schedule_quarter_anchor": {"quarterly"},
                      "schedule_week_end": {"weekly"}}
        for key, allowed in applicable.items():
            if key in data and (not isinstance(kind, str) or kind not in allowed):
                issue("FR-T062", key, f"{key} is not applicable to schedule {kind!r}")
        time = data.get("schedule_time", "00:00")
        if not isinstance(time, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", time):
            issue("FR-T061", "schedule_time", "schedule_time must be HH:MM")
            time = "00:00"
        weekday = data.get("schedule_weekday", "sunday")
        if weekday not in WEEKDAYS:
            issue("FR-T063", "schedule_weekday", "invalid weekday")
            weekday = "sunday"
        day = data.get("schedule_day", 31 if kind == "yearly" else None)
        if day is not None and (type(day) is not int or not 1 <= day <= 31):
            issue("FR-T064", "schedule_day", "schedule_day must be 1..31")
            day = None
        month = data.get("schedule_month", 12)
        if type(month) is not int or not 1 <= month <= 12:
            issue("FR-T065", "schedule_month", "schedule_month must be 1..12")
            month = 12
        anchor = data.get("schedule_quarter_anchor", "end")
        invalid_anchor = None
        if anchor not in ("start", "end"):
            issue("FR-T066", "schedule_quarter_anchor", "quarter anchor must be start or end", disable=False)
            invalid_anchor, anchor = str(anchor), "end"
        if kind == "yearly" and day and day > calendar.monthrange(2028, month)[1]:
            issue("FR-T067", "schedule_day", f"schedule_month={month}, schedule_day={day} never exists",
                  severity="warning", disable=False)
        zone = data.get("timezone", default_timezone)
        try:
            ZoneInfo(zone)
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            issue("FR-T070", "timezone", "unknown timezone")
            zone = "Asia/Seoul"
        week_end = data.get("schedule_week_end", False)
        if type(week_end) is not bool:
            issue("FR-T068", "schedule_week_end", "schedule_week_end must be boolean")
            week_end = False
        if week_end and ("schedule_weekday" in data or "schedule_time" in data):
            issue("FR-T068", "schedule_week_end", "week_end cannot be combined with weekday or time")
        if kind == "work_slots" and zone != "Asia/Seoul":
            issue("FR-T070", "timezone", "work_slots requires Asia/Seoul")
        ranges = []
        for key, default, cap, code in (("input_token_range", (1000, 4000), 200000, "FR-T080"),
                                         ("output_token_range", (200, 700), 32000, "FR-T081")):
            value = data.get(key, default)
            if (not isinstance(value, (list, tuple)) or len(value) != 2
                    or any(type(v) is not int for v in value) or not 1 <= value[0] <= value[1] <= cap):
                issue(code, key, "invalid token range")
                value = default
            ranges.append(tuple(value))
        evidence = data.get("evidence", EVIDENCE)
        if not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 1000:
            issue("FR-T090", "evidence", "evidence must be a non-empty string of at most 1000 characters")
            evidence = EVIDENCE

        def check_id(value, path, output=False):
            code = "FR-O013" if output else "FR-S010"
            if isinstance(value, str) and ("DESIGN_ONLY" in value or re.search(r"<.*?>", value)):
                issue("FR-O013" if output else "FR-S011", path, "ID is a placeholder",
                      "warning" if allow_placeholders else "error", not allow_placeholders)
            elif not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                issue(code, path, "ID is required and must be a valid Dynalist ID")

        def sources(name, *, data=data, kind=kind):
            values = data.get(name, [])
            if not isinstance(values, list) or any(not isinstance(v, dict) for v in values) or (
                    len(values) > 20 or (name == "selection" and not values)):
                issue("FR-T100" if name == "selection" else "FR-T101", name, "invalid source array")
                return ()
            result = []
            for j, entry in enumerate(values):
                path = f"{name}[{j}]"
                for key in entry.keys() - {"file_id", "parent_node_id", "scope", "time", "tags_any", "context_scope"}:
                    issue("FR-S099", f"{path}.{key}", f"unknown key '{key}'")
                for key in ("file_id", "parent_node_id"):
                    check_id(entry.get(key), f"{path}.{key}")
                scope = entry.get("scope")
                if not isinstance(scope, str) or scope not in SCOPES:
                    issue("FR-S020", f"{path}.scope", "invalid scope")
                if scope == "self" and entry.get("parent_node_id") == "root":
                    issue("FR-S021", f"{path}.scope", "root cannot use scope self")
                source_time = entry.get("time")
                if not isinstance(source_time, str) or source_time not in TIMES:
                    issue("FR-S040", f"{path}.time", "invalid time filter")
                tags = entry.get("tags_any", [])
                if not isinstance(tags, list) or len(tags) > 20 or any(not isinstance(t, str) for t in tags):
                    issue("FR-S030", f"{path}.tags_any", "tags_any must be a list of strings")
                    tags = []
                for tag in tags:
                    if tag.startswith("#"):
                        issue("FR-S031", f"{path}.tags_any", "tag must not start with #")
                    elif not 1 <= len(tag) <= 64 or re.search(r"[\s,.\#!@()\[\];:?]", tag):
                        issue("FR-S032", f"{path}.tags_any", "invalid tag")
                context = entry.get("context_scope", "self_and_descendants" if name == "selection" else None)
                if name == "references" and "context_scope" in entry:
                    issue("FR-S051", f"{path}.context_scope", "context_scope is only allowed in selection")
                elif name == "selection" and (not isinstance(context, str) or context not in CONTEXTS):
                    issue("FR-S050", f"{path}.context_scope", "invalid context_scope")
                if context == "self_excluding_nested_goal_subtrees" and not tags:
                    issue("FR-S052", f"{path}.context_scope", "nested goals require tags_any")
                if name == "references" and tags:
                    issue("FR-W005", path, "only tagged nodes themselves are read", "warning", False)
                if name == "selection" and kind == "each_run":
                    if source_time == "fixed_context":
                        issue("FR-W003", path, "each_run with fixed_context has recurring cost", "warning", False)
                    elif source_time not in ("created_since_last_run", "modified_since_last_run"):
                        issue("FR-W004", path, "rolling windows re-evaluate nodes", "warning", False)
                result.append(SourceSpec(entry.get("file_id", ""), entry.get("parent_node_id", ""),
                                         scope, source_time, tuple(tags), context))
            return tuple(result)

        selection, references = sources("selection"), sources("references")
        output = data.get("output")
        if not isinstance(output, dict):
            issue("FR-T102", "output", "output is required")
            output = {}
        for key in output.keys() - {"placement", "file_id", "parent_node_id", "format"}:
            issue("FR-O099", f"output.{key}", f"unknown key '{key}'")
        placement = output.get("placement", "under_each_selected_node" if unit == "per_node" else "under_parent")
        if placement not in ("under_parent", "under_each_selected_node"):
            issue("FR-O010", "output.placement", "invalid placement")
        if placement == "under_parent":
            if not output.get("file_id") or not output.get("parent_node_id"):
                issue("FR-O011", "output", "under_parent requires file_id and parent_node_id")
            for key in ("file_id", "parent_node_id"):
                if key in output:
                    check_id(output[key], f"output.{key}", True)
        elif placement == "under_each_selected_node" and ("file_id" in output or "parent_node_id" in output):
            issue("FR-O012", "output", "file_id and parent_node_id are not allowed")
        output_format = output.get("format")
        if output_format is not None and output_format != "career_note":
            issue("FR-O014", "output.format", "invalid output format")
        if output_format == "career_note" and not (
                domain == "career" and unit == "aggregate" and placement == "under_parent"
                and output.get("parent_node_id") == "root"):
            issue("FR-O014", "output.format", "career_note requires career aggregate under root")
        if output.get("parent_node_id") == "root" and output_format != "career_note":
            issue("FR-O014", "output.parent_node_id", "root output requires career_note")
        schedule = ScheduleSpec(kind or "", int(time[:2]), int(time[3:]), WEEKDAYS.index(weekday),
                                day, month, anchor, invalid_anchor, week_end)
        cfg.tasks.append(TaskSpec(index, task_id, purpose.strip(), domain, persona, approval, unit or "",
                                  schedule, zone, ranges[0], ranges[1], evidence, selection, references,
                                  OutputSpec(placement, output.get("file_id"), output.get("parent_node_id"),
                                             output_format),
                                  Path(base_dir), not any(i.severity == "error" and i.disables_task for i in issues),
                                  tuple(issues)))
    return cfg
