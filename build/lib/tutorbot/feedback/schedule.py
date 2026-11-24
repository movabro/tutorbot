"""Calendar scheduling and first-run windows for feedback rules."""
from __future__ import annotations

import calendar
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import ScheduleDecision, TaskSpec

UTC = timezone.utc


def add_months_clamped(local_dt: datetime, months: int) -> datetime:
    month_index = local_dt.year * 12 + local_dt.month - 1 + months
    year, month = divmod(month_index, 12)
    month += 1
    day = min(local_dt.day, calendar.monthrange(year, month)[1])
    return local_dt.replace(year=year, month=month, day=day, fold=0)


def cycle_key_for(task: TaskSpec, T: datetime, run_id: int | None = None) -> str:
    local = T.astimezone(ZoneInfo(task.timezone))
    kind = task.schedule.kind
    if kind == "each_run":
        return "each_run:" + (str(run_id) if run_id is not None else T.astimezone(UTC).isoformat())
    if kind == "daily":
        return local.strftime("%Y-%m-%d")
    if kind == "work_slots":
        date, hour = _work_slot(local)
        return f"{date.isoformat()}@{hour:02d}:00"
    if kind == "weekly":
        if task.schedule.week_end:
            local -= timedelta(days=7)
        year, week, _ = local.isocalendar()
        return f"{year:04d}-W{week:02d}"
    if kind == "monthly":
        return local.strftime("%Y-%m")
    if kind == "quarterly":
        return f"{local.year:04d}-Q{(local.month - 1) // 3 + 1}"
    if kind == "yearly":
        return f"{local.year:04d}"
    raise ValueError(f"unknown schedule: {kind}")


def scheduled_at_for(task: TaskSpec, T: datetime) -> datetime | None:
    tz = ZoneInfo(task.timezone)
    local = T.astimezone(tz)
    spec = task.schedule
    date = local.date()
    if spec.kind == "each_run":
        return None
    if spec.kind == "work_slots":
        date, hour = _work_slot(local)
        return datetime(date.year, date.month, date.day, hour, tzinfo=tz).astimezone(UTC)
    if spec.kind == "weekly" and spec.week_end:
        date -= timedelta(days=date.weekday())
        return datetime(date.year, date.month, date.day, tzinfo=tz).astimezone(UTC)
    if spec.kind == "weekly":
        date += timedelta(days=spec.weekday - date.weekday())
    elif spec.kind == "monthly":
        last = calendar.monthrange(date.year, date.month)[1]
        date = date.replace(day=min(spec.day or last, last))
    elif spec.kind == "quarterly":
        first_month = ((date.month - 1) // 3) * 3 + 1
        month = first_month if spec.quarter_anchor == "start" else first_month + 2
        day = 1 if spec.quarter_anchor == "start" else calendar.monthrange(date.year, month)[1]
        date = date.replace(month=month, day=day)
    elif spec.kind == "yearly":
        try:
            date = date.replace(month=spec.month, day=spec.day if spec.day is not None else 31)
        except ValueError:
            return None
    elif spec.kind != "daily":
        raise ValueError(f"unknown schedule: {spec.kind}")
    return datetime(date.year, date.month, date.day, spec.hour, spec.minute, tzinfo=tz, fold=0).astimezone(UTC)


def decide(task: TaskSpec, T: datetime, has_cycle: Callable[[str], bool],
           *, run_id: int | None = None) -> ScheduleDecision:
    key = cycle_key_for(task, T, run_id)
    if task.schedule.kind == "each_run":
        return ScheduleDecision("run", key, None, None)
    if has_cycle(key):
        return ScheduleDecision("already_done", key, None, None)
    if task.schedule.kind == "quarterly" and task.schedule.quarter_anchor_invalid is not None:
        return ScheduleDecision("skip", key, None, "invalid_schedule_quarter_anchor")
    scheduled = scheduled_at_for(task, T)
    if scheduled is None:
        return ScheduleDecision("skip", key, None, "yearly_date_not_exist")
    return ScheduleDecision("run" if T >= scheduled else "not_due", key, scheduled, None)


def first_run_lower_bound(task: TaskSpec, T: datetime) -> datetime:
    kind = task.schedule.kind
    if kind == "work_slots":
        return T.astimezone(UTC) - timedelta(hours=3)
    if kind in ("each_run", "daily"):
        return T.astimezone(UTC) - timedelta(days=1)
    if kind == "weekly":
        return T.astimezone(UTC) - timedelta(days=7)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[kind]
    return add_months_clamped(T.astimezone(ZoneInfo(task.timezone)), -months).astimezone(UTC)


def next_due(task: TaskSpec, T: datetime, has_cycle: Callable[[str], bool]) -> datetime | None:
    if task.schedule.kind == "each_run":
        return T.astimezone(UTC)
    if task.schedule.quarter_anchor_invalid is not None:
        return None
    local = T.astimezone(ZoneInfo(task.timezone))
    if not has_cycle(cycle_key_for(task, T)):
        return scheduled_at_for(task, T)
    kind = task.schedule.kind
    if kind == "work_slots":
        date, hour = _work_slot(local)
        slots = (8, 11, 14, 17, 20)
        if hour == 20:
            date += timedelta(days=1)
            hour = 8
        else:
            hour = slots[slots.index(hour) + 1]
        return datetime(date.year, date.month, date.day, hour,
                        tzinfo=ZoneInfo(task.timezone)).astimezone(UTC)
    if kind == "daily":
        future = local + timedelta(days=1)
    elif kind == "weekly":
        future = local + timedelta(days=7)
    else:
        future = add_months_clamped(local, {"monthly": 1, "quarterly": 3, "yearly": 12}[kind])
    return scheduled_at_for(task, future)


def _work_slot(local: datetime):
    hours = (8, 11, 14, 17, 20)
    candidates = [hour for hour in hours if hour <= local.hour]
    return (local.date(), candidates[-1]) if candidates else (local.date() - timedelta(days=1), 20)
