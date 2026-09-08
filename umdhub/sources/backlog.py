"""Backlog source: reads the study system's deferred-work backlog (read-only) and exposes
open DW items with their age, so the Hub can show "skills you owe a cold rep on".

Parses the `## Open Items` table in Claude Master/deferred-work-backlog.md. Rows look like:
| DW-002 | 2026-09-08 | CMSC351 | Gradescope Quiz 1 (…) | Cubic max … | **0** | 0–3 days | 🔴 OPEN — … |
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from ..core.timeutil import today_local
from .base import SourceResult

_ROW = re.compile(r"^\|\s*(DW-\d+)\s*\|")
_BOLD = re.compile(r"\*\*(.*?)\*\*")


def age_band(days: int) -> tuple[str, str]:
    """(band label, severity) per the backlog's own escalation table."""
    if days <= 3:
        return "0–3 days", "listed"
    if days <= 7:
        return "4–7 days", "required"
    if days <= 14:
        return "8–14 days", "urgent"
    return "15+ days", "failure"


def parse_backlog(text: str, today: date | None = None) -> list[dict]:
    today = today or today_local()
    out: list[dict] = []
    in_open = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_open = line.strip().lower().startswith("## open items")
            continue
        if not in_open or not _ROW.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        dw_id, opened, cls = cells[0], cells[1], cells[2]
        artifact = cells[3] if len(cells) > 3 else ""
        topic = cells[4] if len(cells) > 4 else ""
        reps_raw = cells[5] if len(cells) > 5 else ""
        status = cells[-1]
        m = _BOLD.search(reps_raw)
        reps = (m.group(1) if m else reps_raw).strip()
        try:
            opened_d = date.fromisoformat(opened)
            age = (today - opened_d).days
        except ValueError:
            age = -1
        band, severity = age_band(max(age, 0))
        is_open = "open" in status.lower() and "closed" not in status.lower()
        out.append({
            "id": dw_id, "opened": opened, "course": cls, "artifact": artifact, "topic": topic,
            "reps": reps, "status": status, "age_days": age, "band": band, "severity": severity,
            "open": is_open,
        })
    return out


def read_backlog(path: Path, today: date | None = None) -> list[dict]:
    if not path.exists():
        return []
    return parse_backlog(path.read_text(encoding="utf-8"), today)


def fetch(cfg, src_cfg, creds, state, http) -> SourceResult:
    res = SourceResult()
    path = cfg.backlog_path()
    if not path.exists():
        res.errors.append(f"backlog file missing: {path}")
        return res
    rows = read_backlog(path)
    res.state_updates["backlog_json"] = json.dumps(rows)
    res.notes.append(f"{sum(1 for r in rows if r['open'])} open DW items")
    return res
