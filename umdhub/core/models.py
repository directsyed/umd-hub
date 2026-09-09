"""Shared dataclasses + the SQLite schema. This is THE data contract every layer uses."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any

KINDS = ("assignment", "quiz", "exam", "project", "event", "admin", "expected")
# open → still to do · done → submitted · snoozed → hidden until a date · missed → was real, was due,
# never submitted and now closed · cancelled → doesn't apply (dropped by the professor, not mine)
STATUSES = ("open", "done", "snoozed", "missed", "cancelled")
KIND_ICON = {
    "assignment": "📝", "quiz": "❓", "exam": "🎓", "project": "🛠️",
    "event": "📅", "admin": "⚠️", "expected": "◌",
}


@dataclass
class Item:
    """One dated thing. Sources fill the raw fields; state/merge decide identity."""
    course: str
    kind: str
    title: str
    source: str            # producing source name (seed, canvas_ics, gradescope, ...)
    source_id: str         # unique within the source; the merge key
    due_at: str | None = None          # UTC ISO
    all_day: bool = False
    due_date_local: str | None = None  # YYYY-MM-DD in ET; computed from due_at if absent
    release_at: str | None = None
    late_due_at: str | None = None
    url: str | None = None
    points: float | None = None
    max_points: float | None = None
    weight_note: str | None = None
    submission_status: str | None = None
    score: str | None = None
    notes: str | None = None
    raw: dict[str, Any] | None = None

    def to_obs(self, title_norm: str) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        d["title_norm"] = title_norm
        d["all_day"] = 1 if self.all_day else 0
        return d


@dataclass
class FeedItem:
    """A raw message: email, Piazza post, Canvas announcement, or a course-site diff."""
    source: str            # email | piazza | canvas_announce | site_diff
    source_id: str
    subject: str
    body: str
    course: str | None = None
    author: str | None = None
    is_instructor: bool = False
    url: str | None = None
    posted_at: str | None = None  # UTC ISO


@dataclass
class Grade:
    course: str
    title: str
    source: str
    source_id: str
    score: float | None = None
    max_score: float | None = None
    item_id: int | None = None


@dataclass
class Candidate:
    course: str
    kind: str
    title: str
    action: str = "new"          # new | move | cancel
    due_at: str | None = None
    due_date_local: str | None = None
    all_day: bool = False
    confidence: float = 0.0
    quote: str | None = None
    feed_item_id: int | None = None
    matched_item_id: int | None = None
    batch_id: str | None = None


def item_hash(course: str, kind: str, title_norm: str, due_at: str | None, url: str | None) -> str:
    payload = "\x1f".join([course, kind, title_norm, due_at or "", url or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS item (
  id                INTEGER PRIMARY KEY,
  course            TEXT NOT NULL,
  kind              TEXT NOT NULL,
  title             TEXT NOT NULL,
  title_norm        TEXT NOT NULL,
  due_at            TEXT,
  due_date_local    TEXT,
  all_day           INTEGER NOT NULL DEFAULT 0,
  release_at        TEXT,
  late_due_at       TEXT,
  url               TEXT,
  status            TEXT NOT NULL DEFAULT 'open',
  snoozed_until     TEXT,
  points            REAL,
  max_points        REAL,
  weight_note       TEXT,
  submission_status TEXT,
  score             TEXT,
  primary_source    TEXT NOT NULL,
  first_seen        TEXT NOT NULL,
  last_seen         TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  hash              TEXT NOT NULL,
  prev_hash         TEXT,
  changed_at        TEXT,
  notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_item_due_local ON item(due_date_local, status);
CREATE INDEX IF NOT EXISTS idx_item_course ON item(course, status);

CREATE TABLE IF NOT EXISTS item_source (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  source      TEXT NOT NULL,
  source_id   TEXT NOT NULL,
  url         TEXT,
  raw         TEXT,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_item_source_item ON item_source(item_id);

CREATE TABLE IF NOT EXISTS feed_item (
  id             INTEGER PRIMARY KEY,
  source         TEXT NOT NULL,
  source_id      TEXT NOT NULL,
  course         TEXT,
  author         TEXT,
  is_instructor  INTEGER NOT NULL DEFAULT 0,
  subject        TEXT NOT NULL,
  body           TEXT,
  url            TEXT,
  posted_at      TEXT,
  fetched_at     TEXT NOT NULL,
  read_at        TEXT,
  extract_status TEXT NOT NULL DEFAULT 'pending',
  extracted_at   TEXT,
  UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_feed_posted ON feed_item(posted_at);
CREATE INDEX IF NOT EXISTS idx_feed_extract ON feed_item(extract_status);

CREATE TABLE IF NOT EXISTS candidate (
  id               INTEGER PRIMARY KEY,
  feed_item_id     INTEGER REFERENCES feed_item(id) ON DELETE SET NULL,
  course           TEXT NOT NULL,
  kind             TEXT NOT NULL,
  title            TEXT NOT NULL,
  title_norm       TEXT NOT NULL,
  due_at           TEXT,
  due_date_local   TEXT,
  all_day          INTEGER NOT NULL DEFAULT 0,
  confidence       REAL NOT NULL DEFAULT 0,
  quote            TEXT,
  action           TEXT NOT NULL DEFAULT 'new',
  matched_item_id  INTEGER REFERENCES item(id) ON DELETE SET NULL,
  state            TEXT NOT NULL DEFAULT 'pending',
  batch_id         TEXT,
  created_at       TEXT NOT NULL,
  decided_at       TEXT,
  UNIQUE(feed_item_id, title_norm, due_date_local)
);
CREATE INDEX IF NOT EXISTS idx_candidate_state ON candidate(state);

CREATE TABLE IF NOT EXISTS grade (
  id          INTEGER PRIMARY KEY,
  course      TEXT NOT NULL,
  item_id     INTEGER REFERENCES item(id) ON DELETE SET NULL,
  title       TEXT NOT NULL,
  score       REAL,
  max_score   REAL,
  source      TEXT NOT NULL,
  source_id   TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS credential (
  source          TEXT NOT NULL,
  name            TEXT NOT NULL,
  value           TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  last_ok_at      TEXT,
  last_error      TEXT,
  last_checked_at TEXT,
  PRIMARY KEY (source, name)
);

CREATE TABLE IF NOT EXISTS site_snapshot (
  course     TEXT NOT NULL,
  url        TEXT NOT NULL,
  text       TEXT NOT NULL,
  hash       TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (course, url)
);

CREATE TABLE IF NOT EXISTS run (
  id            INTEGER PRIMARY KEY,
  run_group     TEXT NOT NULL,
  trigger       TEXT NOT NULL,
  source        TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  ok            INTEGER NOT NULL DEFAULT 0,
  fetched       INTEGER,
  new_count     INTEGER,
  updated_count INTEGER,
  error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_started ON run(started_at);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""
