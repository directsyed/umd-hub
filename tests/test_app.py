from fastapi.testclient import TestClient

from umdhub.app import create_app


def _login(client):
    r = client.get("/login?key=testsecret&next=/", follow_redirects=False)
    assert r.status_code == 302 and "hub_key" in r.cookies


def test_healthz_is_public(client):
    assert client.get("/healthz").json()["ok"] is True


def test_requires_cookie_then_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")
    r = client.get("/login?key=wrong", follow_redirects=False)
    assert r.status_code == 200 and "wrong key" in r.text
    _login(client)
    r = client.get("/")
    assert r.status_code == 200 and "Today" in r.text


def test_wrong_network_is_forbidden(cfg, monkeypatch):
    monkeypatch.setenv("HUB_SHARED_SECRET", "testsecret")
    with TestClient(create_app(cfg), client=("8.8.8.8", 1)) as c:
        assert c.get("/").status_code == 403
        assert c.get("/login").status_code == 403


def test_today_lists_seed_items_and_actions(seeded, client):
    _login(client)
    html = client.get("/").text
    assert "Quiz 1" in html and "MATH246" in html
    row = seeded.items_between("2026-09-11", "2026-09-11")[0]
    r = client.post(f"/item/{row['id']}/done", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200 and r.json()["status"] == "done"
    r = client.post(f"/item/{row['id']}/snooze", data={"until": "3d"}, headers={"X-Requested-With": "fetch"})
    assert r.json()["status"] == "snoozed"
    r = client.post(f"/item/{row['id']}/reopen", headers={"X-Requested-With": "fetch"})
    assert r.json()["status"] == "open"


def test_missed_status_leaves_overdue_but_stays_in_archive(seeded, client):
    from umdhub.core import timeutil as tu
    _login(client)
    past, all_day = tu.from_local("2026-09-01", None)
    iid = seeded.add_manual_item("CMSC351", "quiz", "Quiz 3 (NP Intro)", past, all_day)
    assert any(r["id"] == iid for r in seeded.overdue(tu.utcnow_iso(), "2026-09-09"))
    today_html = client.get("/").text
    assert 'data-act="missed"' in today_html                # button renders on overdue rows…
    assert today_html.count('data-act="missed"') < today_html.count('data-act="done"')  # …but not on future ones
    r = client.post(f"/item/{iid}/missed", headers={"X-Requested-With": "fetch"})
    assert r.json()["status"] == "missed"
    assert not any(r["id"] == iid for r in seeded.overdue(tu.utcnow_iso(), "2026-09-09"))
    html = client.get("/course/CMSC351").text
    assert "Quiz 3 (NP Intro)" in html and 'class="tag bad">missed' in html


def test_post_with_foreign_origin_rejected(seeded, client):
    _login(client)
    row = seeded.items_between("2026-09-11", "2026-09-11")[0]
    r = client.post(f"/item/{row['id']}/done", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_course_backlog_status_pages(seeded, client):
    _login(client)
    assert "Differential Equations" in client.get("/course/MATH246").text
    assert client.get("/course/NOPE").status_code == 404
    html = client.get("/backlog").text
    assert "DW-002" in html and "DW-000" not in html.split("closed")[0]
    assert "Sources" in client.get("/status").text
    assert client.get("/credentials").status_code == 200
    assert client.get("/candidates").status_code == 200
    assert client.get("/feed").status_code == 200


def test_manual_add_and_credential_save(seeded, client):
    _login(client)
    r = client.post("/item/new", data={"course": "BCHM461", "kind": "assignment", "title": "HW 1 (email)",
                                       "due_date": "2026-09-20", "due_time": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert "HW 1 (email)" in client.get("/course/BCHM461").text
    r = client.post("/credential", data={"source": "gradescope", "name": "cookie", "value": "signed_token=x"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert seeded.creds_for("gradescope") == {"cookie": "signed_token=x"}
    assert client.post("/credential", data={"source": "nope", "name": "x", "value": "y"}).status_code == 400


def test_refresh_status_json(client):
    _login(client)
    j = client.get("/refresh/status").json()
    assert j["in_progress"] is False and j["last_refresh_at"] is None
