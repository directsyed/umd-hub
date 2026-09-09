"""The brain: decides whether an observation is the same item as a stored one, and which
source's fields win. Pure functions on dicts — no I/O — so it can be tested exhaustively.

Precedence (higher wins for dates/url/submission fields):
  gradescope > canvas_api > canvas_ics > candidate > manual > course_site > seed
Seed wins for `weight_note` and `kind` (the syllabus knows it's an exam even when
Canvas files it as an "assignment"), except that a seed `expected` placeholder is
upgraded to the live kind. User `status` (done/snoozed/cancelled) is never touched here.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

SOURCE_PRIORITY: dict[str, int] = {
    "gradescope": 100,
    "canvas_api": 90,
    "canvas_ics": 85,
    "candidate": 70,
    "manual": 60,
    "course_site": 50,
    "seed": 10,
}

_SYNONYMS = {
    "hw": "homework", "hwk": "homework", "hmwk": "homework", "assignment": "homework",
    "proj": "project", "prj": "project", "projects": "project",
    # Canvas says "Midterm 1", a syllabus says "Exam 1", a professor says "Mid-term Exam I" —
    # one thing. Consecutive duplicates ("exam exam 1") are collapsed below.
    "midterm": "exam", "midterms": "exam", "mid-term": "exam", "exams": "exam", "test": "exam",
    "quizzes": "quiz",
    "lec": "lecture",
    "ps": "problemset", "pset": "problemset",
    "wk": "week",
}
# Kind words that name a category, not a specific item (stemmed form). A title reduced to one of
# these alone is too generic to claim another title by containment.
_GENERIC = {"homew", "quiz", "exam", "proje", "lectu", "lab", "essay", "paper", "draft", "final",
            "propo", "memo", "assig", "probl", "matla", "work"}
# Words that only describe a placeholder, never the thing itself; ignored when comparing.
_PLACEHOLDER = {"weekly", "daily", "biweekly", "recurring", "regular", "tbd", "tba", "date",
                "optional", "placeholder", "expected"}
_ORDINALS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
}
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8"}
_STOP = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "due", "at", "by", "with", "your"}
_COURSE_RE = re.compile(r"\b(?:bchm|cmsc|math|engl|stat|chem|phys)\s*-?\s*\d{3}\b", re.I)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_title(title: str, course: str | None = None) -> str:
    """Lowercase, drop course codes/punctuation/stopwords, map synonyms + ordinals, keep numbers.

    'Mid-term Exam I — Modules I+II' -> 'midterm exam 1 modules 1 2'
    'HW #3 (Recurrences)'            -> 'homework 3 recurrences'
    """
    s = unicodedata.normalize("NFKD", title)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()   # résumé -> resume
    s = _COURSE_RE.sub(" ", s)
    s = s.replace("mid-term", "midterm").replace("mid term", "midterm")
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    toks = _TOKEN_RE.findall(s)
    out: list[str] = []
    for t in toks:
        if t in _STOP:
            continue
        t = _SYNONYMS.get(t, t)
        t = _ORDINALS.get(t, t)
        t = _ROMAN.get(t, t)
        # "hw3" -> "homework 3"
        m = re.fullmatch(r"([a-z]+)(\d+)", t)
        if m:
            word, num = m.group(1), m.group(2)
            word = _SYNONYMS.get(word, word)
            out.extend([word, num])
            continue
        out.append(t)
    deduped: list[str] = []
    for t in out:
        if not deduped or deduped[-1] != t:
            deduped.append(t)
    return " ".join(deduped)


def _stem(t: str) -> str:
    """Light stemming for comparison only: 'introductory'/'intro', 'responses'/'response'."""
    return t if (t.isdigit() or len(t) < 6) else t[:5]


def tokens(norm: str) -> set[str]:
    """Comparison tokens: stemmed, with placeholder words removed."""
    return {_stem(t) for t in norm.split() if t not in _PLACEHOLDER}


def number_tokens(norm: str) -> set[str]:
    return {t for t in norm.split() if t.isdigit()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similar(a_norm: str, b_norm: str) -> bool:
    """Same thing? Number tokens must agree when both have them; then Jaccard ≥ 0.5 or containment."""
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    na, nb = number_tokens(a_norm), number_tokens(b_norm)
    if na and nb and na != nb:
        return False
    ta, tb = tokens(a_norm), tokens(b_norm)
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    # Containment: a placeholder like {lecture, quiz} sits inside "Lecture Quiz 1 - Syllabus…".
    # But a lone generic word ({homework}) would sit inside everything — that needs Jaccard.
    if small <= big and (len(small) >= 2 or not small <= _GENERIC):
        return True
    return _jaccard(ta, tb) >= 0.5


def dates_close(a: str | None, b: str | None, days: int = 1) -> bool:
    """YYYY-MM-DD strings. An undated side matches anything (undated seed placeholders)."""
    if not a or not b:
        return True
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    return abs((da - db).days) <= days


def _score(obs: dict, cand: dict) -> float:
    """Higher = better. Exact title beats containment beats Jaccard; closer date breaks ties."""
    a, b = obs["title_norm"], cand["title_norm"]
    if a == b:
        s = 3.0
    else:
        ta, tb = tokens(a), tokens(b)
        s = 2.0 if (ta <= tb or tb <= ta) else 1.0 + _jaccard(ta, tb)
    da, db = obs.get("due_date_local"), cand.get("due_date_local")
    if da and db:
        s -= 0.1 * abs((date.fromisoformat(da) - date.fromisoformat(db)).days)
    elif not db:
        s -= 0.05  # prefer a dated candidate over an undated placeholder when both match
    return s


def match(obs: dict, candidates: list[dict]) -> dict | None:
    """Pick the stored item this observation refers to, or None.

    obs / candidates carry: course, title_norm, due_date_local (may be None), and
    candidates carry id. Same course is assumed by the caller's query but re-checked.
    """
    best, best_score = None, -1.0
    for c in candidates:
        if c.get("course") != obs.get("course"):
            continue
        if not similar(obs["title_norm"], c["title_norm"]):
            continue
        if not dates_close(obs.get("due_date_local"), c.get("due_date_local")):
            continue
        s = _score(obs, c)
        if s > best_score:
            best, best_score = c, s
    return best


_DATE_FIELDS = ("due_at", "due_date_local", "all_day", "release_at", "late_due_at", "url",
                "submission_status", "score", "points", "max_points")


def merge_fields(existing: dict, obs: dict, obs_source: str) -> dict:
    """Return the column updates to apply to `existing` given a fresh observation."""
    obs_pri = SOURCE_PRIORITY.get(obs_source, 0)
    cur_src = existing.get("primary_source") or "seed"
    cur_pri = SOURCE_PRIORITY.get(cur_src, 0)
    up: dict = {}

    for f in _DATE_FIELDS:
        v = obs.get(f)
        if v is None or v == "":
            continue
        if obs_pri >= cur_pri or existing.get(f) in (None, ""):
            if existing.get(f) != v:
                up[f] = v

    # kind: seed wins, but an `expected` placeholder upgrades to whatever showed up.
    obs_kind = obs.get("kind")
    if obs_kind and obs_kind != "expected":
        if existing.get("kind") == "expected":
            up["kind"] = obs_kind
        elif cur_src != "seed" and obs_pri >= cur_pri and existing.get("kind") != obs_kind:
            up["kind"] = obs_kind

    # weight_note / notes: keep what we have, fill if empty.
    for f in ("weight_note", "notes"):
        if not existing.get(f) and obs.get(f):
            up[f] = obs[f]

    # title: highest-priority source names it; remember the seed's wording.
    obs_title = obs.get("title")
    if obs_title and obs_pri >= cur_pri and obs_title != existing.get("title"):
        if obs_source != "seed":
            up["title"] = obs_title
            up["title_norm"] = obs.get("title_norm") or normalize_title(obs_title)
            if cur_src == "seed":
                seed_note = f"seed: {existing.get('title')}"
                notes = existing.get("notes") or ""
                if seed_note not in notes:
                    up["notes"] = (notes + "\n" + seed_note).strip()

    if obs_pri > cur_pri:
        up["primary_source"] = obs_source
    return up
