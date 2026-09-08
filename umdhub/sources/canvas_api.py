"""Canvas REST API via a pasted session cookie — OPTIONAL (disabled by default).

UMD blocks student API tokens, but GET requests to /api/v1 accept the browser session
cookie. This adds submission status, points and announcements on top of the .ics feed.
Cookie: canvas_api:cookie = "canvas_session=…; _legacy_normandy_session=…". Session cookies
expire in days; the .ics feed is the durable source, this is the enhancement.
"""
from __future__ import annotations

import json
import re

from ..core.models import FeedItem, Item
from ..core.timeutil import to_utc_iso
from .base import AuthError, SourceResult, classify_kind, guess_course, parse_cookie_header

_WHILE1 = re.compile(r"^\s*while\s*\(1\);\s*")
_TAG = re.compile(r"<[^>]+>")


def _base(src_cfg) -> str:
    return (src_cfg.get("base_url") or "https://umd.instructure.com").rstrip("/")


def _get(http, base: str, path: str, cookies: dict, **params):
    resp = http.get(f"{base}{path}", cookies=cookies, params=params, timeout=30,
                    headers={"Accept": "application/json"}, allow_redirects=False)
    if resp.status_code in (301, 302, 401, 403):
        raise AuthError("Canvas session cookie rejected — re-paste canvas_session")
    resp.raise_for_status()
    return json.loads(_WHILE1.sub("", resp.text))


def _iso(s: str | None) -> str | None:
    if not s:
        return None
    from datetime import datetime
    try:
        return to_utc_iso(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        return None


def fetch(cfg, src_cfg, creds, state, http) -> SourceResult:
    cookies = parse_cookie_header(creds.get("cookie"))
    if not cookies:
        raise AuthError("no Canvas session cookie pasted")
    base = _base(src_cfg)
    res = SourceResult(auth_ok=True)
    courses = _get(http, base, "/api/v1/courses", cookies, enrollment_state="active", per_page=50)
    mapped = []
    for c in courses:
        code = guess_course(f"{c.get('course_code', '')} {c.get('name', '')}", cfg.courses)
        if code:
            mapped.append((c, code))
    for c, code in mapped:
        cid = c["id"]
        for a in _get(http, base, f"/api/v1/courses/{cid}/assignments", cookies, per_page=100,
                      order_by="due_at", **{"include[]": "submission"}):
            sub = a.get("submission") or {}
            score = sub.get("score")
            pts = a.get("points_possible")
            res.items.append(Item(
                course=code, kind=classify_kind(a.get("name", "")), title=a.get("name", "").strip(),
                source="canvas_api", source_id=f"assignment-{a['id']}", due_at=_iso(a.get("due_at")),
                url=a.get("html_url"), points=score, max_points=pts,
                submission_status=sub.get("workflow_state"),
                score=(f"{score} / {pts}" if score is not None and pts is not None else None),
                raw={"canvas_course_id": cid}))
        for ann in _get(http, base, "/api/v1/announcements", cookies, per_page=20,
                        **{"context_codes[]": f"course_{cid}"}):
            body = _TAG.sub(" ", ann.get("message") or "")
            res.feed_items.append(FeedItem(
                source="canvas_announce", source_id=f"announcement-{ann['id']}", course=code,
                subject=ann.get("title", "").strip(), body=body.strip(), url=ann.get("html_url"),
                author=(ann.get("author") or {}).get("display_name"), is_instructor=True,
                posted_at=_iso(ann.get("posted_at"))))
        res.notes.append(f"{code}: ok")
    return res


def probe(cfg, src_cfg, creds, http) -> tuple[bool, str]:
    cookies = parse_cookie_header(creds.get("cookie"))
    if not cookies:
        return False, "no cookie pasted"
    try:
        me = _get(http, _base(src_cfg), "/api/v1/users/self", cookies)
    except AuthError as e:
        return False, str(e)
    return True, f"logged in as {me.get('name') or me.get('id')}"
