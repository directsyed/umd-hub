"""Canvas/ELMS calendar feed — token-free structured due dates for every enrolled course.

The user pastes the private feed URL (ELMS → Calendar → "Calendar Feed") as credential
canvas_ics:ics_url. The feed lists assignments (UID event-assignment-<id>) and calendar
events (UID event-calendar-event-<id>) with SUMMARY "Title [Course context]". Undated and
To-Do items are not in the feed; announcements/grades come via the email source instead.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

from icalendar import Calendar

from ..core.models import Item
from ..core.timeutil import from_local, to_utc_iso
from .base import AuthError, SourceResult, classify_kind, guess_course

_SUMMARY = re.compile(r"^(?P<title>.*?)\s*\[(?P<ctx>[^\]]+)\]\s*$", re.S)
_ASSIGN = re.compile(r"event-assignment(?:-override)?-(\d+)")
_EVENT = re.compile(r"event-calendar-event-(\d+)")


def _due(ev) -> tuple[str | None, bool]:
    dtstart = ev.get("DTSTART")
    if dtstart is None:
        return None, False
    dt = dtstart.dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # Canvas emits Z times
        return to_utc_iso(dt), False
    if isinstance(dt, date):
        return from_local(dt.isoformat(), None)
    return None, False


def parse_ics(text: str, courses: dict) -> list[Item]:
    cal = Calendar.from_ical(text)
    out: list[Item] = []
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID", "")).strip()
        summary = str(ev.get("SUMMARY", "")).strip()
        if not summary:
            continue
        m = _SUMMARY.match(summary)
        title, ctx = (m.group("title").strip(), m.group("ctx").strip()) if m else (summary, "")
        course = guess_course(ctx, courses) or guess_course(summary, courses)
        if not course:
            continue
        due_at, all_day = _due(ev)
        if not due_at:
            continue
        url = str(ev.get("URL", "")).strip() or None
        desc = str(ev.get("DESCRIPTION", "")).strip() or None
        if _ASSIGN.search(uid):
            kind = classify_kind(title, "assignment")
        else:
            kind = classify_kind(title, "event")
        out.append(Item(course=course, kind=kind, title=title, source="canvas_ics", source_id=uid or summary,
                        due_at=due_at, all_day=all_day, url=url,
                        notes=(desc[:500] if desc and desc != title else None),
                        raw={"summary": summary, "ctx": ctx, "uid": uid}))
    return out


def _get_feed(creds: dict, http):
    url = (creds.get("ics_url") or "").strip()
    if not url:
        raise AuthError("no Canvas calendar feed URL pasted")
    resp = http.get(url, timeout=30)
    if resp.status_code in (401, 403, 404):
        raise AuthError(f"feed returned HTTP {resp.status_code} — regenerate the Calendar Feed link in ELMS")
    resp.raise_for_status()
    if b"BEGIN:VCALENDAR" not in resp.content[:300]:
        raise AuthError("feed did not return a calendar (login page?) — check the URL")
    return resp


def fetch(cfg, src_cfg, creds, state, http) -> SourceResult:
    resp = _get_feed(creds, http)
    res = SourceResult(auth_ok=True)
    res.items = parse_ics(resp.text, cfg.courses)
    res.notes.append(f"{len(res.items)} course events")
    if not res.items:
        res.notes.append("feed parsed but matched no configured course — check courses.*.canvas_names")
    return res


def probe(cfg, src_cfg, creds, http) -> tuple[bool, str]:
    try:
        resp = _get_feed(creds, http)
    except AuthError as e:
        return False, str(e)
    items = parse_ics(resp.text, cfg.courses)
    per = {}
    for it in items:
        per[it.course] = per.get(it.course, 0) + 1
    return True, f"{len(items)} events: " + ", ".join(f"{k} {v}" for k, v in sorted(per.items()))
