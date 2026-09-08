"""Course websites — structured dates where a parser exists (CMSC330 home page), plus a
visible-text diff of every configured page so any change surfaces in the feed (and goes to
the extractor). Snapshots live in meta as course_site:snap:<course>:<page>.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..core.models import FeedItem, Item
from ..core.timeutil import from_local, infer_year, parse_clock, parse_month_day, parse_time_range
from .base import SourceResult, classify_kind, slug

_MONTH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s*\d{1,2}", re.I)


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _date_from(text: str, sem_start: date, sem_end: date) -> tuple[str, str | None] | None:
    md = parse_month_day(text)
    if not md:
        return None
    month, day = md
    year = infer_year(month, day, sem_start, sem_end)
    try:
        d = date(year, month, day).isoformat()
    except ValueError:
        return None
    rng = parse_time_range(text)
    if rng:
        return d, f"{rng[0][0]:02d}:{rng[0][1]:02d}"
    tail = text[text.lower().find(str(day)) + len(str(day)):]
    m = re.search(r"\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?", tail, re.I)
    if m:
        clock = parse_clock(m.group(0))
        if clock:
            return d, f"{clock[0]:02d}:{clock[1]:02d}"
    return d, None


def parse_dated_tables(html: str, course: str, sem_start: date, sem_end: date) -> list[Item]:
    """Every table row whose cells contain a title and a Month-Day becomes an item.

    Handles the CMSC330 syllabus/home tables ('Quiz 2 | 2.00% | September25th',
    'Final Exam | 22.00% | December 15th 6:30pm - 8:30pm') without caring about column order.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[Item] = []
    seen: set[str] = set()
    for table in soup.find_all("table"):
        # One page table may carry several header rows (Quizzes / Exams / Projects…); a row's
        # meaning comes from the nearest header row above it, not from the table as a whole.
        table_is_release = False
        for tr in table.find_all("tr"):
            ths, tds = tr.find_all("th"), tr.find_all("td")
            if ths and not tds:
                table_is_release = any("release" in th.get_text(" ", strip=True).lower() for th in ths)
                continue
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            date_cell = next((c for c in cells if _MONTH_RE.search(c)), None)
            if not date_cell:
                continue
            title = cells[0] if cells[0] != date_cell else next((c for c in cells[1:] if c != date_cell), None)
            if not title or _MONTH_RE.search(title):
                continue
            parsed = _date_from(date_cell, sem_start, sem_end)
            if not parsed:
                continue
            d, t = parsed
            weight = next((c for c in cells if re.search(r"\d+(\.\d+)?\s*%", c)), None)
            note_bits = [c for c in cells if c not in (title, date_cell, weight)]
            extra = re.sub(r".*?\d{1,2}(st|nd|rd|th)?", "", date_cell, count=1).strip(" ()")
            if extra and not parse_time_range(extra) and not parse_clock(extra):
                note_bits.append(extra)
            if table_is_release or re.search(r"release", date_cell + " " + " ".join(note_bits), re.I):
                kind = "event"
                title_out = f"{title} released"
            else:
                kind = classify_kind(title, "event")
                title_out = title
            sid = f"{course}:{slug(title_out)}:{d}"
            if sid in seen:
                continue
            seen.add(sid)
            due_at, all_day = from_local(d, t)
            out.append(Item(course=course, kind=kind, title=title_out, source="course_site", source_id=sid,
                            due_at=due_at, all_day=all_day, weight_note=weight,
                            notes=("; ".join(note_bits) or None), raw={"row": cells}))
    return out


def _diff(prev: str, cur: str) -> str:
    lines = list(difflib.unified_diff(prev.splitlines(), cur.splitlines(), lineterm="", n=1))
    body = [ln for ln in lines[2:] if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]
    return "\n".join(body)[:20000]


def fetch(cfg, src_cfg, creds, state, http) -> SourceResult:
    res = SourceResult()
    sem_start = date.fromisoformat(cfg.semester.start)
    sem_end = date.fromisoformat(cfg.semester.end)
    sites = src_cfg.get("sites") or {}
    for course, site in sites.items():
        if not site.get("enabled", True) or not site.get("url"):
            continue
        base = site["url"]
        parser = site.get("parser", "generic")
        for page in site.get("pages") or [""]:
            url = urljoin(base, page)
            try:
                resp = http.get(url, timeout=30)
            except Exception as e:  # noqa: BLE001
                res.errors.append(f"{course} {url}: {e}")
                continue
            if resp.status_code != 200:
                res.errors.append(f"{course} {url}: HTTP {resp.status_code}")
                continue
            html = resp.text
            if parser == "cmsc330_home" and page == "":
                items = parse_dated_tables(html, course, sem_start, sem_end)
                res.items.extend(items)
                res.notes.append(f"{course}: {len(items)} dated rows")
            text = visible_text(html)
            h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            key = f"snap:{course}:{page or 'home'}"
            prev = json.loads(state.get(key) or "null")
            if prev and prev.get("hash") != h:
                diff = _diff(prev.get("text", ""), text)
                if diff.strip():
                    res.feed_items.append(FeedItem(
                        source="site_diff", source_id=f"{course}:{page or 'home'}:{h}", course=course,
                        subject=f"{course} site changed: {url}", body=diff, url=url, is_instructor=True))
            res.state_updates[key] = json.dumps({"hash": h, "text": text[:60000]})
    return res
