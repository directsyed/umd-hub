"""Common types for source adapters.

Contract:  fetch(cfg, src_cfg, creds, state, http) -> SourceResult
  cfg      Config
  src_cfg  the source's SourceCfg block
  creds    {name: value} from the credential table for this source (env fallbacks are
           the adapter's own business via core.config.env)
  state    read-only view of meta for this source (last UID, last timestamps...) — the
           adapter returns state_updates rather than writing
  http     HttpClient
Raise AuthError when a login wall / expired cookie is detected: the collector marks the
credential unhealthy, keeps every stale item, and continues with the next source.
Optional: probe(cfg, src_cfg, creds, http) -> (ok: bool, message: str) for the
credential panel's Test button.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..core.models import FeedItem, Grade, Item


class AuthError(Exception):
    """Credential missing/expired/rejected. Not a code bug — a user action fixes it."""


@dataclass
class SourceResult:
    items: list[Item] = field(default_factory=list)
    feed_items: list[FeedItem] = field(default_factory=list)
    grades: list[Grade] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    auth_ok: bool | None = None        # None = source has no credential
    state_updates: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CredSlot:
    source: str
    name: str
    label: str
    help: str
    placeholder: str = ""
    multiline: bool = False


# What the /credentials page offers to paste. Password-type secrets stay in secrets.env.
CREDENTIAL_SLOTS: list[CredSlot] = [
    CredSlot("canvas_ics", "ics_url", "Canvas calendar feed URL",
             "ELMS → Calendar → “Calendar Feed” (bottom right) → copy the .ics link. "
             "It is a secret URL: anyone with it can read your calendar.",
             "https://umd.instructure.com/feeds/calendars/user_….ics"),
    CredSlot("gradescope", "cookie", "Gradescope cookies",
             "Log in on gradescope.com, then DevTools → Application → Cookies → www.gradescope.com. "
             "Paste signed_token and _gradescope_session as one line.",
             "signed_token=…; _gradescope_session=…", multiline=True),
    CredSlot("canvas_api", "cookie", "Canvas session cookie (optional)",
             "Only needed for submission status / grades via the API. "
             "DevTools → Cookies → umd.instructure.com → canvas_session (+ _legacy_normandy_session).",
             "canvas_session=…; _legacy_normandy_session=…", multiline=True),
    CredSlot("piazza", "cookie", "Piazza session cookie (optional)",
             "Skip if PIAZZA_EMAIL / PIAZZA_PASSWORD are in secrets.env. "
             "Otherwise: DevTools → Cookies → piazza.com → session_id (+ piazza_session).",
             "session_id=…; piazza_session=…", multiline=True),
]

# Environment-only credentials, shown read-only (set / unset) on the panel.
ENV_SLOTS: list[tuple[str, str, str]] = [
    ("email_imap", "EMAIL_USER", "IMAP account"),
    ("email_imap", "EMAIL_APP_PASSWORD", "IMAP app password"),
    ("piazza", "PIAZZA_EMAIL", "Piazza email"),
    ("piazza", "PIAZZA_PASSWORD", "Piazza password"),
    ("notify", "DISCORD_WEBHOOK_URL", "Discord webhook"),
    ("web", "HUB_SHARED_SECRET", "LAN login secret"),
]

_COOKIE_PAIR = re.compile(r"\s*([^=;\s]+)\s*=\s*([^;]*)")


def parse_cookie_header(s: str | None) -> dict[str, str]:
    """'a=1; b=2' -> {'a': '1', 'b': '2'}. Tolerates newlines and a leading 'Cookie:'."""
    if not s:
        return {}
    s = s.strip()
    if s.lower().startswith("cookie:"):
        s = s[7:]
    s = s.replace("\n", ";")
    return {k: v.strip() for k, v in _COOKIE_PAIR.findall(s) if k}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80]


def guess_course(text: str, courses: dict) -> str | None:
    """Best-effort course from free text using each course's subject_tokens + canvas_names."""
    if not text:
        return None
    low = text.lower()
    best, best_len = None, 0
    for code, c in courses.items():
        for tok in [code, *c.subject_tokens, *c.canvas_names]:
            t = tok.lower()
            if t and t in low and len(t) > best_len:
                best, best_len = code, len(t)
    return best
