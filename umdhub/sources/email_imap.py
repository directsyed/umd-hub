"""Course email over IMAP (Gmail / Google Workspace) with an App Password.

Credentials come from secrets.env (EMAIL_USER, EMAIL_APP_PASSWORD, EMAIL_IMAP_HOST) — never
from the credential panel. First run: Gmail X-GM-RAW search over the lookback window for
course senders; afterwards incremental by UID. Every matching message becomes a feed item;
the extractor turns the ones with deadlines into candidates. `probe email` tells apart
"app password rejected", "IMAP disabled by admin", and OK.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from email import policy
from email.utils import parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup

from ..core.config import env
from ..core.models import FeedItem
from ..core.timeutil import to_utc_iso
from .base import AuthError, SourceResult, guess_course

log = logging.getLogger(__name__)
imaplib._MAXLINE = 20_000_000  # large mailboxes overflow the stdlib default


def _creds() -> tuple[str, str, str]:
    user, pw = env("EMAIL_USER"), env("EMAIL_APP_PASSWORD")
    if not user or not pw:
        raise AuthError("EMAIL_USER / EMAIL_APP_PASSWORD not set in secrets.env")
    return user, pw, env("EMAIL_IMAP_HOST") or "imap.gmail.com"


def _classify_login_error(msg: str) -> str:
    m = msg.lower()
    if "application-specific password" in m or "app password" in m:
        return "Google wants an App Password (2-Step Verification must be on)"
    if "imap access is disabled" in m or "imap is disabled" in m:
        return "IMAP is disabled for this account/domain by the admin — use the forward-to-personal-Gmail route"
    if "authenticationfailed" in m or "invalid credentials" in m:
        return "login rejected (wrong app password, or App Passwords blocked by the Workspace admin)"
    return msg


def _connect() -> imaplib.IMAP4_SSL:
    user, pw, host = _creds()
    try:
        m = imaplib.IMAP4_SSL(host, 993, timeout=30)
        m.login(user, pw)
    except imaplib.IMAP4.error as e:
        raise AuthError(f"IMAP: {_classify_login_error(str(e))}") from e
    except OSError as e:
        raise AuthError(f"IMAP connection failed: {e}") from e
    return m


def _body_text(msg) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001
        part = None
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:  # noqa: BLE001
        return ""
    if part.get_content_type() == "text/html":
        soup = BeautifulSoup(content, "lxml")
        for t in soup(["script", "style"]):
            t.decompose()
        content = soup.get_text("\n")
    lines = [ln.rstrip() for ln in content.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def course_for_message(from_addr: str, subject: str, body: str, courses: dict) -> str | None:
    fa = (from_addr or "").lower()
    for code, c in courses.items():
        if any(s.lower() == fa for s in c.email_senders):
            return code
    return guess_course(subject, courses) or guess_course(body[:1500], courses)


def parse_message(raw: bytes, courses: dict, uid: str, sender_domains: list[str]) -> FeedItem | None:
    msg = email.message_from_bytes(raw, policy=policy.default)
    name, addr = parseaddr(msg.get("From", ""))
    subject = (msg.get("Subject") or "(no subject)").strip()
    body = _body_text(msg)
    course = course_for_message(addr, subject, body, courses)
    domain_ok = any(addr.lower().endswith("@" + d) or addr.lower().endswith("." + d) for d in sender_domains)
    if not course and not domain_ok:
        return None
    posted = None
    try:
        posted = to_utc_iso(parsedate_to_datetime(msg.get("Date")))
    except Exception:  # noqa: BLE001
        pass
    instr = bool(course) and any(addr.lower() == s.lower() for s in courses[course].email_senders) if course else False
    return FeedItem(source="email", source_id=(msg.get("Message-ID") or f"uid:{uid}").strip(),
                    subject=subject, body=body, course=course,
                    author=f"{name} <{addr}>" if name else addr, is_instructor=instr, posted_at=posted)


def _search(m: imaplib.IMAP4_SSL, host: str, domains: list[str], lookback_days: int,
            last_uid: str | None) -> list[bytes]:
    if last_uid:
        typ, data = m.uid("SEARCH", None, f"UID {int(last_uid) + 1}:*")
    elif "gmail" in host or "google" in host:
        froms = " OR ".join(f"from:{d}" for d in domains)
        raw = f'"newer_than:{lookback_days}d ({froms})"'
        typ, data = m.uid("SEARCH", "X-GM-RAW", raw)
    else:
        from datetime import date, timedelta
        since = (date.today() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        typ, data = m.uid("SEARCH", None, f"SINCE {since}")
    if typ != "OK":
        return []
    uids = data[0].split() if data and data[0] else []
    # "UID n:*" always returns at least the last message even when nothing is new
    if last_uid:
        uids = [u for u in uids if int(u) > int(last_uid)]
    return uids


def fetch(cfg, src_cfg, creds, state, http) -> SourceResult:
    m = _connect()
    res = SourceResult(auth_ok=True)
    try:
        mailbox = src_cfg.get("mailbox", "INBOX")
        typ, _ = m.select(mailbox, readonly=True)
        if typ != "OK":
            raise AuthError(f"cannot open mailbox {mailbox}")
        uidvalidity = (m.response("UIDVALIDITY")[1] or [b""])[0]
        uidvalidity = uidvalidity.decode() if isinstance(uidvalidity, bytes) else str(uidvalidity or "")
        last_uid = state.get("last_uid") if state.get("uidvalidity") == uidvalidity else None
        host = env("EMAIL_IMAP_HOST") or "imap.gmail.com"
        domains = list(src_cfg.get("sender_domains") or ["umd.edu"])
        uids = _search(m, host, domains, int(src_cfg.get("lookback_days", 14)), last_uid)
        cap = int(src_cfg.get("max_messages_per_run", 200))
        if len(uids) > cap:
            res.notes.append(f"{len(uids)} candidates, capped to newest {cap}")
            uids = uids[-cap:]
        max_uid = int(last_uid) if last_uid else 0
        kept = 0
        for uid in uids:
            typ, data = m.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            fi = parse_message(data[0][1], cfg.courses, uid.decode(), domains)
            max_uid = max(max_uid, int(uid))
            if fi:
                res.feed_items.append(fi)
                kept += 1
        res.notes.append(f"{len(uids)} scanned, {kept} kept")
        if max_uid:
            res.state_updates["last_uid"] = str(max_uid)
        res.state_updates["uidvalidity"] = uidvalidity
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass
    return res


def probe(cfg, src_cfg, creds, http) -> tuple[bool, str]:
    try:
        m = _connect()
    except AuthError as e:
        return False, str(e)
    try:
        typ, data = m.select(src_cfg.get("mailbox", "INBOX"), readonly=True)
        n = data[0].decode() if typ == "OK" and data and data[0] else "?"
        return True, f"IMAP login ok as {env('EMAIL_USER')}; {n} messages in mailbox"
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass
