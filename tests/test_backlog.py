from datetime import date

from tests.conftest import FIXTURES
from umdhub.sources.backlog import age_band, parse_backlog, read_backlog


def test_parse_backlog_open_items_only():
    rows = parse_backlog((FIXTURES / "backlog.md").read_text(encoding="utf-8"), today=date(2026, 9, 10))
    ids = [r["id"] for r in rows]
    assert ids == ["DW-001", "DW-002", "DW-000"]       # closed-section DW-999 is not parsed
    dw1 = rows[0]
    assert dw1["course"] == "CMSC351"
    assert dw1["artifact"].startswith("Gradescope Quiz 2")
    assert dw1["reps"] == "0"
    assert dw1["age_days"] == 6 and dw1["band"] == "4–7 days" and dw1["severity"] == "required"
    assert dw1["open"] is True
    assert rows[2]["open"] is False


def test_age_band_escalation():
    assert age_band(0)[1] == "listed"
    assert age_band(5)[1] == "required"
    assert age_band(9)[1] == "urgent"
    assert age_band(30)[1] == "failure"


def test_read_backlog_missing_file(tmp_path):
    assert read_backlog(tmp_path / "nope.md") == []
