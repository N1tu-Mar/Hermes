"""HTTP routes: parse input, call CampaignService, return small DTOs.

Bound to 127.0.0.1. All /api routes need the app token (X-App-Token header),
Jupyter-style: the token is printed in the startup URL and saved to a 0600
file the MCP adapter reads. Host header is checked to block DNS rebinding.
"""
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import analytics, demo
from .cache import Cache
from .campaigns import CampaignService, Rejected, parse_request
from .storage import ROOT, CampaignStore
from .workspace import Workspace

APP_DIR = Path(__file__).resolve().parent
TOKEN_FILE = Path(os.path.expanduser("~/.config/outreach/app_token"))
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def app_token():
    tok = os.environ.get("APP_TOKEN") or secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok)
    os.chmod(TOKEN_FILE, 0o600)
    return tok


def build_service():
    load_env()
    port = int(os.environ.get("APP_PORT", 8765))
    data_root = Path(os.environ.get("DATA_ROOT") or "data")
    data_root = data_root if data_root.is_absolute() else ROOT / data_root
    store = CampaignStore(data_root)
    cache = Cache(data_root / "cache.sqlite3")
    if os.environ.get("OPENAI_API_KEY"):
        from .openai_client import OpenAIModel
        from .research import Fetcher
        model, fetcher = OpenAIModel(cache), Fetcher(cache)
    else:
        demo.set_base(port)
        model, fetcher = demo.DemoModel(cache), demo.demo_fetcher(cache)
    gmail, oauth_problem = None, None
    try:
        from .gmail import GmailDrafts, credentials_paths
        gmail = GmailDrafts.connect(interactive=False)
        if gmail is None and credentials_paths()[1].exists():
            oauth_problem = "saved Gmail token is no longer valid"
    except Exception as e:
        logging.warning("Gmail not connected: %s", e)
        oauth_problem = f"Gmail connection failed ({type(e).__name__})"
    if oauth_problem:  # only when Gmail was set up before; a fresh install without Gmail stays quiet
        analytics.notify(cache, f"oauth:{time.strftime('%Y-%m-%d')}", "oauth",
                         f"{oauth_problem}; run `python -m app.gmail` to reconnect")
    return CampaignService(store, cache, model, fetcher, gmail, Workspace(cache, data_root))


def create_app(service=None, token=None):
    state = {}

    @asynccontextmanager
    async def lifespan(app):
        state["svc"] = service or build_service()
        state["token"] = token or app_token()
        state["svc"].startup()
        port = os.environ.get("APP_PORT", 8765)
        mode = "DEMO MODE (fixture data)" if getattr(state["svc"].model, "demo", False) else f"model {state['svc'].model.model}"
        print(f"\n  Outreach assistant [{mode}] -> http://127.0.0.1:{port}/?t={state['token']}\n", flush=True)
        yield
        await state["svc"].jobs.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    svc = lambda: state["svc"]

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS and host != "testserver":
            return JSONResponse({"detail": "bad host"}, 403)
        if request.url.path.startswith("/api/") and not secrets.compare_digest(
                request.headers.get("x-app-token", ""), state.get("token", "")):
            return JSONResponse({"detail": "missing or bad app token"}, 401)
        return await call_next(request)

    for exc, code in ((KeyError, 404), (Rejected, 409), (ValueError, 400)):
        app.add_exception_handler(exc, lambda r, e, code=code: JSONResponse({"detail": str(e).strip("'")}, code))

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(APP_DIR / "templates" / "index.html")

    @app.get("/demo/pages/{slug}", response_class=HTMLResponse)
    def demo_page(slug: str):
        html = demo.page_html(slug)
        if html is None:
            raise HTTPException(503, "unreachable (demo failure case)")
        return html

    @app.get("/api/status")
    def status():
        s = svc()
        return {"demo": getattr(s.model, "demo", False), "model": s.model.model, "gmail_connected": s.gmail is not None}

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

    @app.get("/api/identities")
    def identities():
        return svc().workspace.list_identities()

    @app.post("/api/identities")
    def create_identity(body: dict = Body(...)):
        return svc().workspace.create_identity(body)

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

    @app.post("/api/parse")
    async def parse(body: dict = Body(...)):
        return await svc().parse_intake(body.get("text", ""), body.get("mode"), body.get("subtype"))

    @app.get("/api/campaigns")
    def list_campaigns():
        return svc().list()

    @app.post("/api/campaigns")
    def create(body: dict = Body(...)):
        cid = svc().create(body.get("intake") or {}, body.get("max_candidates", 20), body.get("budget", 60))
        return {"campaign_id": cid}

    @app.get("/api/campaigns/{cid}")
    def get(cid: str):
        return svc().get(cid)

    @app.patch("/api/campaigns/{cid}/intake")
    def patch_intake(cid: str, body: dict = Body(...)):
        return svc().update_intake(cid, body)

    @app.post("/api/campaigns/{cid}/discover")
    def discover(cid: str):
        return {"job_id": svc().discover(cid)}

    @app.post("/api/campaigns/{cid}/select")
    def select(cid: str, body: dict = Body(...)):
        return {"changed": svc().select(cid, body.get("candidate_ids") or [], body.get("action", "select"))}

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
    def research(cid: str, body: dict = Body(default={})):
        return svc().research(cid, body.get("candidate_ids"), bool(body.get("refresh")))

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
    def detail(cid: str, cand: str):
        return svc().detail(cid, cand)

    @app.patch("/api/campaigns/{cid}/drafts/{cand}")
    def edit(cid: str, cand: str, body: dict = Body(...)):
        return svc().edit_draft(cid, cand, str(body.get("subject", "")), str(body.get("body", "")))

    @app.post("/api/campaigns/{cid}/drafts/{cand}/approve")
    def approve(cid: str, cand: str):
        return svc().approve(cid, cand)

    @app.post("/api/campaigns/{cid}/drafts/{cand}/mark-invited")
    def mark_invited(cid: str, cand: str):
        return svc().mark_invited(cid, cand)

    @app.post("/api/campaigns/{cid}/drafts/{cand}/mark-contacted")
    def mark_contacted(cid: str, cand: str):
        return svc().mark_contacted(cid, cand)

    @app.post("/api/campaigns/{cid}/gmail-drafts")
    async def gmail_drafts(cid: str, body: dict = Body(...)):
        return {"results": await svc().create_gmail_drafts(cid, body.get("candidate_ids") or [])}

    @app.get("/api/campaigns/{cid}/export", response_class=PlainTextResponse)
    def export(cid: str):
        return svc().export(cid) or "(no approved drafts yet)"

    @app.get("/api/campaigns/{cid}/progress")
    def progress(cid: str):
        return svc().progress(cid)

    @app.get("/api/campaigns/{cid}/validate")
    def validate(cid: str):
        return {"problems": svc().store.validate(cid)}

    @app.post("/api/campaigns/{cid}/stop")
    def stop(cid: str):
        return svc().stop(cid)

    @app.post("/api/campaigns/{cid}/resume")
    def resume(cid: str):
        return svc().resume(cid)

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

    return app


def main():
    import uvicorn
    load_env()
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("APP_PORT", 8765)), log_level="warning")


if __name__ == "__main__":
    main()
