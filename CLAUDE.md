# UMD Hub — instructions for Claude sessions in this repo

This is **software**, not study material. The study system (rules, drills, tutor behavior) lives in
`/home/syed/Shared/University of Maryland/` and its `CLAUDE.md` does not apply here. This repo only
*reads* that one (seed calendar, deferred-work backlog); it never writes to it.

## What this is
A self-hosted deadline aggregator for Syed's Fall 2026 classes. `README.md` has the architecture,
`INSTALL.md` the setup, and `/home/syed/.claude/plans/ok-what-i-need-linked-scroll.md` the original
design plan (milestones M0–M5, data model, source specifics, risks).

## Conventions (same as Hardware Parser, its sibling)
- Python 3.12, plain `.venv`, no Docker, no ORM. `config.yaml` = tunables, `secrets.env` = credentials.
- **Never read, print, or commit `secrets.env` or `state.sqlite`.** Never ask for passwords; the user
  puts them in `secrets.env` or pastes cookies on `/credentials`.
- Storage UTC; display/grouping ET via `core/timeutil.py`. Never group by a UTC date.
- Sources are dumb (raw observations); `core/merge.py` is the only place identity/precedence decisions
  live, and it is pure — extend its tests when you touch it.
- One source failing must never kill a pass (`refresh._run_source` isolation). Raise `AuthError` on
  login walls; never on code bugs.
- The LLM extractor (`extract.py`) shells out to `claude -p` on the user's subscription. Never
  `--bare` (that forces API-key auth). Extraction is an optional last stage; it must never block
  structured sources.
- systemd user units live in `systemd/`; paths with spaces are `\x20`-escaped in `ExecStart`.
- Run `.venv/bin/python -m pytest -q` before saying anything works. Parser changes need a fixture.

## Commit style
`<type>(<scope>): <summary>` — types: feat, fix, source, ui, test, docs, chore. Small, reviewable commits.
