from datetime import date

from umdhub.core.models import Item
from umdhub.core import timeutil as tu
from umdhub.sources.seed import load_seed


def test_seed_loads_and_is_idempotent(cfg, state):
    items = load_seed(cfg.seed_path(), set(cfg.course_codes()))
    assert len(items) > 40
    assert {i.course for i in items} == set(cfg.course_codes())
    for it in items:
        _, was_new, _ = state.upsert_item(it)
        assert was_new
    n = state.count_items()
    for it in items:
        _, was_new, changed = state.upsert_item(it)
        assert not was_new and not changed
    assert state.count_items() == n


def test_sep_11_quadruple_deadline(seeded):
    rows = seeded.items_between("2026-09-11", "2026-09-11")
    assert {r["course"] for r in rows} == {"MATH246", "CMSC351", "CMSC330", "ENGL390"}


def test_live_gradescope_merges_into_expected_lecture_quiz(seeded):
    before = seeded.count_items()
    due, _ = tu.from_local("2026-09-14", "23:59")
    live = Item(course="CMSC330", kind="quiz", title="Lecture Quiz 2", source="gradescope",
                source_id="gs:123", due_at=due, url="https://www.gradescope.com/courses/1/assignments/123",
                submission_status="No Submission")
    item_id, was_new, changed = seeded.upsert_item(live)
    assert not was_new and changed
    assert seeded.count_items() == before
    row = seeded.item(item_id)
    assert row["kind"] == "quiz"
    assert row["title"] == "Lecture Quiz 2"
    assert row["primary_source"] == "gradescope"
    assert row["due_date_local"] == "2026-09-14"
    assert row["submission_status"] == "No Submission"
    assert "seed: Lecture quiz (weekly)" in (row["notes"] or "")
    # second observation with a moved date → changed again, still same row
    due2, _ = tu.from_local("2026-09-15", "23:59")
    live.due_at = due2
    item_id2, was_new2, changed2 = seeded.upsert_item(live)
    assert item_id2 == item_id and not was_new2 and changed2
    assert seeded.item(item_id)["due_date_local"] == "2026-09-15"


def test_manual_item_status_and_overdue(seeded):
    past, all_day = tu.from_local("2026-01-05", None)
    iid = seeded.add_manual_item("CMSC351", "assignment", "Old thing", past, all_day)
    over = seeded.overdue(tu.utcnow_iso(), date(2026, 9, 8).isoformat())
    assert any(r["id"] == iid for r in over)
    seeded.set_status(iid, "done")
    assert seeded.item(iid)["status"] == "done"
    assert not any(r["id"] == iid for r in seeded.overdue(tu.utcnow_iso(), "2026-09-08"))


def test_snooze_and_wake(seeded):
    iid = seeded.add_manual_item("MATH246", "assignment", "Snoozable", None, False)
    seeded.set_status(iid, "snoozed", "2020-01-01T00:00:00+00:00")
    assert seeded.item(iid)["status"] == "snoozed"
    assert seeded.wake_snoozed() == 1
    assert seeded.item(iid)["status"] == "open"


def test_prune_seed_orphans(cfg, seeded):
    items = load_seed(cfg.seed_path(), set(cfg.course_codes()))
    keep = {i.source_id for i in items}
    victim = next(i for i in items if i.course == "CMSC330" and i.title == "Quiz 5")
    before = seeded.count_items()
    keep.discard(victim.source_id)
    assert seeded.prune_seed_orphans(keep) == 1
    assert seeded.count_items() == before - 1
    # an orphan the user already touched (done) survives
    victim2 = next(i for i in items if i.course == "CMSC330" and i.title == "Quiz 4")
    row = seeded.conn.execute("SELECT item_id FROM item_source WHERE source='seed' AND source_id=?",
                              (victim2.source_id,)).fetchone()
    seeded.set_status(int(row["item_id"]), "done")
    keep.discard(victim2.source_id)
    assert seeded.prune_seed_orphans(keep) == 0
    assert seeded.item(int(row["item_id"]))["status"] == "done"


def test_merge_items_folds_duplicate(seeded):
    due, _ = tu.from_local("2026-09-07", "23:59")
    dup = seeded.add_manual_item("CMSC330", "quiz", "Lecture Quiz 1 - Syllabus", due, False, notes="from gs")
    placeholder = seeded.items_between("2026-09-07", "2026-09-07", course="CMSC330")
    keep = next(r for r in placeholder if r["primary_source"] == "seed")
    seeded.set_status(dup, "done")
    seeded.merge_items(int(keep["id"]), dup)
    assert seeded.item(dup) is None
    k = seeded.item(int(keep["id"]))
    assert k["status"] == "done"                      # user-touched status carried over
    assert "also: Lecture Quiz 1 - Syllabus" in k["notes"]
    assert len(seeded.item_sources(int(keep["id"]))) == 2


def test_cancelled_and_missed_items_still_absorb_live_observations(seeded):
    due, _ = tu.from_local("2026-09-09", "10:00")
    gs = Item(course="CMSC351", kind="quiz", title="Quiz 3 (NP Intro)", source="gradescope", source_id="gs:q3", due_at=due)
    iid, was_new, _ = seeded.upsert_item(gs)
    assert was_new
    seeded.set_status(iid, "cancelled")
    before = seeded.count_items()
    # a second source sees the same quiz → must merge into the cancelled row, not create a twin
    other = Item(course="CMSC351", kind="quiz", title="Quiz 3", source="course_site", source_id="cs:q3", due_at=due)
    iid2, was_new2, _ = seeded.upsert_item(other)
    assert iid2 == iid and not was_new2 and seeded.count_items() == before
    assert seeded.item(iid)["status"] == "cancelled"        # merging never touches status
    seeded.set_status(iid, "missed")
    iid3, was_new3, _ = seeded.upsert_item(gs)
    assert iid3 == iid and not was_new3 and seeded.item(iid)["status"] == "missed"


def test_credentials_roundtrip(state):
    state.set_credential("gradescope", "cookie", "  signed_token=abc; _gradescope_session=def  ")
    assert state.creds_for("gradescope") == {"cookie": "signed_token=abc; _gradescope_session=def"}
    state.credential_health("gradescope", ok=False, error="login wall")
    row = state.credentials()[("gradescope", "cookie")]
    assert row["last_error"] == "login wall"
    state.credential_health("gradescope", ok=True)
    assert state.credentials()[("gradescope", "cookie")]["last_error"] is None
    state.delete_credential("gradescope", "cookie")
    assert state.creds_for("gradescope") == {}


def test_runs_and_health(state):
    rid = state.start_run("seed", "cli", "grp1")
    state.finish_run(rid, ok=True, fetched=3)
    rid2 = state.start_run("gradescope", "cli", "grp1")
    state.finish_run(rid2, ok=False, error="boom")
    h = state.source_health(48)
    assert h["seed"]["ok"] is True and h["gradescope"]["ok"] is False
    assert h["gradescope"]["last_error"] == "boom"
