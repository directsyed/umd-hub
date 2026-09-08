from datetime import date

from umdhub.core import timeutil as tu


def test_from_local_all_day_is_end_of_day_et():
    iso, all_day = tu.from_local("2026-09-11", None)
    assert all_day is True
    assert iso == "2026-09-12T03:59:59+00:00"  # EDT = UTC-4
    assert tu.local_date_str(iso) == "2026-09-11"


def test_from_local_timed_edt_and_est():
    iso, all_day = tu.from_local("2026-09-11", "23:59")
    assert (iso, all_day) == ("2026-09-12T03:59:00+00:00", False)
    iso, _ = tu.from_local("2026-12-14", "16:00")
    assert iso == "2026-12-14T21:00:00+00:00"  # EST = UTC-5


def test_dst_boundary_nov_1_2026():
    before, _ = tu.from_local("2026-10-31", "23:59")
    after, _ = tu.from_local("2026-11-01", "23:59")
    assert before == "2026-11-01T03:59:00+00:00"
    assert after == "2026-11-02T04:59:00+00:00"
    assert tu.local_date_str(before) == "2026-10-31"
    assert tu.local_date_str(after) == "2026-11-01"


def test_from_local_accepts_clock_strings():
    iso, _ = tu.from_local("2026-10-21", "6:30pm")
    assert tu.fmt_time_et(iso) == "6:30 PM"


def test_parse_clock():
    assert tu.parse_clock("6:30pm") == (18, 30)
    assert tu.parse_clock("11:59 PM") == (23, 59)
    assert tu.parse_clock("12:00am") == (0, 0)
    assert tu.parse_clock("12pm") == (12, 0)
    assert tu.parse_clock("10am") == (10, 0)
    assert tu.parse_clock("noon") is None


def test_parse_time_range():
    assert tu.parse_time_range("6:30pm - 8:30pm") == ((18, 30), (20, 30))
    assert tu.parse_time_range("10:30 am–12:30 pm") == ((10, 30), (12, 30))
    assert tu.parse_time_range("all day") is None


def test_parse_month_day_tolerates_site_typos():
    assert tu.parse_month_day("September25th") == (9, 25)
    assert tu.parse_month_day("Dec. 15th 6:30pm - 8:30pm") == (12, 15)
    assert tu.parse_month_day("October 6th") == (10, 6)
    assert tu.parse_month_day("Sept 11") == (9, 11)
    assert tu.parse_month_day("TBD") is None


def test_infer_year_and_weekday_dates():
    assert tu.infer_year(9, 11, date(2026, 8, 31), date(2026, 12, 16)) == 2026
    assert tu.weekday_dates("FRI", date(2026, 9, 18), date(2026, 10, 2)) == [
        date(2026, 9, 18), date(2026, 9, 25), date(2026, 10, 2)]


def test_day_label_and_fmt():
    today = date(2026, 9, 8)
    assert tu.day_label("2026-09-08", today) == "Today"
    assert tu.day_label("2026-09-09", today) == "Tomorrow"
    assert tu.day_label("2026-09-11", today) == "Fri Sep 11"
    assert tu.day_label("2026-09-06", today) == "Sun Sep 6 (2 d ago)"
    assert tu.fmt_et("2026-09-12T03:59:00+00:00") == "Fri Sep 11, 11:59 PM"
    assert tu.fmt_et("2026-09-12T03:59:59+00:00", all_day=True) == "Fri Sep 11"
    assert tu.fmt_et(None) == "no date"
