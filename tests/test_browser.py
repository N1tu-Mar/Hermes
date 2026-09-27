"""Critical workflow in a real browser against a live server (demo mode):
request -> intake -> discovery -> research -> drafts -> edit -> approve -> export, under the production CSP."""

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


@pytest.mark.browser
def test_critical_workflow_in_browser(server):
    base, svc = server
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("console", lambda m: m.type == "error" and "fonts.g" not in m.text and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(f"{base}/")
        assert page.locator("#token-warning").is_visible()  # no token: nothing usable

        page.goto(f"{base}/?t=browser-token")
        assert "t=" not in page.url  # token stripped from the address bar
        page.click("button.mode[data-mode=research]")
        page.fill("#ask-text", "Rutgers/Princeton professors working on computational neurodevelopment")
        page.click("#ask-form button[type=submit]")
        page.fill(
            "#intake-form [name=sender_background]", "I'm Nitu, a Rutgers undergraduate studying cognitive science."
        )
        page.click("#intake-form button[type=submit]")

        rows = page.locator("#rows tr[data-id]")
        rows.nth(2).wait_for()
        assert rows.count() == 3
        page.check("#check-all")
        page.click("#btn-research")
        page.locator("#rows td.status", has_text="done").first.wait_for()
        page.wait_for_function("!document.querySelector('#rows').innerText.includes('researching')")
        page.click("#btn-generate")
        page.locator("#rows td.status", has_text="needs review").nth(1).wait_for()

        rows.first.click()
        body = page.locator("#detail textarea[name=body]")
        body.wait_for()
        body.fill(body.input_value() + "\nThank you for your time!")
        page.click("#detail .draft-form button[type=submit]")
        page.locator("#toast", has_text="Saved").wait_for()
        page.click("#detail [data-act=approve]")
        page.locator("#toast", has_text="Approved").wait_for()
        page.locator("#rows td.status", has_text="approved").wait_for()

        with page.expect_download() as dl:
            page.click("#btn-export")
        text = open(dl.value.path()).read()
        assert "Thank you for your time!" in text and "Subject:" in text
        browser.close()
    assert not errors, errors
