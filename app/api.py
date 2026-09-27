"""HTTP routes: parse input, call CampaignService, return small DTOs.

Bound to 127.0.0.1. All /api routes need the app token (X-App-Token header),
Jupyter-style: the token is printed in the startup URL and saved to a 0600
file the MCP adapter reads. Host header is checked to block DNS rebinding.
"""
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import demo
from .cache import Cache
from .campaigns import CampaignService, Rejected, parse_request
from .storage import ROOT, CampaignStore

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
    gmail = None
    try:
        from .gmail import GmailDrafts
        gmail = GmailDrafts.connect(interactive=False)
    except Exception as e:
        logging.warning("Gmail not connected: %s", e)
    return CampaignService(store, cache, model, fetcher, gmail)


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

    @app.post("/api/parse")
    def parse(body: dict = Body(...)):
        intake, missing = parse_request(body.get("text", ""), body.get("mode"), body.get("subtype"))
        return {"intake": intake, "question": missing}

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

    @app.post("/api/campaigns/{cid}/research")
    def research(cid: str, body: dict = Body(default={})):
        return svc().research(cid, body.get("candidate_ids"), bool(body.get("refresh")))

    @app.post("/api/campaigns/{cid}/drafts/generate")
    def generate(cid: str, body: dict = Body(...)):
        return svc().generate(cid, body.get("candidate_ids") or [], bool(body.get("followup")))

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

    return app


def main():
    import uvicorn
    load_env()
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("APP_PORT", 8765)), log_level="warning")


if __name__ == "__main__":
    main()
