"""Maintenance: merge item rows that the matcher now recognizes as the same thing.

    python -m umdhub.dedupe            # report pairs, change nothing
    python -m umdhub.dedupe --apply    # fold each duplicate into its higher-priority twin

Needed after the matcher improves (retroactive), or after a source renames something. Live
runs never re-match an item that already has a source link, by design — stability beats cleverness —
so this is the escape hatch.
"""
from __future__ import annotations

import argparse
import sys

from .core import merge
from .core.config import load_config
from .core.state import State


def renormalize(state: State) -> int:
    """Recompute stored title_norm with the current normalizer (it evolves; rows don't)."""
    changed = 0
    for r in state.conn.execute("SELECT id, course, title, title_norm FROM item").fetchall():
        norm = merge.normalize_title(r["title"], r["course"])
        if norm != r["title_norm"]:
            state.conn.execute("UPDATE item SET title_norm=? WHERE id=?", (norm, r["id"]))
            changed += 1
    return changed


def find_pairs(state: State, courses: list[str]) -> list[tuple[int, int, str]]:
    """[(keep_id, drop_id, reason)] — keep = higher source priority, then lower id."""
    pairs: list[tuple[int, int, str]] = []
    gone: set[int] = set()
    for course in courses:
        rows = [dict(r) for r in state.conn.execute(
            "SELECT id, course, title, title_norm, due_date_local, primary_source, status FROM item "
            "WHERE course=? AND status != 'cancelled' ORDER BY id", (course,))]
        for i, a in enumerate(rows):
            if a["id"] in gone:
                continue
            for b in rows[i + 1:]:
                if b["id"] in gone or a["id"] in gone:
                    continue
                if not merge.similar(a["title_norm"], b["title_norm"]):
                    continue
                if not merge.dates_close(a["due_date_local"], b["due_date_local"]):
                    continue
                pa = merge.SOURCE_PRIORITY.get(a["primary_source"], 0)
                pb = merge.SOURCE_PRIORITY.get(b["primary_source"], 0)
                keep, drop = (a, b) if pa >= pb else (b, a)
                pairs.append((keep["id"], drop["id"],
                              f"{course}: keep #{keep['id']} '{keep['title']}' [{keep['primary_source']}]"
                              f" ← drop #{drop['id']} '{drop['title']}' [{drop['primary_source']}]"))
                gone.add(drop["id"])
                if drop is a:
                    break
    return pairs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="merge duplicate items")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--config", default=None)
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    st = State(cfg.db_path())
    try:
        n = renormalize(st)
        if n:
            print(f"renormalized {n} title(s)")
        pairs = find_pairs(st, cfg.course_codes())
        for keep, drop, reason in pairs:
            print(("MERGE " if args.apply else "would merge ") + reason)
            if args.apply:
                st.merge_items(keep, drop)
        print(f"{len(pairs)} pair(s){' merged' if args.apply else ' found (dry run; add --apply)'}; "
              f"{st.count_items()} items now")
    finally:
        st.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
