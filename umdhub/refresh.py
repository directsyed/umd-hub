"""Collector entrypoint: python -m umdhub.refresh

One pass: every enabled source in SOURCE_ORDER (each isolated — one crash never kills
the pass), merge/upsert, wake snoozes, then the optional stages (LLM extraction of
pending feed items, Discord digest). Fired by umdhub-refresh.timer at 07:00/19:00 ET and
by the app's "refresh now" button; guarded by a flock so the two never overlap.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import sys
import uuid
from typing import Any

from .core.config import Config, load_config
from .core.http import HttpClient
from .core.state import State
from .core.timeutil import utcnow_iso
from .sources import SOURCE_ORDER, get_fetch
from .sources.base import AuthError

log = logging.getLogger("umdhub.refresh")


def _setup_logging(debug: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _run_source(cfg: Config, name: str, state: State, http: HttpClient, *, run_group: str,
                trigger: str, dry_run: bool) -> dict[str, Any]:
    src_cfg = cfg.source(name)
    stats: dict[str, Any] = {"ok": False, "fetched": 0, "new": 0, "updated": 0, "feed_new": 0,
                             "error": None, "notes": []}
    run_id = None if dry_run else state.start_run(name, trigger, run_group)
    error: str | None = None
    try:
        fetch = get_fetch(name)
        creds = state.creds_for(name)
        src_state = state.meta_with_prefix(f"{name}:")
        res = fetch(cfg, src_cfg, creds, src_state, http)
        stats["fetched"] = len(res.items) + len(res.feed_items) + len(res.grades)
        stats["notes"] = list(res.notes)
        if dry_run:
            for it in res.items:
                log.info("DRY %s item: [%s/%s] %s @ %s", name, it.course, it.kind, it.title, it.due_at)
            for f in res.feed_items:
                log.info("DRY %s feed: [%s] %s", name, f.course, f.subject[:90])
            for g in res.grades:
                log.info("DRY %s grade: [%s] %s = %s/%s", name, g.course, g.title, g.score, g.max_score)
        else:
            for it in res.items:
                _, was_new, changed = state.upsert_item(it)
                stats["new"] += int(was_new)
                stats["updated"] += int(changed)
            for f in res.feed_items:
                _, is_new = state.upsert_feed_item(f)
                stats["feed_new"] += int(is_new)
            for g in res.grades:
                state.upsert_grade(g)
            for k, v in res.state_updates.items():
                state.meta_set(f"{name}:{k}", v)
            if res.auth_ok is not None:
                state.credential_health(name, ok=res.auth_ok,
                                        error="; ".join(res.errors) if not res.auth_ok else None)
        if res.errors:
            error = "; ".join(res.errors)[:500]
            log.warning("source %s reported: %s", name, error)
        stats["ok"] = not res.errors
    except AuthError as e:
        error = f"auth: {e}"[:500]
        log.warning("source %s auth failure: %s", name, e)
        if not dry_run:
            state.credential_health(name, ok=False, error=str(e))
    except ModuleNotFoundError as e:
        error = f"not installed: {e}"[:500]
        log.info("source %s skipped — %s", name, error)
    except Exception as e:  # noqa: BLE001 — isolation is the point
        log.exception("source %s crashed: %s", name, e)
        error = str(e)[:500]
    finally:
        stats["error"] = error
        if run_id is not None:
            state.finish_run(run_id, ok=stats["ok"], fetched=stats["fetched"], new_count=stats["new"],
                             updated_count=stats["updated"], error=error)
    log.info("source %-12s ok=%s fetched=%d new=%d updated=%d feed_new=%d %s",
             name, stats["ok"], stats["fetched"], stats["new"], stats["updated"], stats["feed_new"],
             ("· " + "; ".join(stats["notes"])) if stats["notes"] else "")
    return stats


def one_pass(cfg: Config, state: State, http: HttpClient, *, trigger: str = "cli",
             only: set[str] | None = None, dry_run: bool = False, no_extract: bool = False,
             no_notify: bool = False) -> dict[str, Any]:
    run_group = uuid.uuid4().hex[:12]
    started = utcnow_iso()
    results: dict[str, Any] = {}
    for name in SOURCE_ORDER:
        if only is not None:
            if name not in only:
                continue
        elif not cfg.source(name).enabled:
            continue
        results[name] = _run_source(cfg, name, state, http, run_group=run_group, trigger=trigger,
                                    dry_run=dry_run)

    if not dry_run:
        woke = state.wake_snoozed()
        if woke:
            log.info("woke %d snoozed items", woke)

    extract_stats: dict[str, Any] | None = None
    if cfg.extract.enabled and not dry_run and not no_extract:
        try:
            from . import extract  # optional stage — imported lazily
            extract_stats = extract.run(cfg, state)
        except ModuleNotFoundError:
            log.info("extractor stage not installed yet — skipping")
        except Exception as e:  # noqa: BLE001
            log.exception("extractor stage failed: %s", e)
            extract_stats = {"error": str(e)[:300]}

    notify_stats: dict[str, Any] | None = None
    if cfg.notify.enabled and not dry_run and not no_notify:
        try:
            from .notify import discord  # optional stage — imported lazily
            notify_stats = discord.send_digest(cfg, state, results, extract_stats)
        except ModuleNotFoundError:
            log.info("notify stage not installed yet — skipping")
        except Exception as e:  # noqa: BLE001
            log.exception("notify stage failed: %s", e)
            notify_stats = {"error": str(e)[:300]}

    summary = {
        "run_group": run_group, "trigger": trigger, "started_at": started, "finished_at": utcnow_iso(),
        "sources": results, "extract": extract_stats, "notify": notify_stats,
        "ok": any(r["ok"] for r in results.values()) if results else False,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="UMD Hub collector — one pass")
    p.add_argument("--dry-run", action="store_true", help="fetch + parse + print; no DB writes, no claude, no Discord")
    p.add_argument("--sources", default=None, help="comma-separated source names (overrides enabled flags)")
    p.add_argument("--no-extract", action="store_true", help="skip the claude -p extraction stage")
    p.add_argument("--no-notify", action="store_true", help="skip the Discord digest")
    p.add_argument("--trigger", default="cli", choices=["timer", "manual", "cli"])
    p.add_argument("--config", default=None)
    p.add_argument("--secrets", default=None)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)
    _setup_logging(args.debug)

    cfg = load_config(args.config, args.secrets)
    only = {s.strip() for s in args.sources.split(",") if s.strip()} if args.sources else None

    lock_fh = None
    if not args.dry_run:
        lock_path = cfg.lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = open(lock_path, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("another refresh is running (lock %s) — exiting", lock_path)
            return 0

    state = State(cfg.db_path())
    http = HttpClient()
    ok = False
    try:
        if not args.dry_run:
            state.meta_set("refresh_in_progress", "1")
            state.meta_set("refresh_started_at", utcnow_iso())
        summary = one_pass(cfg, state, http, trigger=args.trigger, only=only, dry_run=args.dry_run,
                           no_extract=args.no_extract, no_notify=args.no_notify)
        ok = bool(summary["ok"])
        if not args.dry_run:
            state.meta_set("last_refresh_at", summary["finished_at"])
            state.meta_set("last_refresh_ok", "1" if ok else "0")
            state.meta_set("last_refresh_summary", json.dumps(summary, default=str))
        log.info("pass %s: %s", summary["run_group"],
                 ", ".join(f"{k}={'ok' if v['ok'] else 'FAIL'}" for k, v in summary["sources"].items()))
    finally:
        if not args.dry_run:
            state.meta_set("refresh_in_progress", "0")
        state.close()
        if lock_fh:
            lock_fh.close()
    return 0 if ok or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
