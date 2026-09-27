"""Remote mode: accounts, sessions, CSRF, HTTPS, and strict per-user isolation."""
import sqlite3

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import api
from app.auth import Accounts
from app.config import Config
from tests.support import BIO, idle, run_research_flow, wait

URL = "https://hermes.test"
PW = {"alice": "alice-password-123", "bob": "bob-password-4567"}
ALICE_KEY = "sk-alice-0123456789abcdefghij"  # gitleaks:allow (fake fixture)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    token_file = tmp_path / "app_token"
    monkeypatch.setattr(api, "TOKEN_FILE", token_file)
    cfg = Config(mode="remote", data_root=tmp_path / "data", public_url=URL, secret_key=Fernet.generate_key().decode(),
                 shutdown_grace=0.5)
    cfg.data_root.mkdir()
    acc = Accounts(cfg.data_root, cfg.secret_key)
    for name, pw in PW.items():
        acc.add_user(name, pw)
    acc.close()
    app = api.create_app(config=cfg)
    with TestClient(app, base_url=URL) as anon:
        yield app, cfg, anon, token_file


class As:
    """One user's browser: requests go through the lifespan client (one event loop) with that user's cookie."""

    def __init__(self, client, name):
        r = client.post("/auth/login", json={"username": name, "password": PW[name]})
        assert r.status_code == 200, r.text
        self.client = client
        self.headers = {"x-csrf-token": r.json()["csrf"],
                        "cookie": f"{api.SESSION_COOKIE}={client.cookies[api.SESSION_COOKIE]}"}
        client.cookies.clear()

    def request(self, method, url, headers=None, **kw):
        return self.client.request(method, url, headers={**self.headers, **(headers or {})}, **kw)

    def __getattr__(self, method):
        return lambda url, **kw: self.request(method.upper(), url, **kw)


def login(anon, name):
    return As(anon, name)


def campaign(c):
    parsed = c.post("/api/parse", json={"text": "Rutgers/Princeton professors working on computational neurodevelopment"}).json()
    body = {"intake": {**parsed["intake"], "sender_background": BIO}, "max_candidates": 20, "budget": 40}
    return c.post("/api/campaigns", json=body).json()["campaign_id"]


def test_login_session_cookie_and_logout(remote):
    app, cfg, anon, token_file = remote
    assert anon.get("/api/campaigns").status_code == 401
    assert anon.get("/auth/session").json() == {"mode": "remote", "user": None}
    assert anon.post("/auth/login", json={"username": "alice", "password": "wrong-password"}).status_code == 401
    r = anon.post("/auth/login", json={"username": "alice", "password": PW["alice"]})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-hermes_session=")
    for attr in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
        assert attr.lower() in cookie.lower(), attr
    assert "strict-transport-security" in r.headers
    csrf = r.json()["csrf"]
    assert anon.get("/auth/session").json()["user"] == "alice"
    assert anon.post("/auth/logout").status_code == 403  # CSRF required even to log out
    assert anon.post("/auth/logout", headers={"x-csrf-token": csrf}).status_code == 200
    assert anon.get("/api/campaigns").status_code == 401


def test_local_app_token_is_never_remote_auth(remote, monkeypatch):
    app, cfg, anon, token_file = remote
    assert not token_file.exists()  # remote mode never writes the local token file
    for tok in ("t", "", "anything"):
        assert anon.get("/api/campaigns", headers={"x-app-token": tok}).status_code == 401


def test_csrf_origin_https_and_host(remote):
    app, cfg, anon, _ = remote
    c = login(anon, "alice")
    assert c.post("/api/parse", json={"text": "x"}, headers={"x-csrf-token": "forged"}).status_code == 403
    assert c.post("/api/parse", json={"text": "x"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/parse", json={"text": "x"}, headers={"origin": URL}).status_code == 200
    http = TestClient(app, base_url="http://hermes.test")
    assert http.get("/").status_code == 400  # HTTPS only
    assert http.get("/healthz").status_code == 200  # probes allowed from the proxy
    assert TestClient(app, base_url="https://evil.test").get("/").status_code == 403
    assert anon.post("/auth/login", content="username=alice", headers={"content-type": "text/plain"}).status_code in (415, 422)


def test_lockout_and_disabled_users(remote):
    app, cfg, anon, _ = remote
    for _ in range(5):
        anon.post("/auth/login", json={"username": "bob", "password": "nope-nope-nope"})
    assert anon.post("/auth/login", json={"username": "bob", "password": PW["bob"]}).status_code == 401
    c = login(anon, "alice")
    acc = Accounts(cfg.data_root, cfg.secret_key)
    acc.set_disabled("alice")
    acc.close()
    assert c.get("/api/campaigns").status_code == 401


def test_users_are_isolated(remote):
    app, cfg, anon, _ = remote
    alice, bob = login(anon, "alice"), login(anon, "bob")
    cid = campaign(alice)
    ids = run_research_flow(alice, cid)
    assert alice.post(f"/api/campaigns/{cid}/drafts/{ids[0]}/approve").status_code == 200

    # every campaign-scoped route: bob gets 404, never alice's data
    routes = [("get", ""), ("get", "/progress"), ("get", "/validate"), ("get", "/export"),
              ("get", f"/candidates/{ids[0]}"), ("patch", "/intake"), ("post", "/discover"), ("post", "/select"),
              ("post", "/research"), ("post", "/drafts/generate"), ("patch", f"/drafts/{ids[0]}"),
              ("post", f"/drafts/{ids[0]}/approve"), ("post", f"/drafts/{ids[0]}/mark-invited"),
              ("post", "/gmail-drafts"), ("post", "/stop"), ("post", "/resume"),
              ("delete", f"/candidates/{ids[0]}"), ("delete", "")]
    for method, suffix in routes:
        kw = {"json": {"candidate_ids": ids, "subject": "x", "body": "x"}} if method in ("post", "patch") else {}
        r = getattr(bob, method)(f"/api/campaigns/{cid}{suffix}", **kw)
        assert r.status_code == 404, (method, suffix, r.status_code)
        assert "demo" not in r.text.lower() or "detail" in r.json()
    assert bob.get("/api/campaigns").json() == []
    bd = bob.get("/api/diagnostics").json()
    assert bd["campaigns"] == 0 and bd["jobs"] == {}

    # alice is untouched by bob's attempts
    assert alice.get(f"/api/campaigns/{cid}").status_code == 200
    assert alice.get(f"/api/campaigns/{cid}/candidates/{ids[0]}").json()["draft"]["status"] == "approved"

    # files live only under alice's root
    users = cfg.data_root / "users"
    assert (users / "1" / cid).is_dir() and not list((users / "2").glob("cmp_*"))

    # provider credentials: stored encrypted, visible (as a boolean) only to their owner
    assert alice.put("/api/account/openai-key", json={"api_key": ALICE_KEY}).json() == {"openai_key_set": True}
    assert alice.get("/api/account").json()["openai_key_set"] is True
    assert alice.get("/api/status").json()["demo"] is False
    b = bob.get("/api/account").json()
    assert b["openai_key_set"] is False and b["gmail_connected"] is False and b["user"] == "bob"
    assert bob.get("/api/status").json()["demo"] is True
    raw = sqlite3.connect(cfg.data_root / "auth.sqlite3").execute("SELECT user_id, value FROM secrets").fetchall()
    assert [r[0] for r in raw] == [1] and ALICE_KEY.encode() not in raw[0][1]
    for c in (alice, bob):
        assert ALICE_KEY not in c.get("/api/account").text + c.get("/api/diagnostics").text


def test_gmail_oauth_endpoints_guarded(remote):
    app, cfg, anon, _ = remote
    assert anon.get("/auth/gmail/start", follow_redirects=False).status_code == 401
    assert anon.get("/auth/gmail/callback?state=forged&code=x").status_code == 400
    assert login(anon, "alice").get("/auth/gmail/start", follow_redirects=False).status_code in (303, 409)


def test_remote_user_jobs_run(remote):
    """Per-user workers start lazily on the shared event loop and run jobs."""
    app, cfg, anon, _ = remote
    alice = login(anon, "alice")
    cid = campaign(alice)
    alice.post(f"/api/campaigns/{cid}/discover")
    wait(alice, cid, lambda p: p["candidates"].get("discovered") and idle(p))
    assert len(alice.get(f"/api/campaigns/{cid}").json()["candidates"]) == 3
