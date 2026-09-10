"""FastAPI web app: python -m umdhub.app

Phone-first HTML. Every route opens its own SQLite connection (WAL makes that cheap).
Auth (see AuthMiddleware): Phase A = LAN/VPN CIDR + a shared-secret cookie; Phase B adds
Cloudflare Access JWT verification for traffic arriving through the tunnel (127.0.0.1).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .core import timeutil as tu
from .core.config import REPO_ROOT, Config, env, load_config
from .core.models import KIND_ICON, KINDS, Item
from .core.state import State
from .sources import SOURCE_ORDER, get_probe
from .sources.backlog import read_backlog
from .sources.base import CREDENTIAL_SLOTS, ENV_SLOTS

log = logging.getLogger("umdhub.app")
WEB_DIR = Path(__file__).parent / "web"
COOKIE = "hub_key"

templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["et"] = lambda iso, all_day=False: tu.fmt_et(iso, bool(all_day))
templates.env.filters["et_time"] = lambda iso, all_day=False: tu.fmt_time_et(iso, bool(all_day))
templates.env.filters["day_label"] = lambda d: tu.day_label(d)
templates.env.filters["days_until"] = lambda d: tu.days_until(d)
templates.env.globals["KIND_ICON"] = KIND_ICON
templates.env.globals["KINDS"] = KINDS
templates.env.globals["version"] = __version__


# ---------------------------------------------------------------- auth
def _cookie_token(secret: str) -> str:
    return hmac.new(secret.encode(), b"umdhub-login", hashlib.sha256).hexdigest()


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, cfg: Config):
        super().__init__(app)
        self.cfg = cfg
        self.nets = [ipaddress.ip_network(c) for c in cfg.web.allowed_cidrs]
        self._jwks = None

    def _ip_ok(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in self.nets)

    def _cookie_ok(self, request: Request) -> bool:
        secret = env("HUB_SHARED_SECRET")
        if not secret:
            return True  # not configured → CIDR-only (logged once at startup)
        tok = request.cookies.get(COOKIE, "")
        return hmac.compare_digest(tok, _cookie_token(secret))

    def _cf_ok(self, request: Request) -> bool:
        token = request.headers.get("Cf-Access-Jwt-Assertion")
        team, aud = env("CF_ACCESS_TEAM"), env("CF_ACCESS_AUD")
        if not (token and team and aud):
            return False
        try:
            import jwt
            from jwt import PyJWKClient
            if self._jwks is None:
                self._jwks = PyJWKClient(f"https://{team}.cloudflareaccess.com/cdn-cgi/access/certs")
            key = self._jwks.get_signing_key_from_jwt(token)
            jwt.decode(token, key.key, algorithms=["RS256"], audience=aud)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("cloudflare access jwt rejected: %s", e)
            return False

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/healthz" or path.startswith("/static/"):
            return await call_next(request)
        ip = request.client.host if request.client else "0.0.0.0"
        ip_ok = self._ip_ok(ip)
        is_loopback = ip_ok and ipaddress.ip_address(ip).is_loopback

        if self.cfg.web.auth_mode == "both" and is_loopback and request.headers.get("Cf-Access-Jwt-Assertion"):
            allowed = self._cf_ok(request)
        elif path == "/login":
            allowed = ip_ok
        else:
            allowed = ip_ok and self._cookie_ok(request)

        if not allowed:
            if not ip_ok:
                return Response("forbidden", status_code=403)
            if request.method == "GET":
                return RedirectResponse(f"/login?next={quote(str(request.url.path))}", status_code=302)
            return Response("login required", status_code=401)

        if request.method == "POST":
            origin = request.headers.get("Origin") or request.headers.get("Referer")
            if origin:
                o_host = urlparse(origin).netloc.split(":")[0]
                r_host = request.headers.get("Host", "").split(":")[0]
                if o_host and r_host and o_host != r_host:
                    return Response("bad origin", status_code=403)
        return await call_next(request)


# ---------------------------------------------------------------- app factory
def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="UMD Hub", version=__version__, docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.add_middleware(AuthMiddleware, cfg=cfg)
    if not env("HUB_SHARED_SECRET"):
        log.warning("HUB_SHARED_SECRET unset — access is gated by allowed_cidrs only")
    _register(app)
    return app


def get_state(request: Request) -> Iterator[State]:
    st = State(request.app.state.cfg.db_path())
    try:
        yield st
    finally:
        st.close()


def _wants_json(request: Request) -> bool:
    return (request.headers.get("X-Requested-With") == "fetch"
            or "application/json" in request.headers.get("Accept", ""))


def _back(request: Request, default: str = "/") -> RedirectResponse:
    ref = request.headers.get("Referer")
    target = default
    if ref:
        u = urlparse(ref)
        if u.path:
            target = u.path + (f"?{u.query}" if u.query else "")
    return RedirectResponse(target, status_code=303)


def _ctx(request: Request, st: State, **kw: Any) -> dict[str, Any]:
    cfg: Config = request.app.state.cfg
    creds = st.credentials()
    failing = sorted({s for (s, _), r in creds.items() if r["last_error"]})
    last = st.meta_get("last_refresh_at")
    stale = True
    if last:
        dt = tu.parse_iso(last)
        stale = bool(dt) and (tu.utcnow() - dt) > timedelta(hours=cfg.web.stale_after_hours)
    ctx = {
        "request": request, "cfg": cfg, "courses": cfg.courses, "today": tu.today_local(),
        "now_et": tu.fmt_et(tu.utcnow_iso()),
        "badges": {
            "candidates": st.pending_candidate_count(),
            "unread": st.unread_count(),
            "failing": failing,
            "last_refresh": last, "stale": stale,
            "in_progress": st.meta_get("refresh_in_progress") == "1",
        },
        "msg": request.query_params.get("msg"),
    }
    ctx.update(kw)
    return ctx


def _trigger_refresh(cfg: Config) -> tuple[bool, str]:
    unit = cfg.web.refresh_unit
    err = ""
    try:
        r = subprocess.run(["systemctl", "--user", "start", "--no-block", unit],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return True, f"started {unit}"
        err = (r.stderr or r.stdout).strip()
    except Exception as e:  # noqa: BLE001
        err = str(e)
    try:
        subprocess.Popen([sys.executable, "-m", "umdhub.refresh", "--trigger", "manual"], cwd=str(REPO_ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True, f"started in-process fallback ({err or 'systemd unit unavailable'})"
    except Exception as e:  # noqa: BLE001
        return False, f"could not start refresh: {e} / {err}"


def _snooze_until(spec: str, today: date) -> str:
    """'tomorrow' | '1d' | '3d' | 'YYYY-MM-DD' -> UTC ISO at 08:00 ET on that day."""
    spec = (spec or "tomorrow").strip().lower()
    if spec == "tomorrow":
        d = today + timedelta(days=1)
    elif spec.endswith("d") and spec[:-1].isdigit():
        d = today + timedelta(days=int(spec[:-1]))
    else:
        d = date.fromisoformat(spec)
    iso, _ = tu.from_local(d.isoformat(), "08:00")
    return iso


# ---------------------------------------------------------------- routes
def _register(app: FastAPI) -> None:
    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__})

    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request, key: str | None = None, next: str = "/"):
        secret = env("HUB_SHARED_SECRET")
        if key is not None and secret and hmac.compare_digest(key, secret):
            resp = RedirectResponse(next or "/", status_code=302)
            resp.set_cookie(COOKIE, _cookie_token(secret), max_age=request.app.state.cfg.web.cookie_days * 86400,
                            httponly=True, samesite="strict")
            return resp
        return templates.TemplateResponse(request, "login.html.j2", {
            "request": request, "next": next, "bad": key is not None, "no_secret": not secret, "version": __version__})

    @app.post("/login")
    def login_post(request: Request, key: str = Form(...), next: str = Form("/")):
        return login_get(request, key=key, next=next)

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie(COOKIE)
        return resp

    # ---- today
    @app.get("/", response_class=HTMLResponse)
    def today(request: Request, st: State = Depends(get_state)):
        st.wake_snoozed()
        today = tu.today_local()
        now_iso = tu.utcnow_iso()
        overdue = st.overdue(now_iso, today.isoformat())
        seen = {r["id"] for r in overdue}
        days: list[dict[str, Any]] = []
        for i in range(7):
            d = today + timedelta(days=i)
            rows = [r for r in st.items_between(d.isoformat(), d.isoformat()) if r["id"] not in seen]
            days.append({"date": d.isoformat(), "label": tu.day_label(d.isoformat(), today), "rows": rows})
        later_start, later_end = today + timedelta(days=7), today + timedelta(days=13)
        later = st.items_between(later_start.isoformat(), later_end.isoformat())
        beyond = st.items_between((today + timedelta(days=14)).isoformat(),
                                  (today + timedelta(days=45)).isoformat())
        return templates.TemplateResponse(request, "today.html.j2", _ctx(
            request, st, overdue=overdue, days=days, later=later, beyond=beyond, snoozed=st.snoozed()))

    # ---- course
    @app.get("/course/{code}", response_class=HTMLResponse)
    def course(request: Request, code: str, st: State = Depends(get_state)):
        cfg: Config = request.app.state.cfg
        if code not in cfg.courses:
            raise HTTPException(404)
        today = tu.today_local()
        open_rows = st.items_for_course(code, ("open", "snoozed"))
        dated = [r for r in open_rows if r["due_date_local"]]
        undated = [r for r in open_rows if not r["due_date_local"]]
        past = [r for r in dated if r["due_date_local"] < today.isoformat()]
        upcoming = [r for r in dated if r["due_date_local"] >= today.isoformat()]
        archive = st.items_for_course(code, ("done", "missed", "cancelled"))
        return templates.TemplateResponse(request, "course.html.j2", _ctx(
            request, st, code=code, course=cfg.courses[code], upcoming=upcoming, past=past, undated=undated,
            archive=archive, feed=st.feed(course=code, limit=30), grades=st.grades_for_course(code)))

    # ---- item actions
    @app.post("/item/{item_id}/{action}")
    def item_action(request: Request, item_id: int, action: str, until: str = Form(None),
                    st: State = Depends(get_state)):
        row = st.item(item_id)
        if not row:
            raise HTTPException(404)
        if action == "done":
            st.set_status(item_id, "done")
        elif action in ("undone", "reopen"):
            st.set_status(item_id, "open")
        elif action == "missed":
            st.set_status(item_id, "missed")
        elif action == "cancel":
            st.set_status(item_id, "cancelled")
        elif action == "snooze":
            spec = until or request.query_params.get("until") or "tomorrow"
            st.set_status(item_id, "snoozed", _snooze_until(spec, tu.today_local()))
        else:
            raise HTTPException(400, "unknown action")
        if _wants_json(request):
            return JSONResponse({"ok": True, "id": item_id, "status": st.item(item_id)["status"]})
        return _back(request)

    @app.get("/item/new", response_class=HTMLResponse)
    def item_new_get(request: Request, st: State = Depends(get_state)):
        return templates.TemplateResponse(request, "item_form.html.j2", _ctx(request, st))

    @app.post("/item/new")
    def item_new_post(request: Request, course: str = Form(...), kind: str = Form("assignment"),
                      title: str = Form(...), due_date: str = Form(""), due_time: str = Form(""),
                      notes: str = Form(""), url: str = Form(""), st: State = Depends(get_state)):
        cfg: Config = request.app.state.cfg
        if course not in cfg.courses or kind not in KINDS or not title.strip():
            raise HTTPException(400, "bad form")
        due_at, all_day = (None, False)
        if due_date.strip():
            due_at, all_day = tu.from_local(due_date.strip(), due_time.strip() or None)
        st.add_manual_item(course, kind, title.strip(), due_at, all_day, notes.strip() or None, url.strip() or None)
        return RedirectResponse(f"/course/{course}?msg=added", status_code=303)

    # ---- feed
    @app.get("/feed", response_class=HTMLResponse)
    def feed(request: Request, unread: int = 0, source: str | None = None, st: State = Depends(get_state)):
        rows = st.feed(unread_only=bool(unread), source=source or None, limit=150)
        return templates.TemplateResponse(request, "feed.html.j2", _ctx(
            request, st, rows=rows, unread=unread, source=source or ""))

    @app.post("/feed/{fid}/read")
    def feed_read(request: Request, fid: int, st: State = Depends(get_state)):
        st.mark_read(fid)
        if _wants_json(request):
            return JSONResponse({"ok": True})
        return _back(request, "/feed")

    @app.post("/feed/read-all")
    def feed_read_all(request: Request, st: State = Depends(get_state)):
        st.mark_read(None)
        return _back(request, "/feed")

    # ---- candidates
    @app.get("/candidates", response_class=HTMLResponse)
    def candidates(request: Request, st: State = Depends(get_state)):
        return templates.TemplateResponse(request, "candidates.html.j2", _ctx(
            request, st, rows=st.candidates("pending"), recent=st.candidates("confirmed")[:10]))

    @app.post("/candidate/{cid}/confirm")
    def candidate_confirm(request: Request, cid: int, course: str = Form(...), kind: str = Form(...),
                          title: str = Form(...), due_date: str = Form(""), due_time: str = Form(""),
                          st: State = Depends(get_state)):
        c = st.candidate(cid)
        if not c or c["state"] != "pending":
            raise HTTPException(404)
        due_at, all_day = (None, False)
        if due_date.strip():
            due_at, all_day = tu.from_local(due_date.strip(), due_time.strip() or None)
        if c["action"] == "cancel" and c["matched_item_id"]:
            st.set_status(int(c["matched_item_id"]), "cancelled")
            st.decide_candidate(cid, "confirmed")
            return RedirectResponse("/candidates?msg=cancelled", status_code=303)
        it = Item(course=course, kind=kind, title=title.strip(), source="candidate", source_id=f"cand:{cid}",
                  due_at=due_at, all_day=all_day, notes=f"from: {c['quote']}" if c["quote"] else None)
        if c["matched_item_id"] and c["action"] in ("move", "new"):
            # Force-link to the matched item so the merge updates it instead of matching afresh.
            st.conn.execute(
                "INSERT OR IGNORE INTO item_source(item_id, source, source_id, first_seen, last_seen) "
                "VALUES(?,?,?,?,?)", (c["matched_item_id"], "candidate", f"cand:{cid}", tu.utcnow_iso(), tu.utcnow_iso()))
        item_id, _, _ = st.upsert_item(it)
        row = st.item(item_id)
        if row and row["status"] == "cancelled":
            st.set_status(item_id, "open")   # confirming a candidate means "this is real after all"
        st.decide_candidate(cid, "confirmed", item_id)
        return RedirectResponse("/candidates?msg=confirmed", status_code=303)

    @app.post("/candidate/{cid}/reject")
    def candidate_reject(request: Request, cid: int, st: State = Depends(get_state)):
        st.decide_candidate(cid, "rejected")
        return _back(request, "/candidates")

    # ---- credentials
    @app.get("/credentials", response_class=HTMLResponse)
    def credentials(request: Request, st: State = Depends(get_state)):
        cfg: Config = request.app.state.cfg
        rows = st.credentials()
        slots = []
        for s in CREDENTIAL_SLOTS:
            r = rows.get((s.source, s.name))
            val = r["value"] if r else ""
            masked = (val[:10] + "…" + f"({len(val)} chars)") if val else ""
            slots.append({"slot": s, "row": r, "masked": masked,
                          "enabled": cfg.source(s.source).enabled})
        envs = [{"source": s, "name": n, "label": l, "set": bool(env(n))} for s, n, l in ENV_SLOTS]
        return templates.TemplateResponse(request, "credentials.html.j2", _ctx(
            request, st, slots=slots, envs=envs, health=st.source_health(72)))

    @app.post("/credential")
    def credential_set(request: Request, source: str = Form(...), name: str = Form(...), value: str = Form(""),
                       st: State = Depends(get_state)):
        if not any(s.source == source and s.name == name for s in CREDENTIAL_SLOTS):
            raise HTTPException(400, "unknown credential slot")
        if value.strip():
            st.set_credential(source, name, value)
            msg = f"saved {source}:{name}"
        else:
            st.delete_credential(source, name)
            msg = f"cleared {source}:{name}"
        return RedirectResponse(f"/credentials?msg={quote(msg)}", status_code=303)

    @app.post("/credential/{source}/test")
    def credential_test(request: Request, source: str, st: State = Depends(get_state)):
        cfg: Config = request.app.state.cfg
        if source not in SOURCE_ORDER:
            raise HTTPException(404)
        probe = get_probe(source)
        if probe is None:
            msg = f"{source}: no probe available (source not installed yet?)"
        else:
            from .core.http import HttpClient
            try:
                ok, text = probe(cfg, cfg.source(source), st.creds_for(source), HttpClient())
            except Exception as e:  # noqa: BLE001
                ok, text = False, f"probe crashed: {e}"
            st.credential_health(source, ok=ok, error=None if ok else text)
            msg = f"{source}: {'OK' if ok else 'FAIL'} — {text}"
        return RedirectResponse(f"/credentials?msg={quote(msg[:300])}", status_code=303)

    # ---- backlog / status / refresh
    @app.get("/backlog", response_class=HTMLResponse)
    def backlog(request: Request, st: State = Depends(get_state)):
        cfg: Config = request.app.state.cfg
        path = cfg.backlog_path()
        rows = read_backlog(path)
        return templates.TemplateResponse(request, "backlog.html.j2", _ctx(
            request, st, rows=rows, path=str(path), exists=path.exists()))

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request, st: State = Depends(get_state)):
        cfg: Config = request.app.state.cfg
        summary = st.meta_json("last_refresh_summary", {})
        db_size = cfg.db_path().stat().st_size if cfg.db_path().exists() else 0
        return templates.TemplateResponse(request, "status.html.j2", _ctx(
            request, st, runs=st.recent_runs(40), health=st.source_health(48), summary=summary,
            db_size=db_size, item_count=st.count_items(), order=SOURCE_ORDER,
            enabled={s: cfg.source(s).enabled for s in SOURCE_ORDER},
            lifetime=st.meta_json("extract:lifetime", {}) or {}))

    @app.post("/refresh")
    def refresh(request: Request, st: State = Depends(get_state)):
        cfg: Config = request.app.state.cfg
        ok, msg = _trigger_refresh(cfg)
        if _wants_json(request):
            return JSONResponse({"ok": ok, "message": msg})
        return RedirectResponse(f"/status?msg={quote(msg)}", status_code=303)

    @app.get("/refresh/status")
    def refresh_status(st: State = Depends(get_state)):
        return JSONResponse({
            "in_progress": st.meta_get("refresh_in_progress") == "1",
            "started_at": st.meta_get("refresh_started_at"),
            "last_refresh_at": st.meta_get("last_refresh_at"),
            "last_ok": st.meta_get("last_refresh_ok") == "1",
        })


def main() -> int:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.web.bind, port=cfg.web.port, log_level="info", proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
