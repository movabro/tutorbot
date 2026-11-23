"""Injectable clock so tests and --date can control "now" and run_date."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

_fixed_now: datetime | None = None


def set_now(dt: datetime | None) -> None:
    """Freeze the clock (tests). Pass None to restore the real clock."""
    global _fixed_now
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    _fixed_now = dt


def now() -> datetime:
    return _fixed_now if _fixed_now is not None else datetime.now(timezone.utc)


def now_iso() -> str:
    return now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ms() -> int:
    return int(now().timestamp() * 1000)


def local_date(tz_name: str, when: datetime | None = None) -> date:
    return (when or now()).astimezone(ZoneInfo(tz_name)).date()


def ms_to_local_date(ms: int, tz_name: str) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz_name)).date()
