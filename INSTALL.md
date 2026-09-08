# Install / setup

## 1. Server (once)

```bash
cd "/home/syed/Shared/Computing Projects/UMD Hub"
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp secrets.env.example secrets.env && chmod 600 secrets.env     # then edit it
.venv/bin/python -m pytest -q
.venv/bin/python -m umdhub.refresh --sources seed,backlog        # populate from the syllabus seed

mkdir -p ~/.config/systemd/user
cp systemd/umdhub-*.service systemd/umdhub-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now umdhub-app.service umdhub-refresh.timer
systemctl --user status umdhub-app.service --no-pager
sudo loginctl enable-linger syed        # already on for this host; needed on a new one
```

Firewall (LAN + WireGuard only — never a port-forward):

```bash
sudo ufw allow from 10.0.0.0/24 to any port 8765 proto tcp comment umdhub
sudo ufw allow from 10.10.0.0/24 to any port 8765 proto tcp comment umdhub-wg
```

Then open `http://10.0.0.200:8765/login?key=<HUB_SHARED_SECRET>` once per device (cookie lasts 90 days).

## 2. Credentials (only you can do these)

| Source | What | Where it goes |
|---|---|---|
| Canvas/ELMS | ELMS → Calendar → **Calendar Feed** (bottom right) → copy the `.ics` URL. Also Account → Notifications → email **ASAP** for Announcements, Due Date, Grading, Course Content. | `/credentials` page |
| Gradescope | Log in (SSO), DevTools → Application → Cookies → `www.gradescope.com` → copy `signed_token` and `_gradescope_session` as `name=value; name=value`. | `/credentials` page |
| Piazza | `PIAZZA_EMAIL` / `PIAZZA_PASSWORD` (its own account, not UMD's) | `secrets.env` |
| Email | terpmail: Google Account → Security → 2-Step Verification → App passwords. If unavailable, make a Gmail filter forwarding course mail to personal Gmail and use an App Password there. Then `python -m umdhub.probe email`. | `secrets.env` (`EMAIL_USER`, `EMAIL_APP_PASSWORD`) |
| Discord | Server Settings → Integrations → Webhooks → New | `secrets.env` (`DISCORD_WEBHOOK_URL`) |
| LAN login | `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` | `secrets.env` (`HUB_SHARED_SECRET`) |

After editing `secrets.env`: `systemctl --user restart umdhub-app.service`.

## 3. Checks

```bash
.venv/bin/python -m umdhub.refresh --dry-run                 # every enabled source, no writes
.venv/bin/python -m umdhub.probe claude                       # claude -p works headless?
systemctl --user list-timers --all | grep umdhub              # 07:00 / 19:00 ET
journalctl --user -u umdhub-refresh -n 50 --no-pager
curl -s http://127.0.0.1:8765/healthz
```

## 4. Phase B — Cloudflare Tunnel + Access (after registering a domain)

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
cloudflared tunnel login                 # browser
cloudflared tunnel create umdhub         # note the UUID
cat > ~/.cloudflared/config.yml <<EOF
tunnel: <UUID>
credentials-file: /home/syed/.cloudflared/<UUID>.json
ingress:
  - hostname: hub.<domain>
    service: http://localhost:8765
  - service: http_status:404
EOF
cloudflared tunnel route dns umdhub hub.<domain>
cloudflared tunnel run umdhub            # test, Ctrl-C
sudo cloudflared --config /home/syed/.cloudflared/config.yml service install
sudo systemctl enable --now cloudflared
```

Zero Trust dashboard: Integrations → Identity providers → **One-time PIN**. Access → Applications →
Self-hosted → `hub.<domain>`, session 1 week, policy Allow · Emails = your addresses. Copy the
application **AUD** into `secrets.env` (`CF_ACCESS_AUD`, `CF_ACCESS_TEAM`), set `web.auth_mode: both`
in `config.yaml`, restart the app. Keep `ufw` as is — no port-forward.
