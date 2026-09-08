"""Seed source: the syllabus foundation layer, from seed/<semester>.yaml.

YAML shape:
  items:
    - {course: MATH246, kind: quiz, title: "Quiz 1", date: 2026-09-11, time: null,
       weight: "10% total across 6", notes: "...", url: null}
  recurring:
    - {course: CMSC330, kind: expected, title: "Lecture quiz (weekly)", weekday: MON,
       time: "23:59", from: 2026-09-07, until: 2026-12-07, weight: "2% total", notes: "..."}

Every generated item has a stable source_id, so re-runs upsert instead of duplicating.
Live sources overlay these rows via merge.py (seed has the lowest precedence for dates).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from ..core.models import KINDS, Item
from ..core.timeutil import from_local, weekday_dates
from .base import SourceResult, slug


def _date_str(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _mk(course: str, kind: str, title: str, d: str | None, t: str | None, *, weight=None, notes=None,
        url=None, sid_suffix: str | None = None) -> Item:
    if kind not in KINDS:
        raise ValueError(f"seed: bad kind {kind!r} for {title!r}")
    due_at, all_day = (None, False)
    if d:
        due_at, all_day = from_local(d, t)
    sid = f"{course}:{slug(title)}:{sid_suffix or d or 'undated'}"
    return Item(course=course, kind=kind, title=title, source="seed", source_id=sid,
                due_at=due_at, all_day=all_day, url=url, weight_note=weight, notes=notes)


def load_seed(path: Path, valid_courses: set[str] | None = None) -> list[Item]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    out: list[Item] = []
    for e in raw.get("items") or []:
        course = str(e["course"])
        if valid_courses and course not in valid_courses:
            raise ValueError(f"seed: unknown course {course!r}")
        out.append(_mk(course, e.get("kind", "assignment"), str(e["title"]), _date_str(e.get("date")),
                       e.get("time"), weight=e.get("weight"), notes=e.get("notes"), url=e.get("url")))
    for r in raw.get("recurring") or []:
        course = str(r["course"])
        if valid_courses and course not in valid_courses:
            raise ValueError(f"seed: unknown course {course!r}")
        start = date.fromisoformat(_date_str(r["from"]))
        end = date.fromisoformat(_date_str(r["until"]))
        skip = {_date_str(s) for s in (r.get("skip") or [])}
        for d in weekday_dates(str(r["weekday"]), start, end):
            ds = d.isoformat()
            if ds in skip:
                continue
            out.append(_mk(course, r.get("kind", "expected"), str(r["title"]), ds, r.get("time"),
                           weight=r.get("weight"), notes=r.get("notes"), url=r.get("url")))
    return out


def fetch(cfg, src_cfg, creds, state, http) -> SourceResult:
    res = SourceResult()
    path = cfg.seed_path()
    if not path.exists():
        res.errors.append(f"seed file missing: {path}")
        return res
    res.items = load_seed(path, set(cfg.course_codes()))
    res.notes.append(f"{len(res.items)} seed items")
    return res
