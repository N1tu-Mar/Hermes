"""HTTP routes: parse input, call CampaignService, return small DTOs.

Local mode (default): bound to 127.0.0.1. All /api routes need the app token
(X-App-Token header), Jupyter-style: the token is printed in the startup URL
and saved to a 0600 file the MCP adapter reads. Host header is checked to
block DNS rebinding.

Remote mode (HERMES_MODE=remote): HTTPS only behind a reverse proxy. Users log
in with a password and get a Secure/HttpOnly/SameSite=Strict session cookie;
every state-changing request also needs the per-session CSRF token. Each user
has a separate data root, SQLite database, job workers, and encrypted provider
credentials, so a request can only ever reach its own user's service. The
local app token is never read, written, or accepted in this mode.
"""
import fcntl
import logging
import os
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import cache as cache_mod
from . import contracts, demo, logs
from .cache import Cache
from .campaigns import CampaignService, Rejected, parse_request
from .config import Config, ConfigError, load_config, load_env
from .migrations import migrate_campaigns
from .storage import CampaignStore

APP_DIR = Path(__file__).resolve().parent
TOKEN_FILE = Path(os.path.expanduser("~/.config/outreach/app_token"))
LOCAL_HOSTS = {"127.0.0.1", "localhost", "testserver"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SESSION_COOKIE = "__Host-hermes_session"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
VERSION = "0.2.0"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

log = logging.getLogger("api")


def app_token(cfg):
    tok = cfg.app_token or secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok)
    os.chmod(TOKEN_FILE, 0o600)
    return tok


def build_service(cfg, data_root=None, openai_key=None, gmail=None):
    """One service over one data root. Local mode: the whole app. Remote mode: one per user."""
    data_root = Path(data_root or cfg.data_root)
    data_root.mkdir(parents=True, exist_ok=True)
    migrate_campaigns(data_root)
    store = CampaignStore(data_root)
    cache = Cache(data_root / "cache.sqlite3")
    key = openai_key or cfg.openai_api_key
    if key:
        from .openai_client import OpenAIModel
        from .research import Fetcher

        model, fetcher = OpenAIModel(cache, api_key=key, model=cfg.openai_model), Fetcher(cache)
    else:
        demo.set_base(cfg.public_url or f"http://127.0.0.1:{cfg.port}")
        model, fetcher = demo.DemoModel(cache), demo.demo_fetcher(cache)
    if gmail is None and not cfg.remote:
        try:
            from .gmail import GmailDrafts

            gmail = GmailDrafts.connect(interactive=False)
        except Exception as e:
            log.warning("Gmail not connected: %s", type(e).__name__)
    return CampaignService(store, cache, model, fetcher, gmail)


def _lock_data_root(root):
    """Hold an exclusive lock for the process lifetime: one writer per data root; restore refuses while held."""
    f = open(Path(root) / ".lock", "w")  # noqa: SIM115 - held open until shutdown
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError(f"another HERMES process is using {root}") from None
    return f


def create_app(service=None, token=None, config=None):
    cfg = config or Config()
    state = {"services": {}, "gmail_pending": {}, "started": time.time()}

    @asynccontextmanager
    async def lifespan(app):
        if cfg.remote:
            from .auth import Accounts

            state["lock"] = _lock_data_root(cfg.data_root)
            state["accounts"] = Accounts(cfg.data_root, cfg.secret_key)
            print(f"\n  HERMES [remote mode] -> {cfg.public_url}\n", flush=True)
        else:
            svc = service or build_service(cfg)
            state["lock"] = _lock_data_root(svc.store.root)
            state["svc"] = svc
            state["token"] = token or app_token(cfg)
            svc.startup()
            svc.cache.purge(cfg.retention_days)
            mode = "DEMO MODE (fixture data)" if getattr(svc.model, "demo", False) else f"model {svc.model.model}"
            print(f"\n  HERMES [{mode}] -> http://127.0.0.1:{cfg.port}/?t={state['token']}\n", flush=True)
        log.info("started", extra={"status": cfg.mode})
        yield
        log.info("shutting down")
        for svc in [state.get("svc"), *state["services"].values()]:
            if svc:
                await svc.shutdown(cfg.shutdown_grace)
        if "accounts" in state:
            state["accounts"].close()
        state["lock"].close()
        log.info("stopped")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

    # ------------------------------------------------------------ request guard
    def check(request):
        """Returns an error response, or None and sets request.state.user in remote mode."""
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        path = request.url.path
        if not cfg.remote:
            if host not in LOCAL_HOSTS:
                return JSONResponse({"detail": "bad host"}, 403)
            if path.startswith("/api/") and not secrets.compare_digest(
                request.headers.get("x-app-token", ""), state.get("token", "")
            ):
                return JSONResponse({"detail": "missing or bad app token"}, 401)
            return None
        if path in ("/healthz", "/readyz"):  # probes come from the proxy/orchestrator over plain HTTP
            return None
        if host != cfg.public_host:
            return JSONResponse({"detail": "bad host"}, 403)
        if request.url.scheme != "https":
            return JSONResponse({"detail": "HTTPS required"}, 400)
        unsafe = request.method not in SAFE_METHODS
        origin = request.headers.get("origin")
        if unsafe and origin and origin != cfg.public_url:
            return JSONResponse({"detail": "cross-origin request refused"}, 403)
        request.state.user = state["accounts"].session(request.cookies.get(SESSION_COOKIE))
        needs_session = path.startswith("/api/") or path in ("/auth/logout", "/auth/gmail/start")
        if needs_session and not request.state.user:
            return JSONResponse({"detail": "login required"}, 401)
        if unsafe and request.state.user and not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), request.state.user["csrf"]
        ):
            return JSONResponse({"detail": "missing or bad CSRF token"}, 403)
        return None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        rid = request.headers.get("x-request-id", "")
        rid = rid if REQUEST_ID_RE.match(rid) else secrets.token_hex(8)
        logs.request_id.set(rid)
        started = time.monotonic()
        response = check(request) or await call_next(request)
        h = response.headers
        h["X-Request-ID"] = rid
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Content-Security-Policy"] = CSP
        if request.url.path.startswith(("/api/", "/auth/")):
            h["Cache-Control"] = "no-store"
        if cfg.remote:
            h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if not request.url.path.startswith(("/static/", "/healthz", "/readyz", "/demo/")):
            user = getattr(request.state, "user", None)
            log.info("request", extra={"method": request.method, "path": request.url.path,
                                       "status": response.status_code,
                                       "duration_ms": int((time.monotonic() - started) * 1000),
                                       **({"user_id": user["user_id"]} if user else {})})
        return response

    for exc, code in ((KeyError, 404), (Rejected, 409), (ValueError, 400)):
        app.add_exception_handler(exc, lambda r, e, code=code: JSONResponse({"detail": str(e).strip("'")}, code))

    # ------------------------------------------------------------ per-request service
    def user_service(uid):
        acc = state["accounts"]
        gmail = None
        tok = acc.get_secret(uid, "gmail_token")
        if tok:
            try:
                from .gmail import GmailDrafts

                gmail = GmailDrafts.from_token_json(tok)
            except Exception as e:
                log.warning("stored Gmail token unusable: %s", type(e).__name__, extra={"user_id": uid})
        svc = build_service(cfg, acc.user_dir(uid), acc.get_secret(uid, "openai_api_key"), gmail)
        svc.startup()
        return svc

    async def current_service(request: Request):
        if not cfg.remote:
            return state["svc"]
        uid = request.state.user["user_id"]  # the guard guarantees a session on every /api route
        if uid not in state["services"]:  # runs on the event loop: no await between check and set
            state["services"][uid] = user_service(uid)
        return state["services"][uid]

    Svc = Annotated[CampaignService, Depends(current_service)]

    # ------------------------------------------------------------ public
    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(APP_DIR / "templates" / "index.html")

    @app.get("/demo/pages/{slug}", response_class=HTMLResponse)
    def demo_page(slug: str):
        html = demo.page_html(slug)
        if html is None:
            raise HTTPException(503, "unreachable (demo failure case)")
        return html

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        checks = {"data_root_writable": os.access(cfg.data_root if cfg.remote else state["svc"].store.root, os.W_OK)}
        if cfg.remote:
            checks["auth_db"] = bool(state["accounts"].q("SELECT 1 AS ok"))
        else:
            checks["cache_db"] = state["svc"].cache.ping()
            checks["workers"] = state["svc"].jobs.alive()
        ok = all(checks.values())
        return JSONResponse({"ready": ok, "checks": checks}, 200 if ok else 503)

    # ------------------------------------------------------------ remote accounts
    @app.get("/auth/session")
    def session(request: Request):
        if not cfg.remote:
            return {"mode": "local"}
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse({"mode": "remote", "user": None}, 401)
        return {"mode": "remote", "user": user["username"], "csrf": user["csrf"]}

    @app.post("/auth/login")
    def login(request: Request, body: dict = Body(...)):
        if not cfg.remote:
            raise HTTPException(404)
        if "application/json" not in request.headers.get("content-type", ""):
            raise HTTPException(415, "JSON body required")
        res = state["accounts"].login(str(body.get("username", "")), str(body.get("password", "")))
        if not res:
            return JSONResponse({"detail": "wrong username or password, or too many attempts"}, 401)
        tok, csrf, _ = res
        resp = JSONResponse({"mode": "remote", "user": body["username"], "csrf": csrf})
        resp.set_cookie(SESSION_COOKIE, tok, httponly=True, secure=True, samesite="strict", path="/",
                        max_age=7 * 86400)
        return resp

    @app.post("/auth/logout")
    def logout(request: Request):
        state["accounts"].logout(request.cookies.get(SESSION_COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return resp

    def _remote_user(request):
        if not cfg.remote:
            raise HTTPException(404)
        return request.state.user["user_id"]

    @app.get("/api/account")
    def account(request: Request, s: Svc):
        uid = _remote_user(request)
        return {"user": request.state.user["username"], "gmail_connected": s.gmail is not None,
                "openai_key_set": state["accounts"].get_secret(uid, "openai_api_key") is not None,
                "demo": getattr(s.model, "demo", False)}

    async def _reload_service(uid):
        old = state["services"].pop(uid, None)
        if old:
            await old.shutdown(cfg.shutdown_grace)

    @app.put("/api/account/openai-key")
    async def set_openai_key(request: Request, body: dict = Body(...)):
        uid = _remote_user(request)
        key = str(body.get("api_key", "")).strip()
        if not key.startswith("sk-") or len(key) < 20:
            raise ValueError("that does not look like an OpenAI API key")
        state["accounts"].put_secret(uid, "openai_api_key", key)
        await _reload_service(uid)
        return {"openai_key_set": True}

    @app.delete("/api/account/openai-key")
    async def delete_openai_key(request: Request):
        uid = _remote_user(request)
        state["accounts"].delete_secret(uid, "openai_api_key")
        await _reload_service(uid)
        return {"openai_key_set": False}

    @app.delete("/api/account/gmail")
    async def disconnect_gmail(request: Request):
        uid = _remote_user(request)
        state["accounts"].delete_secret(uid, "gmail_token")
        await _reload_service(uid)
        return {"gmail_connected": False}

    @app.get("/auth/gmail/start")
    def gmail_start(request: Request):
        uid = _remote_user(request)
        from .gmail import credentials_paths, web_flow

        if not credentials_paths()[0].exists():
            raise Rejected("the operator has not configured a Gmail OAuth client (GMAIL_CREDENTIALS)")
        flow = web_flow(cfg.public_url + "/auth/gmail/callback")
        url, st = flow.authorization_url(access_type="offline", prompt="consent")
        pending = state["gmail_pending"]
        for k in [k for k, v in pending.items() if time.time() - v[2] > 600]:
            pending.pop(k)
        pending[st] = (uid, getattr(flow, "code_verifier", None), time.time())
        return RedirectResponse(url, 303)

    @app.get("/auth/gmail/callback")
    async def gmail_callback(request: Request, code: str = ""):
        if not cfg.remote:
            raise HTTPException(404)
        st = request.query_params.get("state", "")
        # The state is single-use, 10-minute, and bound to the user who started the flow; the session cookie
        # (SameSite=Strict) is not sent on this cross-site redirect, so the state is what identifies the user.
        uid, verifier, at = state["gmail_pending"].pop(st, (None, None, 0))
        if not uid or time.time() - at > 600 or not code:
            raise HTTPException(400, "unknown or expired Gmail authorization; start again")
        from .gmail import web_flow

        flow = web_flow(cfg.public_url + "/auth/gmail/callback")
        if verifier:
            flow.code_verifier = verifier
        flow.fetch_token(code=code)
        state["accounts"].put_secret(uid, "gmail_token", flow.credentials.to_json())
        await _reload_service(uid)
        return RedirectResponse("/", 303)

    # ------------------------------------------------------------ campaigns
    @app.get("/api/status")
    def status(s: Svc):
        return {"demo": getattr(s.model, "demo", False), "model": s.model.model, "gmail_connected": s.gmail is not None}

    @app.get("/api/diagnostics")
    def diagnostics(s: Svc):
        return {"version": VERSION, "mode": cfg.mode, "python": sys.version.split()[0],
                "uptime_s": int(time.time() - state["started"]),
                "schema": {"sqlite": len(cache_mod.MIGRATIONS), "json": contracts.SCHEMA_VERSION},
                **s.diagnostics()}

    @app.post("/api/parse")
    def parse(body: dict = Body(...)):
        intake, missing = parse_request(body.get("text", ""), body.get("mode"), body.get("subtype"))
        return {"intake": intake, "question": missing}

    @app.get("/api/campaigns")
    def list_campaigns(s: Svc):
        return s.list()

    @app.post("/api/campaigns")
    def create(s: Svc, body: dict = Body(...)):
        cid = s.create(body.get("intake") or {}, body.get("max_candidates", 20), body.get("budget", 60))
        return {"campaign_id": cid}

    @app.get("/api/campaigns/{cid}")
    def get(cid: str, s: Svc):
        return s.get(cid)

    @app.delete("/api/campaigns/{cid}")
    def delete_campaign(cid: str, s: Svc):
        return s.delete_campaign(cid)

    @app.patch("/api/campaigns/{cid}/intake")
    def patch_intake(cid: str, s: Svc, body: dict = Body(...)):
        return s.update_intake(cid, body)

    @app.post("/api/campaigns/{cid}/discover")
    def discover(cid: str, s: Svc):
        return {"job_id": s.discover(cid)}

    @app.post("/api/campaigns/{cid}/select")
    def select(cid: str, s: Svc, body: dict = Body(...)):
        return {"changed": s.select(cid, body.get("candidate_ids") or [], body.get("action", "select"))}

    @app.post("/api/campaigns/{cid}/research")
    def research(cid: str, s: Svc, body: dict = Body(default={})):
        return s.research(cid, body.get("candidate_ids"), bool(body.get("refresh")))

    @app.post("/api/campaigns/{cid}/drafts/generate")
    def generate(cid: str, s: Svc, body: dict = Body(...)):
        return s.generate(cid, body.get("candidate_ids") or [], bool(body.get("followup")))

    @app.get("/api/campaigns/{cid}/candidates/{cand}")
    def detail(cid: str, cand: str, s: Svc):
        return s.detail(cid, cand)

    @app.delete("/api/campaigns/{cid}/candidates/{cand}")
    def delete_candidate(cid: str, cand: str, s: Svc):
        return s.delete_candidate(cid, cand)

    @app.patch("/api/campaigns/{cid}/drafts/{cand}")
    def edit(cid: str, cand: str, s: Svc, body: dict = Body(...)):
        return s.edit_draft(cid, cand, str(body.get("subject", "")), str(body.get("body", "")))

    @app.post("/api/campaigns/{cid}/drafts/{cand}/approve")
    def approve(cid: str, cand: str, s: Svc):
        return s.approve(cid, cand)

    @app.post("/api/campaigns/{cid}/drafts/{cand}/mark-invited")
    def mark_invited(cid: str, cand: str, s: Svc):
        return s.mark_invited(cid, cand)

    @app.post("/api/campaigns/{cid}/gmail-drafts")
    async def gmail_drafts(cid: str, s: Svc, body: dict = Body(...)):
        return {"results": await s.create_gmail_drafts(cid, body.get("candidate_ids") or [])}

    @app.get("/api/campaigns/{cid}/export", response_class=PlainTextResponse)
    def export(cid: str, s: Svc):
        return s.export(cid) or "(no approved drafts yet)"

    @app.get("/api/campaigns/{cid}/progress")
    def progress(cid: str, s: Svc):
        return s.progress(cid)

    @app.get("/api/campaigns/{cid}/validate")
    def validate(cid: str, s: Svc):
        return {"problems": s.store.validate(cid)}

    @app.post("/api/campaigns/{cid}/stop")
    def stop(cid: str, s: Svc):
        return s.stop(cid)

    @app.post("/api/campaigns/{cid}/resume")
    def resume(cid: str, s: Svc):
        return s.resume(cid)

    return app


def main():
    import uvicorn

    load_env()
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"HERMES cannot start: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    logs.setup(cfg.log_level)
    uvicorn.run(create_app(config=cfg), host=cfg.host, port=cfg.port, log_config=None, access_log=False,
                proxy_headers=cfg.remote, forwarded_allow_ips=cfg.trusted_proxies if cfg.remote else None,
                server_header=False)


if __name__ == "__main__":
    main()
