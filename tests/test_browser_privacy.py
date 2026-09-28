"""Remote mode in a real Chromium: one user's saved browser state must never reach another user's session."""

import datetime
import socket
import threading
import time

import pytest
import uvicorn
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.api import create_app
from app.auth import Accounts
from app.config import Config

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.browser
PW = {"alice": "alice-password-123", "bob": "bob-password-4567"}
SECRET = "Alice's private biography: she directs Project Nightjar."


def self_signed(dirpath):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    (dirpath / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (dirpath / "k.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
    )
    return str(dirpath / "c.pem"), str(dirpath / "k.pem")


@pytest.fixture
def remote(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = Config(
        mode="remote",
        data_root=tmp_path / "data",
        public_url=f"https://localhost:{port}",
        secret_key=Fernet.generate_key().decode(),
        shutdown_grace=0,
    )
    cfg.data_root.mkdir()
    acc = Accounts(cfg.data_root, cfg.secret_key)
    for name, pw in PW.items():
        acc.add_user(name, pw)
    acc.close()
    cert, key = self_signed(tmp_path)
    app = create_app(config=cfg)
    srv = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ssl_certfile=cert, ssl_keyfile=key)
    )
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    yield cfg.public_url
    srv.should_exit = True
    t.join(10)


def sign_in(page, name):
    page.fill("#login-form [name=username]", name)
    page.fill("#login-form [name=password]", PW[name])
    page.click("#login-form button[type=submit]")
    page.locator("#view-home").wait_for()


def to_intake(page):
    page.click("button.mode[data-mode=research]")
    page.fill("#ask-text", "Rutgers professors working on computational neurodevelopment")
    page.click("#ask-form button[type=submit]")
    page.locator("#intake-form").wait_for()


def test_alice_state_never_reaches_bob(remote):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        errors = []
        # The anonymous /auth/session probe answers 401 by design (server-owned); Chromium logs that as a console error.
        page.on(
            "console",
            lambda m: (
                m.type == "error"
                and "fonts.g" not in m.text
                and not m.location["url"].endswith("/auth/session")
                and errors.append(m.text)
            ),
        )
        page.on("pageerror", lambda e: errors.append(str(e)))

        # A value written by an older build (or another tab) under the old origin-wide key must not survive sign-in.
        page.goto(f"{remote}/")
        page.evaluate("t => localStorage.setItem('senderBackground', t)", SECRET)

        sign_in(page, "alice")
        to_intake(page)
        page.fill("#intake-form [name=sender_background]", SECRET)
        assert page.locator("#intake-form [name=sender_background]").input_value() == SECRET
        page.click("#intake-form button[type=submit]")  # submitting used to persist it to localStorage
        page.locator("#work-title").wait_for()
        page.click("#account summary")
        page.click("#btn-logout")
        page.locator("#view-login").wait_for()

        sign_in(page, "bob")
        to_intake(page)
        assert page.locator("#intake-form [name=sender_background]").input_value() == ""
        stored = page.evaluate("JSON.stringify([localStorage, sessionStorage])")
        assert "Nightjar" not in stored
        browser.close()
    assert not errors, errors
