"""SQLite-backed state (WAL). Item upsert + merge, feed/candidate/grade/credential CRUD,
run log, meta k/v. Mirrors Hardware Parser core/state.py; no ORM."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from . import merge
from .models import SCHEMA_SQL, Candidate, FeedItem, Grade, Item, item_hash
from .timeutil import local_date_str, utcnow, utcnow_iso


class State:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_SQL)
        self._migrate()
        if fresh:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _migrate(self) -> None:
        """Idempotent ALTER TABLE for columns added after the initial schema."""
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(item)")}
        for col, decl in [
            ("changed_at", "TEXT"),
        ]:
            if col not in existing:
                self.conn.execute(f"ALTER TABLE item ADD COLUMN {col} {decl}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ meta
    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def meta_json(self, key: str, default: Any = None) -> Any:
        raw = self.meta_get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def meta_with_prefix(self, prefix: str) -> dict[str, str]:
        """All meta rows whose key starts with prefix, keyed by the remainder."""
        rows = self.conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (prefix + "%",))
        return {r["key"][len(prefix):]: r["value"] for r in rows}

    # ------------------------------------------------------------------ runs
    def start_run(self, source: str, trigger: str, run_group: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO run(run_group, trigger, source, started_at, ok) VALUES(?,?,?,?,0)",
            (run_group, trigger, source, utcnow_iso()),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, ok: bool, fetched: int = 0, new_count: int = 0,
                   updated_count: int = 0, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE run SET finished_at=?, ok=?, fetched=?, new_count=?, updated_count=?, error=? WHERE id=?",
            (utcnow_iso(), 1 if ok else 0, fetched, new_count, updated_count, error, run_id),
        )

    def recent_runs(self, limit: int = 30) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM run ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)))

    def source_health(self, hours: int = 48) -> dict[str, dict]:
        """Per source: last run ok?, last ok time, last error text."""
        since = (utcnow() - timedelta(hours=hours)).replace(microsecond=0).isoformat()
        out: dict[str, dict] = {}
        for r in self.conn.execute(
            "SELECT * FROM run WHERE started_at >= ? ORDER BY started_at DESC", (since,)):
            s = out.setdefault(r["source"], {"last_ok": None, "last_error": None, "last_run": None,
                                             "ok": None, "runs": 0, "failures": 0})
            s["runs"] += 1
            if s["last_run"] is None:
                s["last_run"] = r["started_at"]
                s["ok"] = bool(r["ok"])
            if r["ok"] and s["last_ok"] is None:
                s["last_ok"] = r["started_at"]
            if not r["ok"]:
                s["failures"] += 1
                if s["last_error"] is None:
                    s["last_error"] = r["error"]
        return out

    # ------------------------------------------------------------------ items
    def _match_pool(self, course: str) -> list[dict]:
        """Every item of the course, whatever its status: a source that still lists a cancelled or
        missed item must merge into it, not create a twin. Status itself is never touched by merging."""
        rows = self.conn.execute(
            "SELECT id, course, title_norm, due_date_local, kind, status FROM item WHERE course=?", (course,))
        return [dict(r) for r in rows]

    def upsert_item(self, it: Item) -> tuple[int, bool, bool]:
        """Insert or merge one observation. Returns (item_id, was_new, changed)."""
        now = utcnow_iso()
        norm = merge.normalize_title(it.title, it.course)
        # Always derive from due_at — a caller may reuse an Item after changing its date.
        it.due_date_local = local_date_str(it.due_at) if it.due_at else None
        obs = it.to_obs(norm)
        raw_json = json.dumps(it.raw, default=str) if it.raw else None

        with self.transaction():
            link = self.conn.execute(
                "SELECT item_id FROM item_source WHERE source=? AND source_id=?",
                (it.source, it.source_id)).fetchone()
            was_new = False
            changed = False
            if link:
                item_id = int(link["item_id"])
            else:
                best = merge.match(obs, self._match_pool(it.course))
                if best:
                    item_id = int(best["id"])
                else:
                    item_id = self._insert_item(obs, it.source, now)
                    was_new = True

            if not was_new:
                existing = dict(self.conn.execute("SELECT * FROM item WHERE id=?", (item_id,)).fetchone())
                up = merge.merge_fields(existing, obs, it.source)
                merged = {**existing, **up}
                new_hash = item_hash(merged["course"], merged["kind"], merged["title_norm"],
                                     merged.get("due_at"), merged.get("url"))
                if new_hash != existing["hash"]:
                    up["prev_hash"] = existing["hash"]
                    up["hash"] = new_hash
                    up["changed_at"] = now
                    changed = True
                up["last_seen"] = now
                if len(up) > 1 or changed:
                    up["updated_at"] = now
                sets = ", ".join(f"{k}=?" for k in up)
                self.conn.execute(f"UPDATE item SET {sets} WHERE id=?", (*up.values(), item_id))

            self.conn.execute(
                "INSERT INTO item_source(item_id, source, source_id, url, raw, first_seen, last_seen) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(source, source_id) DO UPDATE SET "
                "item_id=excluded.item_id, url=COALESCE(excluded.url, url), raw=COALESCE(excluded.raw, raw), "
                "last_seen=excluded.last_seen",
                (item_id, it.source, it.source_id, it.url, raw_json, now, now),
            )
        return item_id, was_new, changed

    def _insert_item(self, obs: dict, source: str, now: str) -> int:
        h = item_hash(obs["course"], obs["kind"], obs["title_norm"], obs.get("due_at"), obs.get("url"))
        cur = self.conn.execute(
            "INSERT INTO item(course, kind, title, title_norm, due_at, due_date_local, all_day, release_at, "
            "late_due_at, url, status, points, max_points, weight_note, submission_status, score, "
            "primary_source, first_seen, last_seen, updated_at, hash, notes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?)",
            (obs["course"], obs["kind"], obs["title"], obs["title_norm"], obs.get("due_at"),
             obs.get("due_date_local"), obs.get("all_day", 0), obs.get("release_at"), obs.get("late_due_at"),
             obs.get("url"), obs.get("points"), obs.get("max_points"), obs.get("weight_note"),
             obs.get("submission_status"), obs.get("score"), source, now, now, now, h, obs.get("notes")),
        )
        return int(cur.lastrowid)

    def prune_seed_orphans(self, seen_source_ids: set[str]) -> int:
        """After a seed run: drop seed links the seed file no longer contains, and delete items that
        were seed-only, untouched (still open, seed-primary) and now unlinked. Returns items deleted."""
        rows = self.conn.execute("SELECT item_id, source_id FROM item_source WHERE source='seed'").fetchall()
        stale = [r for r in rows if r["source_id"] not in seen_source_ids]
        deleted = 0
        if not stale:
            return 0
        with self.transaction():
            for r in stale:
                self.conn.execute("DELETE FROM item_source WHERE source='seed' AND source_id=?", (r["source_id"],))
                left = self.conn.execute("SELECT COUNT(*) FROM item_source WHERE item_id=?",
                                         (r["item_id"],)).fetchone()[0]
                if left == 0:
                    cur = self.conn.execute(
                        "DELETE FROM item WHERE id=? AND status='open' AND primary_source='seed'", (r["item_id"],))
                    deleted += cur.rowcount
        return deleted

    def merge_items(self, keep_id: int, drop_id: int) -> None:
        """Fold item `drop` into item `keep`: move source links/grades/candidates, keep the more
        user-touched status, fill empty notes/weight from the dropped row, delete it."""
        keep = dict(self.item(keep_id))
        drop = dict(self.item(drop_id))
        with self.transaction():
            self.conn.execute("UPDATE item_source SET item_id=? WHERE item_id=?", (keep_id, drop_id))
            self.conn.execute("UPDATE grade SET item_id=? WHERE item_id=?", (keep_id, drop_id))
            self.conn.execute("UPDATE candidate SET matched_item_id=? WHERE matched_item_id=?", (keep_id, drop_id))
            up: dict = {}
            for f in ("weight_note", "notes", "url", "release_at", "late_due_at", "submission_status", "score"):
                if not keep.get(f) and drop.get(f):
                    up[f] = drop[f]
            if keep["status"] == "open" and drop["status"] != "open":
                up["status"] = drop["status"]
                up["snoozed_until"] = drop.get("snoozed_until")
            if keep["title"] != drop["title"]:
                note = f"also: {drop['title']}"
                notes = up.get("notes") or keep.get("notes") or ""
                if note not in notes:
                    up["notes"] = (notes + "\n" + note).strip()
            if up:
                up["updated_at"] = utcnow_iso()
                sets = ", ".join(f"{k}=?" for k in up)
                self.conn.execute(f"UPDATE item SET {sets} WHERE id=?", (*up.values(), keep_id))
            self.conn.execute("DELETE FROM item WHERE id=?", (drop_id,))

    def add_manual_item(self, course: str, kind: str, title: str, due_at: str | None,
                        all_day: bool, notes: str | None = None, url: str | None = None) -> int:
        """User-entered item. Bypasses matching — the user meant a new row."""
        now = utcnow_iso()
        norm = merge.normalize_title(title, course)
        obs = {"course": course, "kind": kind, "title": title, "title_norm": norm, "due_at": due_at,
               "due_date_local": local_date_str(due_at) if due_at else None,
               "all_day": 1 if all_day else 0, "notes": notes, "url": url}
        with self.transaction():
            item_id = self._insert_item(obs, "manual", now)
            self.conn.execute(
                "INSERT INTO item_source(item_id, source, source_id, url, first_seen, last_seen) VALUES(?,?,?,?,?,?)",
                (item_id, "manual", f"manual:{uuid.uuid4().hex[:12]}", url, now, now))
        return item_id

    def item(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM item WHERE id=?", (item_id,)).fetchone()

    def item_sources(self, item_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM item_source WHERE item_id=? ORDER BY first_seen", (item_id,)))

    def update_item_fields(self, item_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utcnow_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE item SET {sets} WHERE id=?", (*fields.values(), item_id))

    def set_status(self, item_id: int, status: str, snoozed_until: str | None = None) -> None:
        self.conn.execute(
            "UPDATE item SET status=?, snoozed_until=?, updated_at=? WHERE id=?",
            (status, snoozed_until if status == "snoozed" else None, utcnow_iso(), item_id))

    def wake_snoozed(self) -> int:
        cur = self.conn.execute(
            "UPDATE item SET status='open', snoozed_until=NULL, updated_at=? "
            "WHERE status='snoozed' AND snoozed_until IS NOT NULL AND snoozed_until <= ?",
            (utcnow_iso(), utcnow_iso()))
        return cur.rowcount

    def items_between(self, start_date: str, end_date: str, statuses: tuple[str, ...] = ("open",),
                      course: str | None = None) -> list[sqlite3.Row]:
        q = ("SELECT * FROM item WHERE due_date_local >= ? AND due_date_local <= ? "
             f"AND status IN ({','.join('?' * len(statuses))})")
        args: list[Any] = [start_date, end_date, *statuses]
        if course:
            q += " AND course=?"
            args.append(course)
        q += " ORDER BY due_date_local, all_day, due_at, course, title"
        return list(self.conn.execute(q, args))

    def overdue(self, now_iso: str, today_local: str) -> list[sqlite3.Row]:
        """Open items whose deadline passed: timed → due_at < now; all-day → date < today."""
        return list(self.conn.execute(
            "SELECT * FROM item WHERE status='open' AND due_at IS NOT NULL AND "
            "((all_day=0 AND due_at < ?) OR (all_day=1 AND due_date_local < ?)) "
            "ORDER BY due_at", (now_iso, today_local)))

    def due_between(self, start_iso: str, end_iso: str) -> list[sqlite3.Row]:
        """Open items with a timed/all-day deadline in [start, end] (UTC ISO)."""
        return list(self.conn.execute(
            "SELECT * FROM item WHERE status='open' AND due_at >= ? AND due_at <= ? ORDER BY due_at",
            (start_iso, end_iso)))

    def match_pool(self, course: str) -> list[dict]:
        return self._match_pool(course)

    def undated(self, course: str | None = None, statuses: tuple[str, ...] = ("open",)) -> list[sqlite3.Row]:
        q = f"SELECT * FROM item WHERE due_at IS NULL AND status IN ({','.join('?' * len(statuses))})"
        args: list[Any] = [*statuses]
        if course:
            q += " AND course=?"
            args.append(course)
        q += " ORDER BY course, kind, title"
        return list(self.conn.execute(q, args))

    def items_for_course(self, course: str, statuses: tuple[str, ...] = ("open", "snoozed"),
                         after_date: str | None = None) -> list[sqlite3.Row]:
        q = f"SELECT * FROM item WHERE course=? AND status IN ({','.join('?' * len(statuses))})"
        args: list[Any] = [course, *statuses]
        if after_date:
            q += " AND (due_date_local IS NULL OR due_date_local >= ?)"
            args.append(after_date)
        q += " ORDER BY due_date_local IS NULL, due_date_local, all_day, due_at, title"
        return list(self.conn.execute(q, args))

    def snoozed(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM item WHERE status='snoozed' ORDER BY snoozed_until"))

    def recently_changed(self, since_iso: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM item WHERE changed_at IS NOT NULL AND changed_at > ? AND first_seen <= ? "
            "ORDER BY changed_at DESC", (since_iso, since_iso)))

    def new_since(self, since_iso: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM item WHERE first_seen > ? AND primary_source != 'seed' "
            "ORDER BY due_date_local IS NULL, due_date_local", (since_iso,)))

    def count_items(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM item").fetchone()[0])

    # ------------------------------------------------------------------ feed
    def upsert_feed_item(self, f: FeedItem) -> tuple[int, bool]:
        """Insert a message, or refresh one whose content changed (a Piazza post that got an
        instructor answer, an edited announcement). A changed body re-queues extraction and
        marks the item unread again — the new text is what carries deadline changes.
        Returns (id, was_new)."""
        now = utcnow_iso()
        body = (f.body or "")[:20000]
        row = self.conn.execute(
            "SELECT id, body, subject FROM feed_item WHERE source=? AND source_id=?",
            (f.source, f.source_id)).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO feed_item(source, source_id, course, author, is_instructor, subject, body, url, "
                "posted_at, fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f.source, f.source_id, f.course, f.author, 1 if f.is_instructor else 0, f.subject[:500],
                 body, f.url, f.posted_at, now))
            return int(cur.lastrowid), True
        if body != (row["body"] or "") or f.subject[:500] != row["subject"]:
            self.conn.execute(
                "UPDATE feed_item SET body=?, subject=?, author=COALESCE(?, author), "
                "is_instructor=MAX(is_instructor, ?), course=COALESCE(course, ?), url=COALESCE(?, url), "
                "fetched_at=?, extract_status='pending', extracted_at=NULL, read_at=NULL WHERE id=?",
                (body, f.subject[:500], f.author, 1 if f.is_instructor else 0, f.course, f.url, now, row["id"]))
        return int(row["id"]), False

    def feed(self, *, unread_only: bool = False, source: str | None = None, course: str | None = None,
             limit: int = 100) -> list[sqlite3.Row]:
        q = "SELECT * FROM feed_item WHERE 1=1"
        args: list[Any] = []
        if unread_only:
            q += " AND read_at IS NULL"
        if source:
            q += " AND source=?"
            args.append(source)
        if course:
            q += " AND course=?"
            args.append(course)
        q += " ORDER BY COALESCE(posted_at, fetched_at) DESC LIMIT ?"
        args.append(limit)
        return list(self.conn.execute(q, args))

    def feed_item(self, fid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM feed_item WHERE id=?", (fid,)).fetchone()

    def unread_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM feed_item WHERE read_at IS NULL").fetchone()[0])

    def mark_read(self, fid: int | None = None) -> None:
        if fid is None:
            self.conn.execute("UPDATE feed_item SET read_at=? WHERE read_at IS NULL", (utcnow_iso(),))
        else:
            self.conn.execute("UPDATE feed_item SET read_at=? WHERE id=? AND read_at IS NULL",
                              (utcnow_iso(), fid))

    def pending_extraction(self, sources: list[str], limit: int) -> list[sqlite3.Row]:
        if not sources:
            return []
        return list(self.conn.execute(
            f"SELECT * FROM feed_item WHERE extract_status='pending' AND source IN "
            f"({','.join('?' * len(sources))}) ORDER BY COALESCE(posted_at, fetched_at) LIMIT ?",
            (*sources, limit)))

    def set_extract_status(self, ids: list[int], status: str) -> None:
        if not ids:
            return
        self.conn.execute(
            f"UPDATE feed_item SET extract_status=?, extracted_at=? WHERE id IN ({','.join('?' * len(ids))})",
            (status, utcnow_iso(), *ids))

    # ------------------------------------------------------------------ candidates
    def add_candidate(self, c: Candidate) -> int | None:
        norm = merge.normalize_title(c.title, c.course)
        c.due_date_local = local_date_str(c.due_at) if c.due_at else None
        try:
            cur = self.conn.execute(
                "INSERT INTO candidate(feed_item_id, course, kind, title, title_norm, due_at, due_date_local, "
                "all_day, confidence, quote, action, matched_item_id, state, batch_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (c.feed_item_id, c.course, c.kind, c.title, norm, c.due_at, c.due_date_local,
                 1 if c.all_day else 0, c.confidence, c.quote, c.action, c.matched_item_id, c.batch_id,
                 utcnow_iso()))
        except sqlite3.IntegrityError:
            return None
        return int(cur.lastrowid)

    def candidates(self, state: str = "pending") -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT c.*, f.subject AS feed_subject, f.source AS feed_source, f.url AS feed_url, "
            "f.posted_at AS feed_posted_at, i.title AS matched_title, i.due_at AS matched_due_at, "
            "i.all_day AS matched_all_day FROM candidate c "
            "LEFT JOIN feed_item f ON f.id = c.feed_item_id "
            "LEFT JOIN item i ON i.id = c.matched_item_id "
            "WHERE c.state=? ORDER BY c.created_at DESC", (state,)))

    def candidate(self, cid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM candidate WHERE id=?", (cid,)).fetchone()

    def decide_candidate(self, cid: int, state: str, matched_item_id: int | None = None) -> None:
        self.conn.execute(
            "UPDATE candidate SET state=?, decided_at=?, matched_item_id=COALESCE(?, matched_item_id) WHERE id=?",
            (state, utcnow_iso(), matched_item_id, cid))

    def pending_candidate_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM candidate WHERE state='pending'").fetchone()[0])

    # ------------------------------------------------------------------ grades
    def upsert_grade(self, g: Grade) -> None:
        self.conn.execute(
            "INSERT INTO grade(course, item_id, title, score, max_score, source, source_id, recorded_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source, source_id) DO UPDATE SET score=excluded.score, "
            "max_score=excluded.max_score, item_id=COALESCE(excluded.item_id, grade.item_id), "
            "recorded_at=excluded.recorded_at",
            (g.course, g.item_id, g.title, g.score, g.max_score, g.source, g.source_id, utcnow_iso()))

    def grades_for_course(self, course: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM grade WHERE course=? ORDER BY recorded_at DESC", (course,)))

    # ------------------------------------------------------------------ credentials
    def credentials(self) -> dict[tuple[str, str], sqlite3.Row]:
        return {(r["source"], r["name"]): r for r in self.conn.execute("SELECT * FROM credential")}

    def credential_value(self, source: str, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM credential WHERE source=? AND name=?", (source, name)).fetchone()
        return row["value"] if row else None

    def creds_for(self, source: str) -> dict[str, str]:
        return {r["name"]: r["value"] for r in self.conn.execute(
            "SELECT name, value FROM credential WHERE source=?", (source,))}

    def set_credential(self, source: str, name: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO credential(source, name, value, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(source, name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
            "last_error=NULL",
            (source, name, value.strip(), utcnow_iso()))

    def delete_credential(self, source: str, name: str) -> None:
        self.conn.execute("DELETE FROM credential WHERE source=? AND name=?", (source, name))

    def credential_health(self, source: str, *, ok: bool, error: str | None = None) -> None:
        """Stamp every credential row of a source with the outcome of its last use/probe."""
        now = utcnow_iso()
        if ok:
            self.conn.execute(
                "UPDATE credential SET last_ok_at=?, last_checked_at=?, last_error=NULL WHERE source=?",
                (now, now, source))
        else:
            self.conn.execute(
                "UPDATE credential SET last_checked_at=?, last_error=? WHERE source=?",
                (now, (error or "failed")[:500], source))

    # ------------------------------------------------------------------ site snapshots
    def site_snapshot(self, course: str, url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM site_snapshot WHERE course=? AND url=?", (course, url)).fetchone()

    def save_site_snapshot(self, course: str, url: str, text: str, h: str) -> None:
        self.conn.execute(
            "INSERT INTO site_snapshot(course, url, text, hash, fetched_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(course, url) DO UPDATE SET text=excluded.text, hash=excluded.hash, "
            "fetched_at=excluded.fetched_at",
            (course, url, text, h, utcnow_iso()))
