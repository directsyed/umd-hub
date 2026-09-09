"""LLM extraction of deadline facts from feed items, via the local Claude Code CLI.

`claude -p` runs on the user's subscription (OAuth) — never --bare, which forces API-key auth.
Structured output is enforced with --json-schema; tools are disabled; the CWD is an empty
temp dir so no CLAUDE.md is auto-loaded. Results become `candidate` rows the user confirms
in the tray. This stage is optional: it runs last, is time-boxed, and its failures never
touch the structured sources.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

from .core import merge
from .core.config import Config
from .core.models import Candidate
from .core.state import State
from .core.timeutil import from_local, today_local, utcnow, utcnow_iso

log = logging.getLogger("umdhub.extract")
HERE = Path(__file__).parent
PROMPT_FILE = HERE / "extract_prompt.txt"
SCHEMA_FILE = HERE / "extract_schema.json"


def system_prompt(cfg: Config, today: date | None = None) -> str:
    today = today or today_local()
    courses = "\n".join(
        f"  - {code}: {c.name} (instructor email(s): {', '.join(c.email_senders) or 'n/a'})"
        for code, c in cfg.courses.items())
    return PROMPT_FILE.read_text(encoding="utf-8").format(
        today=today.isoformat(), sem_start=cfg.semester.start, sem_end=cfg.semester.end, courses=courses)


def schema(cfg: Config) -> dict:
    s = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    s["properties"]["candidates"]["items"]["properties"]["course"]["enum"] = [*cfg.course_codes(), "UNKNOWN"]
    return s


def build_user_message(rows, cfg: Config) -> str:
    blocks = []
    for n, r in enumerate(rows, 1):
        body = (r["body"] or "").strip()
        if len(body) > cfg.extract.max_body_chars:
            body = body[: cfg.extract.max_body_chars] + "\n[…truncated]"
        blocks.append(
            f"### [{n}] source={r['source']} course_guess={r['course'] or 'UNKNOWN'} "
            f"from={r['author'] or '?'} sent={r['posted_at'] or r['fetched_at']}\n"
            f"subject: {r['subject']}\n{body}")
    return "\n\n".join(blocks)


def _usage_of(obj: dict, seconds: float) -> dict:
    """Pull token/cost accounting out of a claude -p result envelope (fields vary by version)."""
    u = obj.get("usage") or {}
    return {
        "calls": 1,
        "tokens_in": int(u.get("input_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "cache_write": int(u.get("cache_creation_input_tokens") or 0),
        "tokens_out": int(u.get("output_tokens") or 0),
        "cost_usd": float(obj.get("total_cost_usd") or 0.0),
        "seconds": round(seconds, 1),
    }


def _add_usage(total: dict, part: dict) -> None:
    for k, v in part.items():
        total[k] = round(total.get(k, 0) + v, 4) if isinstance(v, float) else total.get(k, 0) + v


def call_claude(cfg: Config, sys_prompt: str, user_msg: str, *, timeout: int | None = None,
                usage: dict | None = None) -> dict | None:
    """Run one claude -p invocation. Returns the parsed structured output, or None on failure.
    If `usage` is given, token/cost accounting for the call is accumulated into it."""
    cmd = [
        cfg.extract.claude_bin, "-p",
        "--model", cfg.extract.model,
        "--output-format", "json",
        "--json-schema", json.dumps(schema(cfg)),
        "--tools", "",
        "--system-prompt", sys_prompt,
        "--no-session-persistence",
        "--permission-mode", "dontAsk",
        "--max-budget-usd", str(cfg.extract.budget_usd),
    ]
    if cfg.extract.effort:
        cmd += ["--effort", cfg.extract.effort]
    # Drop nested-session variables (when run from inside an interactive Claude Code session) but
    # keep the long-lived token that headless runs authenticate with (`claude setup-token`).
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_CODE_") or k == "CLAUDE_CODE_OAUTH_TOKEN"}
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="umdhub-extract-") as cwd:
        try:
            r = subprocess.run(cmd, input=user_msg, capture_output=True, text=True,
                               timeout=timeout or cfg.extract.timeout_s, cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            log.warning("claude -p timed out after %ss", timeout or cfg.extract.timeout_s)
            return None
        except FileNotFoundError:
            log.error("claude binary %r not found", cfg.extract.claude_bin)
            return None
    elapsed = time.monotonic() - t0
    if r.returncode != 0:
        log.warning("claude -p exit %s: %s", r.returncode, (r.stderr or r.stdout)[:400].strip())
        return None
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError:
        log.warning("claude -p returned non-JSON: %s", r.stdout[:200].strip())
        return None
    if isinstance(obj, dict) and usage is not None:
        _add_usage(usage, _usage_of(obj, elapsed))
    if isinstance(obj, dict):
        if obj.get("structured_output") is not None:
            return obj["structured_output"]
        if obj.get("subtype") and obj.get("subtype") != "success":
            log.warning("claude -p subtype=%s", obj.get("subtype"))
            return None
        result = obj.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            m = re.search(r"\{.*\}", result, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        if "candidates" in obj:
            return obj
    log.warning("claude -p output had no structured_output")
    return None


_DUE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}))?$")


def to_candidates(payload: dict, rows_by_ref: dict[int, object], cfg: Config, batch_id: str) -> list[Candidate]:
    out: list[Candidate] = []
    for raw in (payload or {}).get("candidates") or []:
        try:
            ref = int(raw.get("feed_ref"))
        except (TypeError, ValueError):
            continue
        row = rows_by_ref.get(ref)
        if row is None:
            continue
        course = raw.get("course") or "UNKNOWN"
        if course not in cfg.courses:
            course = row["course"]
        if not course:
            log.info("extract: dropping candidate with unknown course: %r", raw.get("title"))
            continue
        title = (raw.get("title") or "").strip()
        if not title:
            continue
        due_at, all_day = None, False
        due_local = raw.get("due_local")
        if due_local:
            m = _DUE.match(str(due_local).strip())
            if m:
                try:
                    due_at, all_day = from_local(m.group(1), m.group(2) if raw.get("due_known_time") else None)
                except ValueError:
                    due_at, all_day = None, False
        action = raw.get("action") if raw.get("action") in ("new", "move", "cancel") else "new"
        if not due_at and action != "cancel":
            continue
        try:
            conf = max(0.0, min(1.0, float(raw.get("confidence", 0))))
        except (TypeError, ValueError):
            conf = 0.0
        kind = raw.get("kind") if raw.get("kind") in ("assignment", "quiz", "exam", "project", "event", "admin") else "assignment"
        out.append(Candidate(course=course, kind=kind, title=title[:200], action=action, due_at=due_at,
                             all_day=all_day, confidence=conf, quote=(raw.get("quote") or "")[:1000] or None,
                             feed_item_id=int(row["id"]), batch_id=batch_id))
    return out


def _place(c: Candidate, state: State, stats: dict) -> None:
    """Match a candidate against open items; auto-merge exact repeats, tray everything else."""
    obs = {"course": c.course, "title_norm": merge.normalize_title(c.title, c.course),
           "due_date_local": c.due_date_local}
    from .core.timeutil import local_date_str
    obs["due_date_local"] = local_date_str(c.due_at) if c.due_at else None
    best = merge.match(obs, state.match_pool(c.course))
    if best:
        c.matched_item_id = int(best["id"])
        same_day = obs["due_date_local"] and best.get("due_date_local") == obs["due_date_local"]
        if c.action == "new" and same_day:
            cid = state.add_candidate(c)
            if cid:
                state.decide_candidate(cid, "auto_merged", c.matched_item_id)
                state.conn.execute(
                    "INSERT OR IGNORE INTO item_source(item_id, source, source_id, first_seen, last_seen) "
                    "VALUES(?,?,?,?,?)",
                    (c.matched_item_id, "extract", f"cand:{cid}", utcnow_iso(), utcnow_iso()))
                stats["auto_merged"] += 1
            return
        if c.action == "new":
            c.action = "move"
    elif c.action == "cancel":
        stats["dropped"] += 1
        return
    if state.add_candidate(c):
        stats["candidates"] += 1


def run(cfg: Config, state: State) -> dict:
    stats = {"batches": 0, "feed_items": 0, "candidates": 0, "auto_merged": 0, "failed": 0,
             "skipped": 0, "dropped": 0}
    usage: dict = {}
    if shutil.which(cfg.extract.claude_bin) is None:
        stats["error"] = f"{cfg.extract.claude_bin} not on PATH"
        log.warning("extract: %s", stats["error"])
        return stats
    # Items that failed (timeouts, transient CLI errors) get one more chance every N hours.
    cutoff = (utcnow() - timedelta(hours=cfg.extract.retry_failed_after_hours)).replace(microsecond=0).isoformat()
    stats["requeued"] = state.conn.execute(
        "UPDATE feed_item SET extract_status='pending' WHERE extract_status='failed' AND extracted_at < ?",
        (cutoff,)).rowcount
    sys_prompt = system_prompt(cfg)
    deadline = time.monotonic() + cfg.extract.time_box_s
    for _ in range(cfg.extract.max_batches_per_run):
        rows = state.pending_extraction(cfg.extract.sources, cfg.extract.batch_size)
        if not rows:
            break
        tiny = [int(r["id"]) for r in rows if len((r["body"] or "").strip()) < cfg.extract.min_body_chars]
        if tiny:
            state.set_extract_status(tiny, "skipped")
            stats["skipped"] += len(tiny)
        rows = [r for r in rows if int(r["id"]) not in tiny]
        if not rows:
            continue
        # keep the batch under the character budget
        kept, total = [], 0
        for r in rows:
            n = min(len(r["body"] or ""), cfg.extract.max_body_chars) + 200
            if kept and total + n > cfg.extract.max_batch_chars:
                break
            kept.append(r)
            total += n
        rows = kept
        ids = [int(r["id"]) for r in rows]
        batch_id = uuid.uuid4().hex[:8]
        user_msg = build_user_message(rows, cfg)
        payload = call_claude(cfg, sys_prompt, user_msg, usage=usage)
        if payload is None:
            payload = call_claude(cfg, sys_prompt, user_msg, usage=usage)  # one retry
        if payload is None:
            if stats["batches"] == 0:
                # Nothing has worked this run → almost certainly the CLI itself (auth, quota,
                # binary). Leave the items pending so they're picked up once it's fixed.
                stats["error"] = (f"claude -p failed on the first batch; {len(ids)} feed item(s) left pending "
                                  f"— run `python -m umdhub.probe claude`")
                log.warning("extract: %s", stats["error"])
                break
            state.set_extract_status(ids, "failed")
            stats["failed"] += len(ids)
        else:
            for c in to_candidates(payload, {i + 1: r for i, r in enumerate(rows)}, cfg, batch_id):
                _place(c, state, stats)
            state.set_extract_status(ids, "done")
            stats["feed_items"] += len(ids)
        stats["batches"] += 1
        if time.monotonic() > deadline:
            stats["note"] = "time box reached"
            break
    if usage:
        stats["usage"] = usage
        lifetime = state.meta_json("extract:lifetime", {}) or {}
        _add_usage(lifetime, usage)
        lifetime["runs"] = lifetime.get("runs", 0) + 1
        state.meta_set("extract:lifetime", json.dumps(lifetime))
    log.info("extract: %s", stats)
    return stats
