# UMD Hub

One phone-friendly page for everything due across five classes. Pulls from the syllabus seed,
Canvas (calendar feed), Gradescope, Piazza, course email, and course websites; refreshes at
07:00 and 19:00 ET plus on demand; posts a Discord digest; lets you mark things done, snooze,
add items by hand, and confirm deadlines the extractor pulled out of free text.

```
seed/fall-2026.yaml ─┐                                          ┌─ Discord digest
Canvas .ics feed ────┤                                          │
Gradescope (cookie) ─┤  umdhub.refresh (oneshot, timer/button)  │
Piazza (login) ──────┼──► ─► merge.py ─► state.sqlite ──────────┴─► umdhub.app (FastAPI :8765)
Email IMAP ──────────┤        └─ extract.py (claude -p) ─► candidates tray
Course sites ────────┤
study-repo backlog ──┘
```

- **Stack:** Python 3.12, FastAPI + Jinja2, SQLite (WAL), requests + bs4, systemd user units. No Docker.
- **Layout:** `umdhub/core` (config, models, state, merge, timeutil), `umdhub/sources/*` (one adapter per
  source), `umdhub/extract.py`, `umdhub/notify/discord.py`, `umdhub/app.py` + `web/`.
- **Rules of the road:** storage is UTC, everything displayed/grouped in America/New_York
  (`due_date_local`). Sources produce raw observations; `core/merge.py` decides identity and precedence;
  user status (done/snoozed) is never overwritten by a source. One source crashing never kills a pass.
- **Secrets:** `secrets.env` (gitignored) for passwords/webhook; pasted cookies live in `state.sqlite`.

See `INSTALL.md` for setup and `CLAUDE.md` for how to work on this repo.

```bash
.venv/bin/python -m pytest -q                      # tests
.venv/bin/python -m umdhub.refresh --dry-run       # fetch + parse, no writes
.venv/bin/python -m umdhub.refresh                 # one real pass
.venv/bin/python -m umdhub.app                     # serve on :8765
```
