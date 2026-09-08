from datetime import date

from tests.conftest import FIXTURES
from umdhub.sources import canvas_ics, course_site, email_imap, gradescope
from umdhub.sources.base import classify_kind, guess_course, parse_cookie_header


def test_helpers(cfg):
    assert parse_cookie_header("signed_token=abc; _gradescope_session=def") == {
        "signed_token": "abc", "_gradescope_session": "def"}
    assert parse_cookie_header("Cookie: a=1\nb=2") == {"a": "1", "b": "2"}
    assert parse_cookie_header(None) == {}
    assert classify_kind("Final Exam") == "exam"
    assert classify_kind("Lecture Quiz 3") == "quiz"
    assert classify_kind("Project 2") == "project"
    assert classify_kind("Homework 0") == "assignment"
    assert classify_kind("Fall Break", "event") == "event"
    assert guess_course("[CMSC330] Project 1 released", cfg.courses) == "CMSC330"
    assert guess_course("MATH 246 quiz Friday", cfg.courses) == "MATH246"
    assert guess_course("hello", cfg.courses) is None


# ---------------------------------------------------------------- canvas .ics
def test_canvas_ics_parse(cfg):
    items = canvas_ics.parse_ics((FIXTURES / "canvas.ics").read_text(encoding="utf-8"), cfg.courses)
    by = {it.title: it for it in items}
    assert set(by) == {"Introductory Discussion Board Post", "Fall Break - no class", "Matlab Project 1"}
    post = by["Introductory Discussion Board Post"]
    assert post.course == "ENGL390" and post.kind == "assignment"
    assert post.due_at == "2026-09-12T03:59:00+00:00" and not post.all_day
    assert post.source_id == "event-assignment-7749321"
    assert post.notes == "Post and respond to at least 3 peers."
    brk = by["Fall Break - no class"]
    assert brk.course == "MATH246" and brk.kind == "event" and brk.all_day
    assert brk.due_at == "2026-10-14T03:59:59+00:00"
    # 'Project' outranks 'Matlab' in classify_kind; merge keeps the seed's kind anyway.
    assert by["Matlab Project 1"].kind == "project"
    assert by["Matlab Project 1"].url == "https://umd.instructure.com/courses/999/assignments/7749999"


# ---------------------------------------------------------------- gradescope
def test_gradescope_account_and_mapping(cfg):
    html = (FIXTURES / "gradescope_account.html").read_text(encoding="utf-8")
    boxes = gradescope.parse_account(html, "Fall 2026")
    assert [b["id"] for b in boxes] == ["1374332", "1380001", "1380002"]
    assert [gradescope._map_course(b, cfg.courses) for b in boxes] == ["CMSC330", "CMSC351", None]
    assert len(gradescope.parse_account(html, None)) == 4
    assert len(gradescope.parse_account(html, "Spring 2027")) == 0


def test_gradescope_course_table():
    html = (FIXTURES / "gradescope_course.html").read_text(encoding="utf-8")
    items, grades = gradescope.parse_course(html, "CMSC330", "1374332", "https://www.gradescope.com")
    assert [i.title for i in items] == ["Lecture Quiz 1", "Lecture Quiz 2", "Project 1"]
    q1, q2, p1 = items
    assert q1.source_id == "1374332:5551111"
    assert q1.due_at == "2026-09-08T03:59:00+00:00"
    assert q1.release_at == "2026-09-03T16:00:00+00:00"
    assert q1.submission_status == "Submitted" and q1.score == "9.0 / 10.0"
    assert q1.url == "https://www.gradescope.com/courses/1374332/assignments/5551111/submissions/123"
    assert q2.source_id == "1374332:5552222"          # button row → data-assignment-id
    assert q2.due_at == "2026-09-15T03:59:00+00:00" and q2.late_due_at == "2026-09-16T03:59:00+00:00"
    assert q2.submission_status == "No Submission" and q2.score is None
    assert p1.kind == "project"
    assert len(grades) == 1 and grades[0].score == 9.0 and grades[0].max_score == 10.0


def test_gradescope_empty_page_yields_nothing():
    assert gradescope.parse_course("<html><body>nope</body></html>", "CMSC330", "1", "https://x") == ([], [])


# ---------------------------------------------------------------- course site
def test_cmsc330_home_dated_tables():
    html = (FIXTURES / "cmsc330_home.html").read_text(encoding="utf-8")
    items = course_site.parse_dated_tables(html, "CMSC330", date(2026, 8, 31), date(2026, 12, 16))
    by = {it.title: it for it in items}
    assert len(items) == 14
    assert by["Quiz 2"].due_at == "2026-09-26T03:59:59+00:00" and by["Quiz 2"].all_day   # "September25th"
    assert by["Final Exam"].due_at == "2026-12-15T23:30:00+00:00" and not by["Final Exam"].all_day
    assert by["Exam 1 Coding"].kind == "exam"
    rel = by["Project 1 released"]
    assert rel.kind == "event" and rel.due_at == "2026-09-16T03:59:59+00:00"
    assert all(it.source_id.startswith("CMSC330:") for it in items)


def test_visible_text_and_diff():
    a = course_site.visible_text("<html><body><script>x()</script><h1>Hi</h1><p>one</p><p>two</p></body></html>")
    assert a == "Hi\none\ntwo"
    d = course_site._diff("Hi\none\ntwo", "Hi\none\nthree")
    assert "-two" in d and "+three" in d


# ---------------------------------------------------------------- email
def test_email_parse_message(cfg):
    raw = (FIXTURES / "email_bchm.eml").read_bytes()
    fi = email_imap.parse_message(raw, cfg.courses, "42", ["umd.edu"])
    assert fi is not None
    assert fi.course == "BCHM461" and fi.is_instructor
    assert fi.subject == "BCHM461 Homework 1 posted"
    assert fi.source_id == "<abc123.hw1@umd.edu>"
    assert fi.posted_at == "2026-09-08T14:15:00+00:00"
    assert "due Friday, September 18" in fi.body


def test_email_parse_drops_unrelated(cfg):
    raw = b"From: Store <deals@shop.example>\nSubject: 50% off\nDate: Tue, 8 Sep 2026 10:15:00 -0400\n\nbuy stuff\n"
    assert email_imap.parse_message(raw, cfg.courses, "1", ["umd.edu"]) is None
    raw2 = b"From: Registrar <registrar@umd.edu>\nSubject: Schedule adjustment ends\n\nreminder\n"
    fi = email_imap.parse_message(raw2, cfg.courses, "2", ["umd.edu"])
    assert fi is not None and fi.course is None      # kept by domain, no course guess
    assert email_imap._classify_login_error("[AUTHENTICATIONFAILED] Invalid credentials").startswith("login rejected")
    assert "IMAP is disabled" in email_imap._classify_login_error("[ALERT] IMAP access is disabled for your domain")
