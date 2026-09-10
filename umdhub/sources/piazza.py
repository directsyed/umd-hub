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

from ..core.config import env
from ..core.models import FeedItem
from ..core.timeutil import parse_iso, to_utc_iso
from .base import AuthError, SourceResult, guess_course, html_to_text, parse_cookie_header

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
    return html_to_text(html)


def _latest(node: dict) -> tuple[str, str | None, str]:
    """(text, created, uid) from a node carrying a versioned history[] (post, i_answer, s_answer)."""
    hist = node.get("history") or []
    h = hist[0] if hist else {}
    return _html_to_text(h.get("content")), h.get("created"), str(h.get("uid") or "")


def render_post(full: dict, feed_entry: dict, instructors: set[str]) -> tuple[str, str, bool, str | None]:
    """Flatten a Piazza thread into (subject, body, any_instructor, created).

    The question/note is history[0]; answers live in children[] as type i_answer / s_answer
    (versioned, text in history), and discussion lives in children[] as type followup with
    nested feedback replies (text in `subject`). Staff answer *inside* the student's post, so a
    body built from history[0] alone drops exactly the part that carries deadline changes.
    """
    hist = (full.get("history") or [{}])
    head = hist[0] if hist else {}
    subject = (head.get("subject") or feed_entry.get("subject") or "(no subject)").strip()
    text, created, uid = _latest(full)
    tags = set(full.get("tags") or feed_entry.get("tags") or [])
    any_instr = "instructor-note" in tags or (uid in instructors)
    parts = [text or _html_to_text(feed_entry.get("content_snipet")) or ""]
    for child in full.get("children") or []:
        ctype = child.get("type")
        if ctype in ("i_answer", "s_answer"):
            t, _, u = _latest(child)
            if not t.strip():
                continue
            if ctype == "i_answer" or u in instructors:
                any_instr = True
            parts.append(f"--- {'Instructor' if ctype == 'i_answer' else 'Student'} answer ---\n{t}")
        elif ctype == "followup":
            t = _html_to_text(child.get("subject"))
            u = str(child.get("uid") or "")
            who = "instructor" if u in instructors else "student"
            any_instr = any_instr or who == "instructor"
            if t.strip():
                parts.append(f"--- Follow-up ({who}) ---\n{t}")
            for fb in child.get("children") or []:
                t2 = _html_to_text(fb.get("subject"))
                u2 = str(fb.get("uid") or "")
                who2 = "instructor" if u2 in instructors else "student"
                any_instr = any_instr or who2 == "instructor"
                if t2.strip():
                    parts.append(f"--- Reply ({who2}) ---\n{t2}")
    body = "\n\n".join(p for p in parts if p and p.strip())
    return subject, body, any_instr, created


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
                subject, body, is_instr, created = render_post(full, post, instructors)
                nr = full.get("nr") or post.get("nr")
                url = f"https://piazza.com/class/{nid}/post/{nr}" if nr else f"https://piazza.com/class/{nid}"
                res.feed_items.append(FeedItem(
                    source="piazza", source_id=f"{nid}:{cid}", course=code, subject=subject, body=body,
                    author=("instructor" if is_instr else "student"), is_instructor=is_instr, url=url,
                    posted_at=_iso(created or full.get("created"))))
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
