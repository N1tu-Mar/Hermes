"""Management screens in a real Chromium under the production CSP: navigation, campaigns, contacts, identities."""

import socket
import threading
import time

import pytest
import uvicorn

from app.api import create_app
from app.config import Config
from app.workspace import Workspace
from tests.conftest import make_service

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.browser
INTAKE = {"mode": "research", "subtype": "research_professor", "raw_request": "Neuro professors at Rutgers"}


@pytest.fixture
def server(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    svc = make_service(tmp_path / "data")
    svc.workspace = Workspace(svc.cache, svc.store.root)
    app = create_app(service=svc, token="browser-token", config=Config(port=port, shutdown_grace=0))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", svc
    srv.should_exit = True
    t.join(10)


@pytest.fixture
def page(server):
    base, _ = server
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.errors, pg.requests = [], []
        pg.on("console", lambda m: m.type == "error" and "fonts.g" not in m.text and pg.errors.append(m.text))
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("request", lambda r: pg.requests.append((r.method, r.url)))
        pg.goto(f"{base}/?t=browser-token")
        pg.wait_for_function("document.querySelector('#campaign-select')")
        yield pg
        browser.close()
    assert not pg.errors, pg.errors


def test_nav_screens_and_unique_ids(page):
    for name in ("campaigns", "contacts", "identities"):
        page.click(f".nav a[data-go={name}]")
        assert page.locator(f"#view-{name}").is_visible()
        assert page.locator(".view:visible").count() == 1
    page.click("#btn-library")
    page.click("#btn-analytics")
    page.click(".brand")
    assert page.locator("#view-home").is_visible()
    dupes = page.evaluate(
        "() => { const seen = {}; document.querySelectorAll('[id]').forEach(e => seen[e.id] = (seen[e.id] || 0) + 1);"
        " return Object.keys(seen).filter(k => seen[k] > 1); }"
    )
    assert not dupes
    assert "Outreach Desk" not in page.content() and page.title() == "HERMES"


def test_campaign_management(page, server):
    _, svc = server
    cid = svc.create(INTAKE, name="Alpha")
    page.click(".nav a[data-go=campaigns]")
    row = page.locator(f"#campaign-rows tr[data-key={cid}]")
    row.wait_for()

    row.locator("[data-act=rename]").click()
    page.fill("#input-dialog-field", "Beta")
    page.click("#input-dialog-ok")
    page.locator(f"#campaign-rows tr[data-key={cid}]", has_text="Beta").wait_for()
    assert page.evaluate("document.activeElement.dataset.act") == "rename"  # focus survived the re-render

    row.locator("[data-act=duplicate]").click()
    page.locator("#campaign-rows tr", has_text="Beta (copy)").wait_for()

    assert row.locator("[data-act=delete]").count() == 0  # active campaigns cannot be deleted
    row.locator("[data-act=archive]").click()
    row.wait_for(state="detached")
    page.select_option("#campaign-filters [name=status]", "archived")
    row.wait_for()
    row.locator("[data-act=archive]").click()  # restore
    row.wait_for(state="detached")
    page.select_option("#campaign-filters [name=status]", "active")
    row.wait_for()
    row.locator("[data-act=archive]").click()
    row.wait_for(state="detached")

    page.select_option("#campaign-filters [name=status]", "archived")
    row.locator("[data-act=delete]").click()
    assert page.locator("#input-dialog-ok").is_disabled()  # needs the typed id
    page.fill("#input-dialog-field", "wrong")
    assert page.locator("#input-dialog-ok").is_disabled()
    page.fill("#input-dialog-field", cid)
    page.click("#input-dialog-ok")
    row.wait_for(state="detached")
    assert not svc.store.exists(cid)
    assert not [r for r in page.requests if r[0] == "DELETE" and "/api/campaigns/" in r[1]]
    assert any(r[0] == "POST" and r[1].endswith(f"/api/campaigns/{cid}/delete") for r in page.requests)


def test_double_submit_is_blocked(page, server):
    _, svc = server
    cid = svc.create(INTAKE, name="Solo")
    page.click(".nav a[data-go=campaigns]")
    btn = page.locator(f"#campaign-rows tr[data-key={cid}] [data-act=duplicate]")
    btn.wait_for()
    btn.dblclick()
    page.locator("#campaign-rows tr", has_text="Solo (copy)").wait_for()
    time.sleep(0.5)
    assert len(svc.list(status="all")) == 2


def test_contacts_search_edit_timeline_and_review(page, server):
    _, svc = server
    svc.ledger.upsert_person({"name": "Ada Lovelace", "organization": "Analytical U", "email": "ada@example.edu"}, "manual")
    svc.ledger.upsert_person({"name": "Grace Hopper", "organization": "Navy", "email": "grace@example.mil"}, "manual")
    # Same email as Ada but another person's profile URL: needs a human decision.
    svc.ledger.upsert_person({"name": "A. Lovelace", "email": "ada@example.edu", "profile_url": "https://x.example/grace"}, "manual")
    svc.ledger.upsert_person({"name": "Grace H", "profile_url": "https://x.example/grace"}, "manual")
    svc.ledger.upsert_person({"name": "Grace Hopper", "organization": "Navy", "email": "ada@example.edu"}, "manual")

    page.click(".nav a[data-go=contacts]")
    page.locator("#contact-rows tr", has_text="Ada").wait_for()
    page.fill("#contact-filters [name=q]", "grace")
    page.locator("#contact-rows tr", has_text="Ada").wait_for(state="detached")

    page.locator("#contact-rows tr", has_text="Grace").first.click()
    form = page.locator("#contact-detail .contact-form")
    form.wait_for()
    form.locator("[name=notes]").fill("Met at the fireside chat")
    form.locator("[name=do_not_contact]").check()
    form.locator("button[type=submit]").click()
    page.locator("#toast", has_text="Contact saved").wait_for()
    assert page.locator("#contact-rows .dnc").count() == 1

    page.select_option("#contact-detail [name=kind]", "meeting")
    page.fill("#contact-detail .interaction-form [name=detail]", "Coffee chat")
    page.click("#contact-detail .interaction-form button")
    page.locator("#contact-detail .timeline li", has_text="Coffee chat").wait_for()

    reviews = svc.ledger.reviews()
    assert reviews, "fixture must have produced a duplicate review"
    assert page.locator("#reviews").is_visible()
    page.locator("#reviews [data-act=new]").first.click()
    page.wait_for_function(f"document.querySelectorAll('#reviews [data-key]').length < {len(reviews)} || document.querySelector('#reviews').hidden")
    assert len(svc.ledger.reviews()) == len(reviews) - 1


def test_identities_crud_and_intake_selection(page, server):
    page.click(".nav a[data-go=identities]")
    form = page.locator("#identity-form")
    form.locator("[name=display_name]").fill("Rutgers Club")
    form.locator("[name=biography]").fill("We run founder events at Rutgers.")
    form.locator("[name=reply_to]").fill("club@example.com")
    form.locator("button[type=submit]").click()
    card = page.locator("#identity-list .card", has_text="Rutgers Club")
    card.wait_for()

    card.locator("[data-act=edit]").click()
    form.locator("[name=signature]").fill("Best, The Club")
    form.locator("button[type=submit]").click()
    page.locator("#toast", has_text="Identity saved").wait_for()

    page.click(".brand")
    page.click("button.mode[data-mode=research]")
    page.fill("#ask-text", "Rutgers professors working on computational neurodevelopment")
    page.click("#ask-form button[type=submit]")
    select = page.locator("#intake-form [name=sender_identity_id]")
    select.locator("option", has_text="Rutgers Club").wait_for(state="attached")
    select.select_option(label="Rutgers Club")
    assert page.locator("#intake-form [data-when=custom]").is_hidden()
    page.click("#intake-form button[type=submit]")
    page.locator("#work-title").wait_for()
    cid = page.locator("#campaign-select").input_value()
    assert cid

    page.click(".nav a[data-go=identities]")
    card = page.locator("#identity-list .card", has_text="Rutgers Club")
    card.locator("[data-act=delete]").click()
    page.click("#confirm-dialog button[value=ok]")
    page.locator("#toast", has_text="used by").wait_for()  # still referenced by the campaign
    page.locator("#toast", has_text="used by").wait_for(state="hidden", timeout=6000)
    page.click(".nav a[data-go=campaigns]")
    page.click(f"#campaign-rows tr[data-key={cid}] [data-act=archive]")
    page.locator(f"#campaign-rows tr[data-key={cid}]").wait_for(state="detached")
