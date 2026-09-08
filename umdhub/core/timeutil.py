"""Time helpers. Storage is always UTC ISO-8601; display and grouping are America/New_York.

The host clock is UTC. Every "what day is this due" question goes through
`local_date_str` / `today_local`, never through a UTC date — a Canvas 11:59 pm ET
deadline is 03:59 UTC the *next* day, which is the bug this module exists to prevent.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


# ---------------------------------------------------------------- now / parse / format
def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return utcnow().replace(microsecond=0).isoformat()


def to_utc_iso(dt: datetime) -> str:
    """Naive datetimes are taken as ET (the only naive source is human input)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO string (with or without tz) to an aware UTC datetime."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(UTC)


def local_dt(iso: str | None) -> datetime | None:
    dt = parse_iso(iso)
    return dt.astimezone(ET) if dt else None


def local_date_str(iso: str | None) -> str | None:
    dt = local_dt(iso)
    return dt.date().isoformat() if dt else None


def today_local() -> date:
    return datetime.now(ET).date()


def from_local(date_str: str, time_str: str | None = None) -> tuple[str, bool]:
    """(date, optional 'HH:MM') in ET -> (due_at UTC ISO, all_day).

    All-day items are stored at 23:59:59 ET so that `due_at < now` means overdue
    for timed and all-day items alike, and all-day items sort after timed ones.
    """
    d = date.fromisoformat(str(date_str))
    if time_str:
        hh, mm = _parse_hhmm(time_str)
        dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)
        return to_utc_iso(dt), False
    dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=ET)
    return to_utc_iso(dt), True


def _parse_hhmm(s: str) -> tuple[int, int]:
    s = s.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    clock = parse_clock(s)
    if clock:
        return clock
    raise ValueError(f"bad time {s!r}")


def fmt_et(iso: str | None, all_day: bool = False, with_date: bool = True) -> str:
    dt = local_dt(iso)
    if not dt:
        return "no date"
    day = dt.strftime("%a %b %-d")
    if all_day:
        return day if with_date else "all day"
    t = dt.strftime("%-I:%M %p")
    return f"{day}, {t}" if with_date else t


def fmt_time_et(iso: str | None, all_day: bool = False) -> str:
    return fmt_et(iso, all_day, with_date=False)


def days_until(date_str: str | None, today: date | None = None) -> int | None:
    if not date_str:
        return None
    today = today or today_local()
    return (date.fromisoformat(date_str) - today).days


def day_label(date_str: str | None, today: date | None = None) -> str:
    if not date_str:
        return "No date"
    today = today or today_local()
    d = date.fromisoformat(date_str)
    delta = (d - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta == -1:
        return "Yesterday"
    label = d.strftime("%a %b %-d")
    if delta < -1:
        return f"{label} ({-delta} d ago)"
    if delta < 7:
        return label
    return f"{label} (in {delta} d)"


# ---------------------------------------------------------------- loose date/time parsing
def parse_clock(s: str) -> tuple[int, int] | None:
    """'6:30pm', '11:59 PM', '10am', '6 pm' -> (hour24, minute). None if unparseable."""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*", s.strip(), re.I)
    if not m:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    if hh == 12:
        hh = 0
    if ap == "p":
        hh += 12
    return hh, mm


def parse_time_range(s: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """'6:30pm - 8:30pm' / '10:30 am–12:30 pm' -> ((18,30),(20,30))."""
    parts = re.split(r"\s*[-–—]\s*", s.strip())
    if len(parts) != 2:
        return None
    a, b = parse_clock(parts[0]), parse_clock(parts[1])
    if a and b:
        return a, b
    return None


def parse_month_day(s: str) -> tuple[int, int] | None:
    """'September25th', 'Sept 11', 'Dec. 15th', 'October 6th 6:30pm' -> (month, day)."""
    m = re.search(r"([A-Za-z]{3,9})\.?\s*(\d{1,2})(?:st|nd|rd|th)?\b", s)
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return None
    day = int(m.group(2))
    if not 1 <= day <= 31:
        return None
    return month, day


def infer_year(month: int, day: int, sem_start: date, sem_end: date) -> int:
    """Pick the year that lands the date inside the semester window (fall may span Dec→Jan)."""
    for y in (sem_start.year, sem_end.year):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if sem_start - timedelta(days=21) <= d <= sem_end + timedelta(days=21):
            return y
    return sem_start.year


def weekday_dates(weekday: str, start: date, end: date) -> list[date]:
    """Every <weekday> from start..end inclusive. weekday = MON..SUN."""
    wd = _WEEKDAYS[weekday.upper()[:3]]
    d = start + timedelta(days=(wd - start.weekday()) % 7)
    out = []
    while d <= end:
        out.append(d)
        d += timedelta(days=7)
    return out
