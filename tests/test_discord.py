from datetime import timedelta

from umdhub.core import timeutil as tu
from umdhub.notify import discord


class _Resp:
    def __init__(self, code=204, text=""):
        self.status_code, self.text = code, text


def test_build_digest_sections(cfg, seeded, monkeypatch):
    cfg.notify.enabled = True
    soon = tu.to_utc_iso(tu.utcnow() + timedelta(hours=3))
    seeded.add_manual_item("CMSC351", "assignment", "Due very soon", soon, False)
    past, all_day = tu.from_local("2026-01-02", None)
    seeded.add_manual_item("MATH246", "quiz", "Ancient quiz", past, all_day)
    seeded.set_credential("gradescope", "cookie", "x=y")
    seeded.credential_health("gradescope", ok=False, error="cookie rejected")
    embeds, substantive = discord.build_digest(cfg, seeded, {"seed": {"ok": True, "error": None},
                                                             "piazza": {"ok": False, "error": "login failed"}})
    titles = " | ".join(e["title"] for e in embeds)
    assert substantive
    assert "Due within" in titles and "Overdue" in titles and "Needs attention" in titles
    attention = next(e for e in embeds if "attention" in e["title"])
    assert "cookie rejected" in attention["description"] and "login failed" in attention["description"]


def test_send_digest_posts_and_advances_marker(cfg, seeded, monkeypatch):
    calls = []
    monkeypatch.setattr(discord.requests, "post", lambda url, json, timeout: calls.append((url, json)) or _Resp())
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    soon = tu.to_utc_iso(tu.utcnow() + timedelta(hours=2))
    seeded.add_manual_item("CMSC330", "quiz", "Soon quiz", soon, False)
    out = discord.send_digest(cfg, seeded, {})
    assert out["sent"] is True and out["embeds"] >= 1
    assert calls and calls[0][1]["username"] == cfg.notify.username
    assert seeded.meta_get("last_notified_at")


def test_send_digest_skips_without_webhook(cfg, seeded, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert discord.send_digest(cfg, seeded, {}) == {"skipped": "no webhook configured"}
