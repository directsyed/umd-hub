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

## 1b. Headless Claude auth for the extractor (step 1, done right)

`claude auth status` can say "logged in" while `claude -p` still fails with *OAuth access token has
expired*: the desktop app refreshes its own token and never writes it back to `~/.claude/.credentials.json`.
A systemd service needs a token that doesn't depend on that file:

```bash
claude setup-token          # in an SSH terminal on syedlab; opens a browser URL, prints a long-lived token
nano "/home/syed/Shared/Computing Projects/UMD Hub/secrets.env"   # paste it after CLAUDE_CODE_OAUTH_TOKEN=
cd "/home/syed/Shared/Computing Projects/UMD Hub" && .venv/bin/python -m umdhub.probe claude   # probe loads secrets.env itself
```

The probe must print `OK claude replied 'pong'`. The refresh service loads `secrets.env`, so it picks the
token up on its next run — nothing to restart.

## 2. First login (step 3)

`secrets.env` already holds a generated `HUB_SHARED_SECRET`. Print it, then open the login URL once
on each device; the cookie lasts 90 days.

```bash
grep HUB_SHARED_SECRET "/home/syed/Shared/Computing Projects/UMD Hub/secrets.env"
```

- **Desktop / laptop on home Wi-Fi, or laptop over WireGuard:** `http://10.0.0.200:8765/login?key=PASTE-THE-SECRET`
- **Phone on home Wi-Fi:** same URL (it's in `10.0.0.0/24`). Off-network phone access arrives with Phase B.
- If you get **"forbidden"**: your device isn't in an allowed CIDR (check `web.allowed_cidrs` in `config.yaml`,
  or the ufw rule). If you get the login form with **"wrong key"**: the secret was mistyped/URL-encoded — paste it
  into the form instead.

## 3. Credentials (step 4 — pasted on the Hub's `/credentials` page)

Nothing here is a password. Both values are revocable by logging out / regenerating.

**Canvas calendar feed** (structured due dates for every ELMS course, no token needed)
1. Log in to `https://umd.instructure.com` → left rail **Calendar**.
2. Bottom-right of the calendar page: **"Calendar Feed"** link → a dialog shows a URL starting
   `https://umd.instructure.com/feeds/calendars/user_…ics` → **Copy**.
3. Hub → **Keys** → *Canvas calendar feed URL* → paste → **save** → **test** (should report the event count per course).
4. While in ELMS: **Account → Notifications**. Set these to *Notify immediately* (the envelope icon) so they arrive by
   email and the Hub's email source picks them up: *Due Date*, *Grading Policies*, *Course Content*, *Announcement*,
   *Grading*, *Discussion*. (Canvas announcements/grade changes are not in the .ics feed.)

**Gradescope cookies** (assignments, due dates, submission status, scores)
1. Log in at `https://www.gradescope.com` with School Credentials as usual, land on your dashboard.
2. Open DevTools: **F12** (or right-click → Inspect).
   - **Chrome / Edge / Brave:** *Application* tab → left tree **Storage → Cookies → https://www.gradescope.com**.
   - **Firefox:** *Storage* tab → **Cookies → https://www.gradescope.com**.
3. Find the rows named **`signed_token`** and **`_gradescope_session`**. Double-click each *Value* cell, copy.
4. Hub → **Keys** → *Gradescope cookies* → paste as one line:
   `signed_token=PASTE1; _gradescope_session=PASTE2` → **save** → **test** (should list your Fall 2026 courses and
   which Hub course each maps to). CMSC351 will be missing until the roster issue is fixed — expected.
5. When the pill turns red weeks from now, repeat steps 1–4. Logging out of Gradescope in that browser invalidates
   the cookie, so use a browser you stay logged into.

## 4. secrets.env (step 5)

```bash
nano "/home/syed/Shared/Computing Projects/UMD Hub/secrets.env"
```

| Key | How to get it |
|---|---|
| `PIAZZA_EMAIL` / `PIAZZA_PASSWORD` | Your Piazza account's own email + password (not UMD's). If Piazza login throttles you, leave these blank and paste the `session_id` cookie from piazza.com on the Keys page instead. |
| `EMAIL_USER` / `EMAIL_APP_PASSWORD` | **Try terpmail first:** `myaccount.google.com` while signed in as `…@terpmail.umd.edu` → **Security** → *2-Step Verification* must be **On** (turn it on if not) → then `myaccount.google.com/apppasswords` → name it `umd-hub` → **Create** → copy the 16-character password (spaces don't matter). `EMAIL_USER` is the full address. **If the App passwords page says it isn't available for your account**, UMD's admin blocked it: instead, in terpmail Gmail → Settings → *Filters and Blocked Addresses* → create a filter `from:(umd.edu OR instructure.com OR gradescope.com OR piazza.com)` → *Forward it to* your personal Gmail, then make the App Password on the personal Gmail and use that address here. |
| `EMAIL_IMAP_HOST` | leave `imap.gmail.com` |
| `DISCORD_WEBHOOK_URL` | Discord → your server → **Server Settings → Integrations → Webhooks → New Webhook** → pick a channel (make a `#umd-hub` one) → **Copy Webhook URL**. |
| `HUB_SHARED_SECRET` | already set |
| `CF_ACCESS_*` | leave blank until Phase B |

Save, then restart and probe each source — every probe prints exactly what's wrong if something is:

```bash
cd "/home/syed/Shared/Computing Projects/UMD Hub"
systemctl --user restart umdhub-app.service
.venv/bin/python -m umdhub.probe all
```

## 5. First real refresh (step 6)

```bash
.venv/bin/python -m umdhub.refresh --trigger manual
```

Then open **Status** on the Hub: every enabled source should show `ok`. Expect the Discord digest within a few
seconds (first one lists everything due in 24 h plus any credential/source problems). Open **Feed** to see the
emails/Piazza posts that came in, and **Tray** for any deadlines the extractor pulled from them — confirm or reject
each; nothing reaches the calendar without you.

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
