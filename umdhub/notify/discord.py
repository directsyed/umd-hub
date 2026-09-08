"""Discord webhook digest after each refresh. Adapted from Hardware Parser notify/discord.py.

One message, one embed per non-empty section: new items · changed · due ≤ 24 h · overdue ·
candidates waiting · credential problems · source errors · deferred-work backlog ≥ N days.
Nothing to say → nothing posted (but last_notified_at still advances).
"""
from __future__ import annotations

import logging
from datetime import timedelta

import requests

from ..core.config import Config, env
from ..core.state import State
from ..core.timeutil import fmt_et, parse_iso, today_local, utcnow, utcnow_iso

log = logging.getLogger(__name__)

GREEN, BLUE, ORANGE, RED, GREY, PURPLE = 0x2ECC71, 0x3498DB, 0xE67E22, 0xE74C3C, 0x95A5A6, 0x9B59B6


def webhook_url(cfg: Config) -> str | None:
    return env(cfg.notify.discord_webhook_env)


def _line(row) -> str:
    when = fmt_et(row["due_at"], bool(row["all_day"])) if row["due_at"] else "no date"
    title = row["title"]
    if row["url"]:
        title = f"[{title}]({row['url']})"
    return f"• **{row['course']}** {title} — {when}"


def _embed(title: str, lines: list[str], color: int, cap: int = 15) -> dict:
    shown = lines[:cap]
    if len(lines) > cap:
        shown.append(f"… and {len(lines) - cap} more")
    return {"title": title, "description": "\n".join(shown)[:4000], "color": color}


def build_digest(cfg: Config, state: State, results: dict | None = None,
                 extract_stats: dict | None = None) -> tuple[list[dict], bool]:
    now = utcnow()
    now_iso = utcnow_iso()
    since = state.meta_get("last_notified_at") or (now - timedelta(hours=24)).replace(microsecond=0).isoformat()
    embeds: list[dict] = []
    substantive = False

    new = state.new_since(since)
    if new:
        embeds.append(_embed(f"🆕 New ({len(new)})", [_line(r) for r in new], GREEN))
        substantive = True
    changed = [r for r in state.recently_changed(since) if r["status"] == "open"]
    if changed:
        embeds.append(_embed(f"✏️ Changed ({len(changed)})", [_line(r) for r in changed], BLUE))
        substantive = True
    soon = state.due_between(now_iso, (now + timedelta(hours=cfg.notify.due_soon_hours)).isoformat())
    if soon:
        embeds.append(_embed(f"⏰ Due within {cfg.notify.due_soon_hours} h ({len(soon)})",
                             [_line(r) for r in soon], ORANGE))
        substantive = True
    overdue = state.overdue(now_iso, today_local().isoformat())
    if overdue:
        embeds.append(_embed(f"🔴 Overdue / not marked done ({len(overdue)})", [_line(r) for r in overdue], RED))
        substantive = True
    pending = state.candidates("pending")
    if pending:
        lines = [f"• **{c['course']}** {c['title']} ({c['action']}, {int(c['confidence'] * 100)}%)" for c in pending]
        embeds.append(_embed(f"📥 Candidates waiting for you ({len(pending)})", lines, PURPLE, cap=6))
        substantive = True

    problems: list[str] = []
    stale_cut = now - timedelta(days=cfg.notify.credential_stale_days)
    for (source, name), row in state.credentials().items():
        if row["last_error"]:
            problems.append(f"• `{source}:{name}` — {row['last_error'][:120]}")
        elif row["last_ok_at"]:
            ok_at = parse_iso(row["last_ok_at"])
            if ok_at and ok_at < stale_cut:
                problems.append(f"• `{source}:{name}` — last worked {fmt_et(row['last_ok_at'])}")
    for src, r in (results or {}).items():
        if r.get("error") and not r["error"].startswith("not installed"):
            problems.append(f"• `{src}` — {r['error'][:140]}")
    if extract_stats and extract_stats.get("error"):
        problems.append(f"• `extract` — {extract_stats['error'][:140]}")
    if problems:
        embeds.append(_embed("⚠️ Needs attention", problems, RED, cap=10))
        substantive = True

    backlog = state.meta_json("backlog:backlog_json", []) or []
    old = [b for b in backlog if b.get("open") and b.get("age_days", 0) >= cfg.notify.backlog_alert_days]
    if old:
        lines = [f"• **{b['id']}** {b['course']} — {b['artifact'][:70]} ({b['age_days']} d, {b['band']})" for b in old]
        embeds.append(_embed(f"🧠 Deferred-work backlog ≥ {cfg.notify.backlog_alert_days} d", lines, GREY))
        substantive = True
    return embeds[:10], substantive


def post(url: str, *, content: str | None = None, embeds: list[dict] | None = None,
         username: str = "umd-hub") -> bool:
    payload: dict = {"username": username}
    if content:
        payload["content"] = content[:1900]
    if embeds:
        payload["embeds"] = embeds
    try:
        resp = requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        log.warning("discord post failed: %s", e)
        return False
    if resp.status_code >= 400:
        log.warning("discord webhook %s: %s", resp.status_code, resp.text[:200])
        return False
    return True


def send_digest(cfg: Config, state: State, results: dict | None = None,
                extract_stats: dict | None = None) -> dict:
    url = webhook_url(cfg)
    if not url:
        return {"skipped": "no webhook configured"}
    embeds, substantive = build_digest(cfg, state, results, extract_stats)
    if not substantive:
        state.meta_set("last_notified_at", utcnow_iso())
        return {"skipped": "nothing to report", "embeds": 0}
    ok = post(url, embeds=embeds, username=cfg.notify.username,
              content=f"UMD Hub refresh · {fmt_et(utcnow_iso())} ET")
    if ok:
        state.meta_set("last_notified_at", utcnow_iso())
    return {"sent": ok, "embeds": len(embeds)}


def send_smoke(cfg: Config, message: str) -> bool:
    url = webhook_url(cfg)
    return bool(url) and post(url, content=message, username=cfg.notify.username)
