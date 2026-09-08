"""Gradescope — assignments, due dates, submission status and scores, via pasted cookies.

UMD logs in through school SSO, so there is no password to automate. The user pastes
`signed_token` (remember-me, long-lived) and `_gradescope_session` as credential
gradescope:cookie. We read /account for the term's course list, then each course page's
student assignments table. Selectors are the ones the community scrapers use; every parse
is behind a fixture test because Gradescope's markup drifts.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from bs4 import BeautifulSoup

from ..core.models import Grade, Item
from ..core.timeutil import to_utc_iso
from .base import AuthError, SourceResult, classify_kind, parse_cookie_header, slug

_SCORE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")
_ASSIGN_ID = re.compile(r"/assignments/(\d+)")


def _base(src_cfg) -> str:
    return (src_cfg.get("base_url") or "https://www.gradescope.com").rstrip("/")


def _cookies(creds: dict) -> dict[str, str]:
    cookies = parse_cookie_header(creds.get("cookie"))
    if not cookies:
        raise AuthError("no Gradescope cookie pasted")
    if "signed_token" not in cookies and "_gradescope_session" not in cookies:
        raise AuthError("cookie must include signed_token and/or _gradescope_session")
    return cookies


def _logged_out(resp) -> bool:
    return resp.status_code in (401, 403) or "/login" in resp.url or 'name="session[email]"' in resp.text


def parse_account(html: str, term: str | None) -> list[dict]:
    """[{id, short, name, term}] for the course boxes on /account, filtered to `term` if given."""
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    terms = soup.select(".courseList--term")
    if terms:
        for t in terms:
            term_name = t.get_text(" ", strip=True)
            container = t.find_next_sibling()
            boxes = container.select("a.courseBox") if container else []
            for a in boxes:
                out.append(_box(a, term_name))
    else:
        for a in soup.select("a.courseBox"):
            out.append(_box(a, ""))
    if term:
        want = term.lower().replace(" ", "")
        filtered = [c for c in out if want in c["term"].lower().replace(" ", "")]
        if filtered or terms:
            out = filtered
    return [c for c in out if c["id"]]


def _box(a, term_name: str) -> dict:
    href = a.get("href", "")
    m = re.search(r"/courses/(\d+)", href)
    short = a.select_one(".courseBox--shortname")
    name = a.select_one(".courseBox--name")
    return {"id": m.group(1) if m else None, "short": short.get_text(" ", strip=True) if short else "",
            "name": name.get_text(" ", strip=True) if name else "", "term": term_name}


def _map_course(box: dict, courses: dict) -> str | None:
    hay = f"{box['short']} {box['name']}".lower().replace(" ", "")
    for code, c in courses.items():
        for tok in [code, *c.gradescope_short]:
            if tok.lower().replace(" ", "") in hay:
                return code
    return None


def _dt(el) -> str | None:
    if el is None:
        return None
    raw = (el.get("datetime") or el.get_text(" ", strip=True) or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return to_utc_iso(datetime.strptime(raw, fmt))
        except ValueError:
            continue
    try:
        return to_utc_iso(datetime.fromisoformat(raw))
    except ValueError:
        return None


def parse_course(html: str, course: str, course_id: str, base: str) -> tuple[list[Item], list[Grade]]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table#assignments-student-table") or soup.select_one("table.table")
    items: list[Item] = []
    grades: list[Grade] = []
    if table is None:
        return items, grades
    for tr in table.select("tbody tr"):
        first = tr.find(["th", "td"])
        if first is None:
            continue
        a = first.find("a")
        btn = first.find("button")
        title = (a.get_text(" ", strip=True) if a else first.get_text(" ", strip=True)).strip()
        if not title:
            continue
        href = a.get("href") if a else None
        aid = None
        if href:
            m = _ASSIGN_ID.search(href)
            aid = m.group(1) if m else None
        if not aid and btn is not None:
            aid = btn.get("data-assignment-id")
        if not aid:
            aid = slug(title)
        url = (base + href) if href and href.startswith("/") else (href or f"{base}/courses/{course_id}")
        release = _dt(tr.select_one(".submissionTimeChart--releaseDate"))
        dues = [d for d in (_dt(x) for x in tr.select(".submissionTimeChart--dueDate")) if d]
        due_at = dues[0] if dues else None
        late = dues[1] if len(dues) > 1 else None
        status_el = tr.select_one(".submissionStatus--text")
        status = status_el.get_text(" ", strip=True) if status_el else None
        score_el = tr.select_one(".submissionStatus--score")
        score_txt = score_el.get_text(" ", strip=True) if score_el else None
        if not score_txt:
            m = _SCORE.search(tr.get_text(" ", strip=True))
            score_txt = m.group(0) if m else None
        sid = f"{course_id}:{aid}"
        items.append(Item(course=course, kind=classify_kind(title), title=title, source="gradescope",
                          source_id=sid, due_at=due_at, all_day=False, release_at=release, late_due_at=late,
                          url=url, submission_status=status, score=score_txt,
                          raw={"course_id": course_id, "assignment_id": aid}))
        if score_txt:
            m = _SCORE.search(score_txt)
            if m:
                grades.append(Grade(course=course, title=title, source="gradescope", source_id=sid,
                                    score=float(m.group(1)), max_score=float(m.group(2))))
    return items, grades


def _account(cfg, src_cfg, creds, http):
    base = _base(src_cfg)
    cookies = _cookies(creds)
    resp = http.get(f"{base}/account", cookies=cookies, timeout=30)
    if _logged_out(resp):
        raise AuthError("Gradescope cookie rejected — log in again and re-paste signed_token + _gradescope_session")
    boxes = parse_account(resp.text, src_cfg.get("term"))
    mapped = [(b, _map_course(b, cfg.courses)) for b in boxes]
    return base, cookies, boxes, mapped


def fetch(cfg, src_cfg, creds, state, http) -> SourceResult:
    base, cookies, boxes, mapped = _account(cfg, src_cfg, creds, http)
    res = SourceResult(auth_ok=True)
    unmapped = [b["short"] or b["name"] for b, code in mapped if not code]
    if unmapped:
        res.notes.append("unmapped: " + ", ".join(unmapped))
    sleep_s = float(src_cfg.get("per_course_sleep_s", 1.0))
    for box, code in mapped:
        if not code:
            continue
        resp = http.get(f"{base}/courses/{box['id']}", cookies=cookies, timeout=30)
        if _logged_out(resp):
            raise AuthError("Gradescope session expired mid-run — re-paste cookies")
        items, grades = parse_course(resp.text, code, box["id"], base)
        if not items:
            res.errors.append(f"{code}: 0 rows parsed from course page — selectors drifted?")
        res.items.extend(items)
        res.grades.extend(grades)
        res.notes.append(f"{code}: {len(items)} assignments")
        time.sleep(sleep_s)
    if not any(code for _, code in mapped):
        res.notes.append("no Fall 2026 courses matched — roster gap or gradescope_short tokens?")
    return res


def probe(cfg, src_cfg, creds, http) -> tuple[bool, str]:
    try:
        _, _, boxes, mapped = _account(cfg, src_cfg, creds, http)
    except AuthError as e:
        return False, str(e)
    names = [f"{b['short'] or b['name']}→{code or '?'}" for b, code in mapped]
    return True, f"logged in; {len(boxes)} course(s) this term: " + (", ".join(names) or "none")
