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
import contextvars
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import analytics, cache as cache_mod, contracts, demo, logs
from .cache import Cache
from .campaigns import CampaignService, Rejected, parse_request
from .config import Config, ConfigError, load_config, load_env
from .migrations import migrate_campaigns
from .sending import Blocked
from .storage import CampaignStore
from .workspace import Workspace

APP_DIR = Path(__file__).resolve().parent
TOKEN_FILE = Path(os.path.expanduser("~/.config/outreach/app_token"))
LOCAL_HOSTS = {"127.0.0.1", "localhost", "testserver"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SESSION_COOKIE = "__Host-hermes_session"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
VERSION = "0.2.0"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

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
    gmail_error = None
    if gmail is None and not cfg.remote:
        try:
            from .gmail import GmailDrafts, credentials_paths

            gmail = GmailDrafts.connect(interactive=False)
            if gmail is None and credentials_paths()[1].exists():
                gmail_error = "saved Gmail token is no longer valid"
        except Exception as e:
            log.warning("Gmail not connected: %s", type(e).__name__)
            gmail_error = str(e) or f"Gmail connection failed ({type(e).__name__})"
    if gmail_error:  # only when Gmail was set up before; a fresh install without Gmail stays quiet
        analytics.notify(cache, f"oauth:{time.strftime('%Y-%m-%d')}", "oauth",
                         f"{gmail_error}; run `python -m app.gmail` to reconnect")
    svc = CampaignService(store, cache, model, fetcher, gmail, Workspace(cache, data_root))
    svc.outreach.gmail_error = gmail_error
    return svc


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
    active_service = contextvars.ContextVar("active_service", default=None)

    def svc():
        current = active_service.get()
        if current is not None:
            return current
        return state.get("svc")

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
        if (
            unsafe
            and request.state.user
            and not secrets.compare_digest(request.headers.get("x-csrf-token", ""), request.state.user["csrf"])
        ):
            return JSONResponse({"detail": "missing or bad CSRF token"}, 403)
        return None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        rid = request.headers.get("x-request-id", "")
        rid = rid if REQUEST_ID_RE.match(rid) else secrets.token_hex(8)
        logs.request_id.set(rid)
        started = time.monotonic()
        problem = check(request)
        if not problem and request.url.path.startswith("/api/"):
            active_service.set(await current_service(request))
        response = problem or await call_next(request)
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
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    **({"user_id": user["user_id"]} if user else {}),
                },
            )
        return response

    for exc, code in ((KeyError, 404), (Rejected, 409), (Blocked, 409), (ValueError, 400)):
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
        resp.set_cookie(SESSION_COOKIE, tok, httponly=True, secure=True, samesite="strict", path="/", max_age=7 * 86400)
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
        return {
            "user": request.state.user["username"],
            "gmail_connected": s.gmail is not None,
            "openai_key_set": state["accounts"].get_secret(uid, "openai_api_key") is not None,
            "demo": getattr(s.model, "demo", False),
        }

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
    def status():
        s = svc()
        return {"demo": getattr(s.model, "demo", False), "model": s.model.model,
                **s.gmail_status(), "sending": s.outbox.status()}

    # Reusable messaging workspace. Template versions are immutable snapshots.
    @app.get("/api/templates")
    def templates(include_archived: bool = False):
        return svc().workspace.list_templates(include_archived)

    @app.post("/api/templates")
    def create_template(body: dict = Body(...)):
        return svc().workspace.create_template(body)

    @app.get("/api/templates/{tid}")
    def template(tid: str, version: int | None = None):
        return svc().workspace.get_template(tid, version)

    @app.put("/api/templates/{tid}")
    def edit_template(tid: str, body: dict = Body(...)):
        return svc().workspace.edit_template(tid, body)

    @app.get("/api/templates/{tid}/history")
    def template_history(tid: str):
        return svc().workspace.history(tid)

    @app.post("/api/templates/{tid}/preview")
    def preview_template(tid: str, body: dict = Body(...)):
        return svc().workspace.preview_template(tid, body.get("version"), body.get("values") or {})

    @app.post("/api/templates/{tid}/duplicate")
    def duplicate_template(tid: str, body: dict = Body(default={})):
        return svc().workspace.duplicate_template(tid, body)

    @app.post("/api/templates/{tid}/archive")
    def archive_template(tid: str, body: dict = Body(default={})):
        return svc().workspace.archive_template(tid, body.get("archived", True))

    @app.get("/api/content")
    def content(identity_id: int | None = None):
        return svc().workspace.list_content(identity_id)

    @app.post("/api/content")
    def create_content(body: dict = Body(...)):
        return svc().workspace.create_content(body)

    @app.get("/api/attachments")
    def attachments(identity_id: int | None = None):
        return svc().workspace.list_attachments(identity_id)

    @app.post("/api/attachments")
    def add_attachment(body: dict = Body(...)):
        return svc().workspace.add_attachment(body)

    @app.get("/api/policies")
    def policies():
        return svc().workspace.policies()

    @app.get("/api/diagnostics")
    def diagnostics(s: Svc):
        return {
            "version": VERSION,
            "mode": cfg.mode,
            "python": sys.version.split()[0],
            "uptime_s": int(time.time() - state["started"]),
            "schema": {"sqlite": len(cache_mod.MIGRATIONS), "json": contracts.SCHEMA_VERSION},
            **s.diagnostics(),
        }

    @app.post("/api/parse")
    async def parse(body: dict = Body(...)):
        return await svc().parse_intake(body.get("text", ""), body.get("mode"), body.get("subtype"))

    def csv_response(text, filename):
        return Response(text, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/campaigns")
    def list_campaigns(q: str = "", status: str = "active", subtype: str = ""):
        return svc().list(q, status, subtype or None)

    @app.post("/api/campaigns")
    def create(body: dict = Body(...)):
        cid = svc().create(body.get("intake") or {}, body.get("max_candidates", 20), body.get("budget", 60), body.get("name"))
        return {"campaign_id": cid}

    @app.get("/api/campaigns/{cid}")
    def get(cid: str, s: Svc):
        return s.get(cid)

    @app.delete("/api/campaigns/{cid}")
    def delete_campaign(cid: str, s: Svc):
        return s.delete_campaign(cid)

    @app.patch("/api/campaigns/{cid}")
    def patch_campaign(cid: str, body: dict = Body(...)):
        if "name" in body:
            svc().rename(cid, body["name"])
        if "archived" in body:
            svc().archive(cid, bool(body["archived"]))
        return svc().ledger.campaign_meta(cid)

    @app.post("/api/campaigns/{cid}/duplicate")
    def duplicate(cid: str, body: dict = Body(default={})):
        return {"campaign_id": svc().duplicate(cid, body.get("name"))}

    @app.post("/api/campaigns/{cid}/delete")
    def delete(cid: str, body: dict = Body(default={})):
        return svc().delete(cid, body.get("confirm"))

    @app.get("/api/campaigns/{cid}/export.csv")
    def export_campaign_csv(cid: str):
        return csv_response(svc().export_campaign_csv(cid), f"{cid}-candidates.csv")

    @app.post("/api/campaigns/{cid}/import")
    def import_candidates(cid: str, body: dict = Body(...)):
        return svc().import_csv("candidates", body.get("csv", ""), cid, bool(body.get("commit")))

    # contacts --------------------------------------------------------------
    @app.get("/api/contacts")
    def list_contacts(q: str = "", tag: str = "", dnc: str = "", relationship: str = ""):
        return svc().ledger.contacts(q or None, tag or None, {"yes": True, "no": False}.get(dnc), relationship or None)

    @app.post("/api/contacts")
    def create_contact(body: dict = Body(...)):
        cid, result = svc().ledger.upsert_person(body, source=body.get("source") or "manual")
        return {"contact_id": cid, "result": result}

    @app.get("/api/contacts/export.csv")
    def export_contacts_csv():
        return csv_response(svc().export_contacts_csv(), "hermes-contacts.csv")

    @app.post("/api/contacts/import")
    def import_contacts(body: dict = Body(...)):
        return svc().import_csv("contacts", body.get("csv", ""), commit=bool(body.get("commit")))

    @app.get("/api/contacts/{contact_id}")
    def get_contact(contact_id: int):
        return svc().ledger.contact_detail(contact_id)

    @app.patch("/api/contacts/{contact_id}")
    def patch_contact(contact_id: int, body: dict = Body(...)):
        svc().ledger.update_contact(contact_id, body)
        return svc().ledger.contact_detail(contact_id)

    @app.post("/api/contacts/{contact_id}/interactions")
    def add_interaction(contact_id: int, body: dict = Body(...)):
        return svc().log_interaction(contact_id, body.get("kind"), str(body.get("detail") or ""), body.get("at"))

    @app.get("/api/contact-reviews")
    def contact_reviews():
        return svc().ledger.reviews()

    @app.post("/api/contact-reviews/{review_id}/resolve")
    def resolve_review(review_id: int, body: dict = Body(default={})):
        return svc().ledger.resolve_review(review_id, body.get("contact_id"))

    # sender identities ----------------------------------------------------
    @app.get("/api/identities")
    def list_identities():
        return svc().ledger.identities()

    @app.post("/api/identities")
    def create_identity(body: dict = Body(...)):
        return svc().ledger.identity(svc().ledger.create_identity(body))

    @app.patch("/api/identities/{identity_id}")
    def patch_identity(identity_id: int, body: dict = Body(...)):
        return svc().ledger.update_identity(identity_id, body)

    @app.delete("/api/identities/{identity_id}")
    def delete_identity(identity_id: int):
        return svc().delete_identity(identity_id)

    @app.patch("/api/campaigns/{cid}/intake")
    def patch_intake(cid: str, s: Svc, body: dict = Body(...)):
        return s.update_intake(cid, body)

    @app.post("/api/campaigns/{cid}/discover")
    def discover(cid: str, s: Svc):
        return {"job_id": s.discover(cid)}

    @app.post("/api/campaigns/{cid}/select")
    def select(cid: str, s: Svc, body: dict = Body(...)):
        return {"changed": s.select(cid, body.get("candidate_ids") or [], body.get("action", "select"))}

    @app.patch("/api/campaigns/{cid}/ranking")
    def ranking(cid: str, body: dict = Body(...)):
        return svc().configure_ranking(cid, body.get("weights") or {})

    @app.patch("/api/campaigns/{cid}/candidates/{cand}/ranking")
    def candidate_ranking(cid: str, cand: str, body: dict = Body(...)):
        return svc().adjust_candidate(cid, cand, body.get("pinned"), body.get("manual_score_adjustment"))

    @app.patch("/api/campaigns/{cid}/candidates/{cand}/corrections")
    def correct(cid: str, cand: str, body: dict = Body(...)):
        return svc().correct(cid, cand, str(body.get("field", "")), body.get("value"))

    @app.post("/api/campaigns/{cid}/compare")
    def compare(cid: str, body: dict = Body(...)):
        return svc().compare(cid, body.get("candidate_ids") or [])

    @app.post("/api/campaigns/{cid}/research")
    def research(cid: str, s: Svc, body: dict = Body(default={})):
        return s.research(cid, body.get("candidate_ids"), bool(body.get("refresh")))

    @app.post("/api/campaigns/{cid}/drafts/generate")
    def generate(cid: str, body: dict = Body(...)):
        return svc().generate(cid, body.get("candidate_ids") or [], bool(body.get("followup")),
                              body.get("template_id"), body.get("template_version"))

    @app.get("/api/campaigns/{cid}/assets")
    def assets(cid: str):
        svc()._require(cid)
        return {"attachment_ids": svc().workspace.asset_ids(cid, "attachment"),
                "content_ids": svc().workspace.asset_ids(cid, "content"),
                "attachments": svc().workspace.campaign_attachments(cid),
                "content": svc().workspace.campaign_content(cid)}

    @app.put("/api/campaigns/{cid}/assets")
    def set_assets(cid: str, body: dict = Body(...)):
        svc()._require(cid)
        return {"attachment_ids": svc().workspace.set_assets(cid, "attachment", body.get("attachment_ids") or []),
                "content_ids": svc().workspace.set_assets(cid, "content", body.get("content_ids") or [])}

    @app.get("/api/campaigns/{cid}/rules")
    def rules(cid: str):
        svc()._require(cid)
        return svc().workspace.list_rules(cid)

    @app.post("/api/campaigns/{cid}/rules")
    def create_rule(cid: str, body: dict = Body(...)):
        svc()._require(cid)
        return svc().workspace.create_rule(cid, body)

    @app.post("/api/campaigns/{cid}/rules/{rid}/run")
    def run_rule(cid: str, rid: str, body: dict = Body(default={})):
        return svc().run_rule(cid, rid, bool(body.get("dry_run", True)))

    @app.put("/api/campaigns/{cid}/candidates/{cand}/do-not-contact")
    def do_not_contact(cid: str, cand: str, body: dict = Body(...)):
        svc()._require(cid)
        candidate = next((c for c in svc().store.candidates(cid)["candidates"] if c["candidate_id"] == cand), None)
        if not candidate: raise KeyError(cand)
        profile = svc().store.research(cid)["profiles"].get(cand) or {}
        return svc().workspace.set_do_not_contact(cid, cand, candidate, profile,
                                                   bool(body.get("do_not_contact", True)), body.get("reason"))

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

    @app.post("/api/campaigns/{cid}/drafts/{cand}/mark-contacted")
    def mark_contacted(cid: str, cand: str):
        return svc().mark_contacted(cid, cand)

    @app.post("/api/campaigns/{cid}/gmail-drafts")
    async def gmail_drafts(cid: str, s: Svc, body: dict = Body(...)):
        return {"results": await s.create_gmail_drafts(cid, body.get("candidate_ids") or [])}

    @app.post("/api/campaigns/{cid}/gmail-sync")
    async def gmail_sync(cid: str):
        return await svc().sync_now(cid)

    @app.post("/api/campaigns/{cid}/contacts/{cand}/outcome")
    def set_outcome(cid: str, cand: str, body: dict = Body(...)):
        return svc().set_outcome(cid, cand, body.get("outcome"), body.get("note") or "")

    @app.post("/api/campaigns/{cid}/contacts/{cand}/sequence")
    def sequence(cid: str, cand: str, body: dict = Body(...)):
        return svc().sequence(cid, cand, body.get("action"))

    @app.get("/api/campaigns/{cid}/followups")
    def followups(cid: str):
        return svc().followups(cid)

    @app.post("/api/campaigns/{cid}/followups/{cand}/{step}/{action}")
    async def followup_action(cid: str, cand: str, step: int, action: str, body: dict = Body(default={})):
        return await svc().followup_action(cid, cand, step, action, body)

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

    # Outcomes are recorded by the user; HERMES never reads the inbox or tracks opens.
    @app.post("/api/campaigns/{cid}/candidates/{cand}/outcomes")
    def record_outcome(cid: str, cand: str, body: dict = Body(...)):
        return {"timeline": svc().record_outcome(cid, cand, body.get("outcome"), body.get("at"))}

    @app.delete("/api/campaigns/{cid}/candidates/{cand}/outcomes/{outcome}")
    def delete_outcome(cid: str, cand: str, outcome: str):
        return {"timeline": svc().delete_outcome(cid, cand, outcome)}

    @app.get("/api/analytics")
    def analytics_report(campaign_id: str | None = None, start: str | None = None, end: str | None = None,
                         group_by: str = "campaign"):
        return analytics.report(svc(), campaign_id or None, start or None, end or None, group_by)

    @app.get("/api/analytics/export")
    def analytics_export(kind: str = "aggregate", campaign_id: str | None = None, start: str | None = None,
                         end: str | None = None, group_by: str = "campaign"):
        rep = analytics.report(svc(), campaign_id or None, start or None, end or None, group_by)
        name = f"hermes-{kind}-{campaign_id or 'all'}-{start or 'start'}-{end or 'now'}.csv"
        return Response(analytics.to_csv(rep, kind), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/notifications")
    def notifications(include_dismissed: bool = False):
        analytics.followups_due(svc().cache)  # evaluated lazily on read; no scheduler needed
        return analytics.notifications(svc().cache, include_dismissed)

    @app.post("/api/notifications/read-all")
    def read_all():
        svc().cache.x("UPDATE notifications SET read_at=? WHERE read_at IS NULL", (time.time(),))
        return {"ok": True}

    @app.post("/api/notifications/{nid}/{action}")
    def notification_action(nid: int, action: str):
        if action not in ("read", "dismiss"):
            raise HTTPException(404, "unknown action")
        analytics.mark(svc().cache, nid, "read_at" if action == "read" else "dismissed_at")
        return {"ok": True}

    # ------------------------------------------------------------ sending (opt-in, off by default)
    @app.get("/api/sending")
    def sending_status():
        return {**svc().outbox.status(), "audit": svc().outbox.global_audit(20)}

    @app.patch("/api/sending/settings")
    def sending_settings(body: dict = Body(...)):
        return svc().outbox.update_settings(body)

    @app.post("/api/sending/pause")
    def sending_pause():
        return svc().outbox.pause(True)

    @app.post("/api/sending/unpause")
    def sending_unpause():
        return svc().outbox.pause(False)

    @app.post("/api/sending/emergency-stop")
    def sending_emergency_stop():
        return svc().outbox.emergency_stop()

    @app.get("/api/suppressions")
    def suppressions():
        return svc().outbox.suppressions()

    @app.post("/api/suppressions")
    def suppress(body: dict = Body(...)):
        return svc().outbox.suppress(body.get("email"), body.get("reason", "do_not_contact"))

    @app.patch("/api/campaigns/{cid}/sending")
    def campaign_sending(cid: str, body: dict = Body(...)):
        svc()._require(cid)
        return svc().outbox.set_campaign_enabled(cid, bool(body.get("enabled")))

    @app.post("/api/campaigns/{cid}/sends/preview")
    def send_preview(cid: str, body: dict = Body(...)):
        return svc().preview_send(cid, str(body.get("candidate_id", "")), body.get("scheduled_at") or None)

    @app.post("/api/campaigns/{cid}/sends")
    def send_confirm(cid: str, body: dict = Body(...)):
        return svc().confirm_send(cid, str(body.get("candidate_id", "")), str(body.get("approval_hash", "")),
                                  body.get("scheduled_at") or None)

    @app.get("/api/campaigns/{cid}/sends")
    def send_list(cid: str):
        svc()._require(cid)
        return svc().outbox.list(cid)

    @app.get("/api/sends/{send_id}")
    def send_detail(send_id: str):
        return svc().outbox.detail(send_id)

    @app.post("/api/sends/{send_id}/cancel")
    def send_cancel(send_id: str):
        return svc().outbox.view(svc().outbox.cancel(send_id))

    @app.post("/api/sends/{send_id}/outcome")
    def send_outcome(send_id: str, body: dict = Body(...)):
        return svc().outbox.view(svc().outbox.record_outcome(send_id, body.get("outcome")))

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
    uvicorn.run(
        create_app(config=cfg),
        host=cfg.host,
        port=cfg.port,
        log_config=None,
        access_log=False,
        proxy_headers=cfg.remote,
        forwarded_allow_ips=cfg.trusted_proxies if cfg.remote else None,
        server_header=False,
    )


if __name__ == "__main__":
    main()
