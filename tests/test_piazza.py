from umdhub.core.models import FeedItem
from umdhub.sources.piazza import activity_stamp, created_stamp, render_post


def test_activity_and_created_stamps_follow_the_log():
    # real shape from post #21 (2026-09-10): create → followup → update; `updated` is the creation time
    e = {"modified": "2026-09-05T03:19:57Z", "updated": "2026-09-04T17:17:06Z",
         "log": [{"t": "2026-09-04T17:17:06Z", "n": "create"}, {"t": "2026-09-04T20:19:30Z", "n": "followup"},
                 {"t": "2026-09-05T03:19:57Z", "n": "update"}]}
    assert activity_stamp(e) == "2026-09-05T03:19:57Z"
    assert created_stamp(e) == "2026-09-04T17:17:06Z"
    # an answer logged after `modified` still counts as activity
    e2 = {"modified": "2026-09-08T23:30:40Z",
          "log": [{"t": "2026-09-08T23:30:40Z", "n": "create"}, {"t": "2026-09-08T23:34:00Z", "n": "i_answer"}]}
    assert activity_stamp(e2) == "2026-09-08T23:34:00Z"
    assert activity_stamp({}) == "" and created_stamp({"updated": "x"}) == "x"

INSTRUCTORS = {"u-cliff", "u-ta1"}

FULL = {
    "id": "abc", "nr": 7, "type": "question", "tags": ["project0", "student"],
    "history": [
        {"subject": "Project 0 due date?", "content": "<p>When is <b>Project 0</b> due? The site says Dec 10.</p>",
         "created": "2026-09-05T14:00:00Z", "uid": "u-student"},
        {"subject": "Project 0 due?", "content": "<p>older revision</p>", "created": "2026-09-05T13:00:00Z", "uid": "u-student"},
    ],
    "children": [
        {"type": "i_answer", "history": [
            {"content": "<p>Dec 10 is a placeholder. Project 0 is due <b>Friday Sept 19 at 11:59pm</b>.</p>",
             "created": "2026-09-05T15:00:00Z", "uid": "u-cliff"}]},
        {"type": "s_answer", "history": [{"content": "<p>I think it's the 19th</p>", "uid": "u-other"}]},
        {"type": "followup", "subject": "<p>Is there a late deadline?</p>", "uid": "u-student2",
         "children": [{"type": "feedback", "subject": "<p>24h late window, -10%.</p>", "uid": "u-ta1"}]},
        {"type": "followup", "subject": "", "uid": "u-x", "children": []},
    ],
}


def test_render_post_includes_answers_and_followups():
    subject, body, instr, created = render_post(FULL, {"subject": "feed subj"}, INSTRUCTORS)
    assert subject == "Project 0 due date?"
    assert created == "2026-09-05T14:00:00Z"
    assert instr is True
    assert "When is Project 0 due?" in body
    assert "--- Instructor answer ---" in body and "Friday Sept 19 at 11:59pm" in body
    assert "--- Student answer ---" in body and "I think it's the 19th" in body
    assert "--- Follow-up (student) ---" in body and "late deadline" in body
    assert "--- Reply (instructor) ---" in body and "24h late window" in body
    assert "older revision" not in body


def test_render_post_student_only_thread():
    full = {"history": [{"subject": "hi", "content": "<p>hello</p>", "uid": "u-s"}], "children": []}
    subject, body, instr, _ = render_post(full, {}, INSTRUCTORS)
    assert (subject, body, instr) == ("hi", "hello", False)


def test_render_post_falls_back_to_snippet():
    subject, body, instr, _ = render_post({}, {"subject": "s", "content_snipet": "snip"}, set())
    assert (subject, body, instr) == ("s", "snip", False)


def test_feed_upsert_requeues_on_changed_body(state):
    fi = FeedItem(source="piazza", source_id="n:1", subject="Q", body="question only", course="CMSC330")
    fid, new = state.upsert_feed_item(fi)
    assert new
    state.set_extract_status([fid], "done")
    state.mark_read(fid)
    # same content again → nothing changes
    fid2, new2 = state.upsert_feed_item(fi)
    assert fid2 == fid and not new2
    row = state.feed_item(fid)
    assert row["extract_status"] == "done" and row["read_at"] is not None
    # instructor answered → body grows → pending + unread again, instructor flag set
    fi.body = "question only\n\n--- Instructor answer ---\ndue Friday"
    fi.is_instructor = True
    fid3, new3 = state.upsert_feed_item(fi)
    assert fid3 == fid and not new3
    row = state.feed_item(fid)
    assert row["extract_status"] == "pending" and row["read_at"] is None and row["is_instructor"] == 1
    assert "Instructor answer" in row["body"]
