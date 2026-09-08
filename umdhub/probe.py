"""Credential / environment probes: python -m umdhub.probe {email|gradescope|piazza|canvas|claude|discord|all}

Each probe answers one question a human would otherwise have to guess at ("is it my app
password or did the admin disable IMAP?", "does claude -p work headless?").
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from .core.config import load_config
from .core.http import HttpClient
from .core.state import State
from .sources import get_probe

ALIASES = {"canvas": "canvas_ics", "email": "email_imap", "imap": "email_imap"}


def probe_claude(cfg) -> tuple[bool, str]:
    cmd = [cfg.extract.claude_bin, "-p", "--model", cfg.extract.model, "--output-format", "json",
           "--tools", "", "--no-session-persistence", "--max-budget-usd", "0.05",
           "--system-prompt", "Reply with exactly the single word: pong"]
    try:
        r = subprocess.run(cmd, input="ping", capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return False, f"{cfg.extract.claude_bin} not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "claude -p timed out (auth prompt waiting for a browser?)"
    if r.returncode != 0:
        return False, f"exit {r.returncode}: {(r.stderr or r.stdout)[:300].strip()}"
    try:
        obj = json.loads(r.stdout)
        text = str(obj.get("result", ""))
    except json.JSONDecodeError:
        text = r.stdout
    ok = "pong" in text.lower()
    return ok, f"replied {text.strip()[:60]!r} (model alias {cfg.extract.model})"


def probe_discord(cfg) -> tuple[bool, str]:
    from .notify import discord
    if not discord.webhook_url(cfg):
        return False, "DISCORD_WEBHOOK_URL not set"
    ok = discord.send_smoke(cfg, "umd-hub: webhook probe ✓")
    return ok, "posted a test message" if ok else "webhook rejected the post"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="UMD Hub probes")
    p.add_argument("what", choices=["email", "imap", "gradescope", "piazza", "canvas", "canvas_ics", "canvas_api",
                                    "claude", "discord", "all"])
    p.add_argument("--config", default=None)
    p.add_argument("--secrets", default=None)
    args = p.parse_args(argv)
    cfg = load_config(args.config, args.secrets)
    targets = (["canvas_ics", "gradescope", "piazza", "email_imap", "claude", "discord"]
               if args.what == "all" else [ALIASES.get(args.what, args.what)])
    st = State(cfg.db_path())
    rc = 0
    try:
        for t in targets:
            if t == "claude":
                ok, msg = probe_claude(cfg)
            elif t == "discord":
                ok, msg = probe_discord(cfg)
            else:
                fn = get_probe(t)
                if fn is None:
                    ok, msg = False, "no probe for this source"
                else:
                    try:
                        ok, msg = fn(cfg, cfg.source(t), st.creds_for(t), HttpClient())
                    except Exception as e:  # noqa: BLE001
                        ok, msg = False, f"probe crashed: {e}"
                st.credential_health(t, ok=ok, error=None if ok else msg)
            print(f"{'OK  ' if ok else 'FAIL'} {t:12s} {msg}")
            rc |= 0 if ok else 1
    finally:
        st.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
