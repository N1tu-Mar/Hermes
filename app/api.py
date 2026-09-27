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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
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
    def get(cid: str):
        return svc().get(cid)

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
