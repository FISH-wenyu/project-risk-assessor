from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_BEIJING_FALLBACK = timezone(timedelta(hours=8))


def beijing_now_text(now_utc: datetime | None = None) -> str:
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        beijing_timezone = ZoneInfo("Asia/Shanghai")
    except (ZoneInfoNotFoundError, OSError):
        beijing_timezone = _BEIJING_FALLBACK
    return current.astimezone(beijing_timezone).strftime("%Y-%m-%d %H:%M:%S")
