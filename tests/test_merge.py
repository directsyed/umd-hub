from umdhub.core import merge


def test_normalize_title():
    n = merge.normalize_title
    assert n("Mid-term Exam I") == "exam 1"                   # midterm ≡ exam, duplicates collapsed
    assert n("Midterm 1") == "exam 1"
    assert n("HW #3 (Recurrences)") == "homework 3 recurrences"
    assert n("CMSC330 Project 1") == "project 1"
    assert n("Lecture Quiz 3") == "lecture quiz 3"
    assert n("Lecture quiz (weekly)") == "lecture quiz weekly"
    assert n("hw3") == "homework 3"
    assert n("Quiz 2 — 11:59pm") == "quiz 2 11 59pm"
    assert n("Job description + résumé") == "job description resume"


def test_similar():
    s = merge.similar
    n = merge.normalize_title
    assert s("lecture quiz 3", "lecture quiz weekly")        # placeholder word ignored → containment
    assert not s("quiz 1", "quiz 2")                          # number tokens disagree
    assert s("homework 0", "homework 0 cmsc250 review")       # containment
    assert s(n("Mid-term Exam I"), n("Exam 1"))               # midterm ≡ exam
    assert not s("matlab 1", "quiz 1")                        # same number, no overlap otherwise
    # the real-world pairs that slipped through on 2026-09-09
    assert s(n("Lecture Quiz 1 - Syllabus, Semantical Rules, Ocaml"), n("Lecture quiz (weekly)"))
    assert s(n("Introductory Discussion Board Post"), n("Intro discussion board post + ≥3 peer responses"))
    assert s(n("Midterm 1"), n("Exam 1"))
    assert s(n("Project 1"), n("Matlab Project 1"))
    assert s(n("HW1"), n("Regular homework (weekly)"))                       # via Jaccard, not containment
    assert not s(n("HW0"), n("NP"))
    assert not s(n("Quiz 1"), n("Lecture quiz (weekly)"))    # discussion quiz ≠ lecture-quiz placeholder
    # a placeholder reduced to one generic word must not swallow unrelated items
    assert not s(n("NP-completeness assignment (date TBD)"), n("Regular homework (weekly)"))
    assert s(n("NP"), n("NP-completeness assignment (date TBD)"))            # specific single token is fine


def test_match_prefers_same_course_and_close_date():
    obs = {"course": "MATH246", "title_norm": "quiz 1", "due_date_local": "2026-09-11"}
    cands = [
        {"id": 1, "course": "MATH246", "title_norm": "quiz 1", "due_date_local": "2026-09-11"},
        {"id": 2, "course": "CMSC330", "title_norm": "quiz 1", "due_date_local": "2026-09-11"},
        {"id": 3, "course": "MATH246", "title_norm": "quiz 1", "due_date_local": "2026-09-25"},
    ]
    assert merge.match(obs, cands)["id"] == 1
    obs_far = {**obs, "due_date_local": "2026-10-16"}
    assert merge.match(obs_far, cands) is None


def test_match_undated_placeholder():
    obs = {"course": "ENGL390", "title_norm": "cover letter draft", "due_date_local": "2026-10-02"}
    cands = [{"id": 5, "course": "ENGL390", "title_norm": "cover letter draft final", "due_date_local": None}]
    assert merge.match(obs, cands)["id"] == 5


def test_match_prefers_dated_over_placeholder_when_both_fit():
    obs = {"course": "CMSC330", "title_norm": "lecture quiz 3", "due_date_local": "2026-09-21"}
    cands = [
        {"id": 1, "course": "CMSC330", "title_norm": "lecture quiz weekly", "due_date_local": "2026-09-21"},
        {"id": 2, "course": "CMSC330", "title_norm": "lecture quiz weekly", "due_date_local": "2026-09-14"},
    ]
    assert merge.match(obs, cands)["id"] == 1


def _seed_existing():
    return {"id": 1, "course": "CMSC330", "kind": "quiz", "title": "Quiz 2", "title_norm": "quiz 2",
            "due_at": "2026-09-26T03:59:59+00:00", "due_date_local": "2026-09-25", "all_day": 1,
            "url": None, "weight_note": "2%", "notes": None, "primary_source": "seed", "status": "open"}


def test_live_source_overrides_seed_dates_but_seed_keeps_kind_and_weight():
    ex = _seed_existing()
    obs = {"course": "CMSC330", "kind": "assignment", "title": "Quiz 2 (discussion)",
           "title_norm": "quiz 2 discussion", "due_at": "2026-09-25T14:00:00+00:00",
           "due_date_local": "2026-09-25", "all_day": 0, "url": "https://gradescope/1"}
    up = merge.merge_fields(ex, obs, "gradescope")
    assert up["due_at"] == "2026-09-25T14:00:00+00:00"
    assert up["url"] == "https://gradescope/1"
    assert "kind" not in up                       # seed wins on kind
    assert "weight_note" not in up                # kept
    assert up["title"] == "Quiz 2 (discussion)"
    assert "seed: Quiz 2" in up["notes"]
    assert up["primary_source"] == "gradescope"
    assert "status" not in up


def test_expected_placeholder_upgrades_kind():
    ex = {**_seed_existing(), "kind": "expected"}
    up = merge.merge_fields(ex, {"kind": "quiz", "title": "Lecture Quiz 3", "title_norm": "lecture quiz 3"},
                            "gradescope")
    assert up["kind"] == "quiz"


def test_seed_does_not_override_live_dates():
    ex = {**_seed_existing(), "primary_source": "gradescope", "weight_note": None}
    obs = {"due_at": "2026-09-30T03:59:59+00:00", "due_date_local": "2026-09-29", "all_day": 1,
           "weight_note": "2%", "title": "Quiz 2", "title_norm": "quiz 2", "kind": "quiz"}
    up = merge.merge_fields(ex, obs, "seed")
    assert "due_at" not in up
    assert up["weight_note"] == "2%"
    assert "primary_source" not in up
