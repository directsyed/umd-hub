"""Piazza — new/updated posts per class become feed items; deadline changes get extracted.

Auth, in order: a session persisted from a previous run (meta piazza:session), a pasted
cookie (credential piazza:cookie), then PIAZZA_EMAIL / PIAZZA_PASSWORD from secrets.env.
Piazza throttles repeated logins, so the working session is always saved back.
Uses the `piazza-api` package (hfaran) — unofficial, so every call is wrapped.
"""
from __future__ import annotations

import json
import logging
import time

from bs4 import BeautifulSoup

from ..core.config import env
from ..core.models import FeedItem
from ..core.timeutil import parse_iso, to_utc_iso
from .base import AuthError, SourceResult, guess_course, parse_cookie_header

log = logging.getLogger(__name__)
_INSTRUCTOR_ROLES = {"instructor", "professor", "ta"}


def _client(creds: dict, state: dict):
    from piazza_api import Piazza
    from piazza_api.rpc import PiazzaRPC

    p = Piazza()
    tried: list[str] = []
    saved = state.get("session")
    attempts = [("saved session", json.loads(saved) if saved else None),
                ("pasted cookie", parse_cookie_header(creds.get("cookie")) or None)]
    for label, cookies in attempts:
        if not cookies:
            continue
        rpc = PiazzaRPC()
        rpc.set_cookies(cookies)
        p._rpc_api = rpc
        try:
            p.get_user_status()
            return p
        except Exception as e:  # noqa: BLE001
            tried.append(f"{label}: {type(e).__name__}")
            p._rpc_api = None
    email, pw = env("PIAZZA_EMAIL"), env("PIAZZA_PASSWORD")
    if email and pw:
        try:
            p.user_login(email=email, password=pw)
            return p
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"Piazza login failed: {e}") from e
    hint = " (" + "; ".join(tried) + ")" if tried else ""
    raise AuthError("no working Piazza session — set PIAZZA_EMAIL/PIAZZA_PASSWORD in secrets.env "
                    "or paste a cookie" + hint)


def _classes(p, cfg, term: str) -> list[tuple[str, str, dict]]:
    """[(nid, course_code, raw)] for this term's classes that map to a configured course."""
    out = []
    for c in p.get_user_classes() or []:
        t = str(c.get("term") or "")
        if term and t and term.lower().replace(" ", "") not in t.lower().replace(" ", ""):
            continue
        hay = " ".join(str(c.get(k) or "") for k in ("num", "name", "nid"))
        code = guess_course(hay, cfg.courses)
        if not code:
            for k, cc in cfg.courses.items():
                if any(n.lower().replace(" ", "") in hay.lower().replace(" ", "") for n in cc.piazza_names):
                    code = k
                    break
        if code and c.get("nid"):
            out.append((str(c["nid"]), code, c))
    return out


def _html_to_text(html: str | None) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    return soup.get_text("\n").strip()


def _iso(s) -> str | None:
    dt = parse_iso(str(s)) if s else None
    return to_utc_iso(dt) if dt else None


def fetch(cfg, src_cfg, creds, state, http) -> SourceResult:
    p = _client(creds, state)
    res = SourceResult(auth_ok=True)
    term = str(src_cfg.get("term") or cfg.semester.name)
    max_posts = int(src_cfg.get("max_posts_per_class", 30))
    classes = _classes(p, cfg, term)
    if not classes:
        res.notes.append("no Piazza classes matched configured courses")
    for nid, code, raw in classes:
        try:
            net = p.network(nid)
            instructors: set[str] = set()
            try:
                for u in net.get_all_users() or []:
                    if str(u.get("role", "")).lower() in _INSTRUCTOR_ROLES:
                        instructors.add(str(u.get("id")))
            except Exception:  # noqa: BLE001
                pass
            last_ts = state.get(f"{nid}:last_ts") or ""
            feed = net.get_feed(limit=max_posts, offset=0) or {}
            newest = last_ts
            n_new = 0
            for post in feed.get("feed", []):
                cid = post.get("id")
                modified = str(post.get("modified") or post.get("updated") or "")
                if not cid or (modified and modified <= last_ts):
                    continue
                full = net.get_post(cid) or {}
                hist = (full.get("history") or [{}])[0]
                subject = (hist.get("subject") or post.get("subject") or "(no subject)").strip()
                body = _html_to_text(hist.get("content")) or post.get("content_snipet") or ""
                tags = set(full.get("tags") or post.get("tags") or [])
                uid = str(hist.get("uid") or "")
                is_instr = "instructor-note" in tags or uid in instructors
                nr = full.get("nr") or post.get("nr")
                url = f"https://piazza.com/class/{nid}/post/{nr}" if nr else f"https://piazza.com/class/{nid}"
                res.feed_items.append(FeedItem(
                    source="piazza", source_id=f"{nid}:{cid}", course=code, subject=subject, body=body,
                    author=("instructor" if is_instr else "student"), is_instructor=is_instr, url=url,
                    posted_at=_iso(hist.get("created") or full.get("created"))))
                n_new += 1
                if modified > newest:
                    newest = modified
                time.sleep(0.5)
            if newest:
                res.state_updates[f"{nid}:last_ts"] = newest
            res.notes.append(f"{code}: {n_new} new/updated posts")
        except Exception as e:  # noqa: BLE001
            res.errors.append(f"{code} ({nid}): {e}")
    try:
        res.state_updates["session"] = json.dumps(p._rpc_api.get_cookies())
    except Exception:  # noqa: BLE001
        pass
    return res


def probe(cfg, src_cfg, creds, http) -> tuple[bool, str]:
    try:
        p = _client(creds, {})
    except AuthError as e:
        return False, str(e)
    try:
        classes = _classes(p, cfg, str(src_cfg.get("term") or cfg.semester.name))
    except Exception as e:  # noqa: BLE001
        return False, f"logged in but listing classes failed: {e}"
    names = ", ".join(f"{raw.get('num') or raw.get('name')}→{code}" for _, code, raw in classes)
    return True, f"logged in; matched classes: {names or 'none'}"
