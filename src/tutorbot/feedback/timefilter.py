"""Time-window filtering and bot-write modified timestamp correction."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import BotWriteMark, TimeBounds


def floor_to_second(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def own_text_hash(node) -> str:
    content = unicodedata.normalize("NFC", node.content or "")
    note = unicodedata.normalize("NFC", node.note or "")
    encoded = json.dumps([content, note], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "v2:" + hashlib.sha256(encoded).hexdigest()


def effective_modified_ms(node, mark: BotWriteMark | None) -> int:
    modified = node.modified or 0
    # v1 concatenated fields ambiguously. Never trust it for suppression:
    # the next output snapshot naturally replaces it with a v2 mark.
    if mark is not None and mark.own_hash == own_text_hash(node):
        return min(modified, mark.effective_modified_ms)
    return modified


def resolve_time_bounds(kind: str, T: datetime, tz: str | ZoneInfo,
                        prev_started_at: datetime | str | None,
                        task_schedule) -> TimeBounds | None:
    T = floor_to_second(T)
    if kind == "fixed_context":
        return None
    field, separator, window = kind.partition("_")
    if not separator or field not in ("created", "modified"):
        raise ValueError(f"unknown time filter: {kind}")
    if window == "today":
        zone = ZoneInfo(tz) if isinstance(tz, str) else tz
        local = T.astimezone(zone)
        lower = local.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    elif window in ("last_24h", "last_7d"):
        lower = T - timedelta(days=1 if window == "last_24h" else 7)
    elif window == "since_last_run":
        if isinstance(prev_started_at, str):
            lower = datetime.fromisoformat(prev_started_at.replace("Z", "+00:00"))
        elif prev_started_at is not None:
            lower = prev_started_at
        else:
            from .schedule import first_run_lower_bound
            lower = first_run_lower_bound(task_schedule, T)
    else:
        raise ValueError(f"unknown time filter: {kind}")
    return TimeBounds(kind, field, int(lower.timestamp() * 1000), int(T.timestamp() * 1000))


def matches_time(node, bounds: TimeBounds | None, mark: BotWriteMark | None = None) -> bool:
    if bounds is None:
        return True
    value = effective_modified_ms(node, mark) if bounds.field == "modified" else (node.created or 0)
    return value > 0 and bounds.lower_ms <= value < bounds.upper_ms
