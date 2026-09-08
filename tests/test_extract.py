import json
from pathlib import Path

import pytest

from tests.conftest import FIXTURES
from umdhub import extract
from umdhub.core.models import FeedItem

FAKE = Path(__file__).parent / "fakebin" / "claude"


@pytest.fixture
def xcfg(cfg, monkeypatch):
    cfg.extract.enabled = True
    cfg.extract.claude_bin = str(FAKE)
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", str(FIXTURES / "claude_ok.json"))
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    return cfg


def _feed(state, body="Homework 1 is attached. It is due Friday, September 18 at 11:59 pm. "
                       "Remember the first midterm is September 29."):
    fid, _ = state.upsert_feed_item(FeedItem(source="email", source_id="<m1@umd.edu>", course="BCHM461",
                                             subject="BCHM461 Homework 1 posted", body=body,
                                             author="yxliu@umd.edu", is_instructor=True,
                                             posted_at="2026-09-08T14:15:00+00:00"))
    return fid


def test_schema_and_prompt_are_config_driven(xcfg):
    s = extract.schema(xcfg)
    assert s["properties"]["candidates"]["items"]["properties"]["course"]["enum"][-1] == "UNKNOWN"
    assert "BCHM461" in s["properties"]["candidates"]["items"]["properties"]["course"]["enum"]
    sp = extract.system_prompt(xcfg)
    assert "BCHM461" in sp and "yxliu@umd.edu" in sp and '{"candidates": []}' in sp


def test_run_creates_tray_candidate_and_auto_merges_known_exam(xcfg, seeded):
    fid = _feed(seeded)
    stats = extract.run(xcfg, seeded)
    assert stats["batches"] == 1 and stats["feed_items"] == 1 and stats["failed"] == 0
    assert stats["candidates"] == 1 and stats["auto_merged"] == 1
    pending = seeded.candidates("pending")
    assert len(pending) == 1
    c = pending[0]
    assert c["title"] == "Homework 1" and c["course"] == "BCHM461" and c["action"] == "new"
    assert c["due_at"] == "2026-09-19T03:59:00+00:00" and c["feed_item_id"] == fid
    merged = seeded.candidates("auto_merged")
    assert len(merged) == 1 and merged[0]["title"] == "Mid-term Exam I" and merged[0]["matched_item_id"]
    assert seeded.feed_item(fid)["extract_status"] == "done"
    # a second run finds nothing pending
    assert extract.run(xcfg, seeded)["batches"] == 0


def test_cli_failure_on_first_batch_leaves_items_pending(xcfg, seeded, monkeypatch):
    fid = _feed(seeded)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    stats = extract.run(xcfg, seeded)
    assert "error" in stats and stats["candidates"] == 0 and stats["failed"] == 0
    assert seeded.feed_item(fid)["extract_status"] == "pending"     # retried next run, not lost


def test_garbage_output_on_first_batch_leaves_items_pending(xcfg, seeded, monkeypatch):
    fid = _feed(seeded)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    stats = extract.run(xcfg, seeded)
    assert "error" in stats
    assert seeded.feed_item(fid)["extract_status"] == "pending"


def test_prose_wrapped_json_still_parses(xcfg, seeded, monkeypatch):
    fid = _feed(seeded)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "text")
    stats = extract.run(xcfg, seeded)
    assert stats["failed"] == 0 and seeded.feed_item(fid)["extract_status"] == "done"


def test_tiny_bodies_are_skipped(xcfg, seeded):
    fid = _feed(seeded, body="ok thanks")
    stats = extract.run(xcfg, seeded)
    assert stats["skipped"] == 1 and stats["batches"] == 0
    assert seeded.feed_item(fid)["extract_status"] == "skipped"


def test_missing_binary(xcfg, seeded):
    xcfg.extract.claude_bin = "/nonexistent/claude"
    _feed(seeded)
    assert "error" in extract.run(xcfg, seeded)


def test_to_candidates_validation(xcfg):
    row = {"id": 5, "course": "MATH246"}
    payload = {"candidates": [
        {"feed_ref": 1, "course": "UNKNOWN", "kind": "quiz", "title": "Quiz 9", "due_local": "2026-10-02",
         "due_known_time": False, "confidence": 1.7, "quote": "q", "action": "new"},
        {"feed_ref": 1, "course": "MATH246", "kind": "bogus", "title": "", "due_local": "2026-10-02",
         "due_known_time": False, "confidence": 0.5, "quote": "q", "action": "new"},
        {"feed_ref": 1, "course": "MATH246", "kind": "exam", "title": "No date", "due_local": None,
         "due_known_time": False, "confidence": 0.5, "quote": "q", "action": "new"},
        {"feed_ref": 1, "course": "MATH246", "kind": "exam", "title": "Bad date", "due_local": "Friday",
         "due_known_time": False, "confidence": 0.5, "quote": "q", "action": "new"},
    ]}
    cands = extract.to_candidates(payload, {1: row}, xcfg, "b1")
    assert len(cands) == 1
    c = cands[0]
    assert c.course == "MATH246" and c.confidence == 1.0 and c.all_day
    assert c.due_at == "2026-10-03T03:59:59+00:00"
