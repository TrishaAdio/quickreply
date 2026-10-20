"""Time helpers. UTC internally; Asia/Kolkata for every "day" boundary."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

from qr.config.settings import IST

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Aware UTC datetime (naive values are assumed to already be UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ist_date(moment: datetime) -> str:
    """The Asia/Kolkata calendar date (YYYY-MM-DD) containing ``moment``."""
    return as_utc(moment).astimezone(IST).date().isoformat()


def day_window(day: str | date) -> tuple[datetime, datetime]:
    """``[start, end)`` in UTC for one Asia/Kolkata calendar day.

    Start is 00:00:00 on ``day`` (inclusive); end is 00:00:00 on the next day
    (exclusive).
    """
    if isinstance(day, str):
        day = parse_day(day)
    start_local = datetime.combine(day, time(0, 0), tzinfo=IST)
    end_local = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=IST)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def in_day(moment: datetime, day: str | date) -> bool:
    start, end = day_window(day)
    return start <= as_utc(moment) < end


def parse_day(value: str) -> date:
    value = (value or "").strip()
    if not DATE_RE.match(value):
        raise ValueError("dates use the YYYY-MM-DD format")
    return date.fromisoformat(value)


def today_ist(now: datetime | None = None) -> str:
    return ist_date(now or utcnow())


def day_is_complete(day: str, now: datetime | None = None) -> bool:
    """True once the Asia/Kolkata day has fully ended."""
    _start, end = day_window(day)
    return as_utc(now or utcnow()) >= end


def ts_now() -> float:
    return utcnow().timestamp()


def ts_day_window(day: str | date) -> tuple[float, float]:
    start, end = day_window(day)
    return start.timestamp(), end.timestamp()
