"""Remote runtime: bounded per-user services, idle eviction, clean reconnect, shutdown, retention, OAuth states."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import api
from app.auth import Accounts
from app.cache import Cache
from app.config import Config
from app.routes import pool as pool_mod
from app.routes.pool import Busy, OAuthStates, ServicePool
from tests.support import BIO

URL = "https://hermes.test"
USERS = ["user1", "user2", "user3", "user4"]
PW = "correct-horse-battery-1"


class Fake:
    def __init__(self, uid):
        self.uid, self.jobs, self.shut = uid, SimpleNamespace(running=0), 0

    async def shutdown(self, grace):
        await asyncio.sleep(0)
        self.shut += 1


def fake_pool(limit=2, idle=100.0):
    now = [0.0]
    built = []

    def build(uid):
        built.append(Fake(uid))
        return built[-1]

    return ServicePool(build, 0.1, limit, idle, clock=lambda: now[0]), built, now


def test_pool_never_exceeds_limit_and_evicts_least_recently_used():
    async def go():
        pool, built, _ = fake_pool(limit=2)
        for uid in (1, 2, 1, 3, 4):  # 1 is touched again, so 2 goes first, then 1
            await pool.acquire(uid)
            pool.release(uid)
            assert len(pool) <= 2
        assert list(pool.live) == [3, 4]
        assert [f.shut for f in built if f.uid == 2] == [1]

    asyncio.run(go())


def test_pool_busy_when_every_slot_is_held_and_recovers_on_release():
    async def go():
        pool, _, _ = fake_pool(limit=1)
        await pool.acquire("a")
        with pytest.raises(Busy):
            await pool.acquire("b")
        pool.release("a")
        assert (await pool.acquire("b")).uid == "b" and list(pool.live) == ["b"]

    asyncio.run(go())


def test_pool_keeps_services_with_running_jobs():
    async def go():
        pool, _, now = fake_pool(limit=1, idle=10)
        svc = await pool.acquire("a")
        pool.release("a")
        svc.jobs.running = 1
        now[0] = 1000
        await pool.evict_idle()
        assert "a" in pool.live
        with pytest.raises(Busy):
            await pool.acquire("b")
        svc.jobs.running = 0
        await pool.evict_idle()
        assert "a" not in pool.live and svc.shut == 1

    asyncio.run(go())


def test_pool_last_used_and_idle_eviction():
    async def go():
        pool, _, now = fake_pool(limit=5, idle=60)
        for uid in "ab":
            await pool.acquire(uid)
            pool.release(uid)
        now[0] = 50
        await pool.acquire("a")
        pool.release("a")  # a used at 50, b at 0
        now[0] = 70
        await pool.evict_idle()
        assert list(pool.live) == ["a"] and pool.last_used == {"a": 50}
        now[0] = 200
        await pool.evict_idle()
        assert not pool.live

    asyncio.run(go())


def test_pool_concurrent_first_requests_build_once_and_close_waits_for_all():
    async def go():
        pool, built, _ = fake_pool(limit=3)
        got = await asyncio.gather(*(pool.acquire("a") for _ in range(5)))
        assert len({id(g) for g in got}) == 1 and len(built) == 1 and pool.busy["a"] == 5
        await pool.acquire("b")
        await pool.close()
        assert not pool.live and [f.shut for f in built] == [1, 1]
        with pytest.raises(Busy):
            await pool.acquire("c")

    asyncio.run(go())


def test_reconnect_waits_for_shutdown_before_rebuilding():
    async def go():
        pool, built, _ = fake_pool(limit=1)
        await pool.acquire("a")
        pool.release("a")
        await pool.acquire("b")  # evicts a
        pool.release("b")
        again = await pool.acquire("a")
        assert again is built[-1] and built[0].shut == 1 and len(built) == 3

    asyncio.run(go())


def test_oauth_states_expire_are_single_use_and_bounded():
    now = [0.0]
    st = OAuthStates(ttl=10, limit=4, per_user=2, clock=lambda: now[0])
    st.add("s1", 1, "v")
    assert st.pop("s1") == (1, "v") and st.pop("s1") is None  # single use
    st.add("s2", 1, None)
    now[0] = 10
    assert st.pop("s2") is None  # expired
    st.add("a", 1, None), st.add("b", 1, None), st.add("c", 1, None)
    assert list(st.items) == ["b", "c"]  # per-user cap drops the user's oldest
    st.add("d", 2, None), st.add("e", 2, None), st.add("f", 3, None)
    assert len(st) == 4 and "b" not in st.items  # global cap drops the oldest overall
    now[0] = 30
    st.sweep()
    assert len(st) == 0


@pytest.fixture
def remote(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "TOKEN_FILE", tmp_path / "app_token")
    monkeypatch.setattr(pool_mod, "MAX_ACTIVE_SERVICES", 2)
    cfg = Config(
        mode="remote",
        data_root=tmp_path / "data",
        public_url=URL,
        secret_key=Fernet.generate_key().decode(),
        shutdown_grace=0.5,
    )
    cfg.data_root.mkdir()
    acc = Accounts(cfg.data_root, cfg.secret_key)
    ids = {u: acc.add_user(u, PW) for u in USERS}
    acc.close()
    app = api.create_app(config=cfg)
    with TestClient(app, base_url=URL) as client:
        yield app, cfg, client, ids


def session(client, name):
    r = client.post("/auth/login", json={"username": name, "password": PW})
    hdr = {"x-csrf-token": r.json()["csrf"], "cookie": f"{api.SESSION_COOKIE}={client.cookies[api.SESSION_COOKIE]}"}
    client.cookies.clear()
    return lambda method, url, **kw: client.request(method, url, headers=hdr, **kw)


def new_campaign(call):
    parsed = call("POST", "/api/parse", json={"text": "startup founders working on climate tech"}).json()
    body = {"intake": {**parsed["intake"], "sender_background": BIO}, "max_candidates": 20, "budget": 40}
    return call("POST", "/api/campaigns", json=body).json()["campaign_id"]


def test_many_users_never_exceed_the_bound_and_evicted_users_reconnect(remote):
    app, cfg, client, ids = remote
    calls = {u: session(client, u) for u in USERS}
    cid = new_campaign(calls["user1"])
    for u in USERS * 2:  # far more users than slots, twice around
        assert calls[u]("GET", "/api/campaigns").status_code == 200
        assert len(app.state.pool) <= 2
    # u1 was evicted and rebuilt from disk: its campaign is still there and usable
    assert [c["campaign_id"] for c in calls["user1"]("GET", "/api/campaigns").json()] == [cid]
    assert calls["user1"]("GET", f"/api/campaigns/{cid}").status_code == 200
    assert calls["user2"]("GET", "/api/campaigns").json() == []  # still isolated


def test_idle_eviction_and_clean_reconnect_over_http(remote):
    app, cfg, client, ids = remote
    call = session(client, "user1")
    cid = new_campaign(call)
    pool = app.state.pool
    old = pool.live[ids["user1"]]
    pool.idle = 0.0
    client.portal.call(pool.evict_idle)
    assert not pool.live and pool.last_used == {}
    with pytest.raises(Exception):  # the evicted service's database is closed
        old.cache.ping()
    assert call("GET", f"/api/campaigns/{cid}").status_code == 200
    assert pool.live[ids["user1"]] is not old and ids["user1"] in pool.last_used


def test_pool_exhaustion_is_503_with_retry_after(remote):
    app, cfg, client, ids = remote
    pool = app.state.pool
    pool.limit = 1
    call = session(client, "user1")
    assert call("GET", "/api/campaigns").status_code == 200
    pool.busy[ids["user1"]] += 1  # u1 has a request in flight
    try:
        r = session(client, "user2")("GET", "/api/campaigns")
        assert r.status_code == 503 and r.headers["retry-after"] and "busy" in r.json()["detail"]
    finally:
        pool.busy.clear()


def test_shutdown_closes_every_service(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "TOKEN_FILE", tmp_path / "app_token")
    cfg = Config(
        mode="remote", data_root=tmp_path / "d", public_url=URL, secret_key=Fernet.generate_key().decode(),
        shutdown_grace=0.5,
    )  # fmt: skip
    cfg.data_root.mkdir()
    acc = Accounts(cfg.data_root, cfg.secret_key)
    for u in USERS[:2]:
        acc.add_user(u, PW)
    acc.close()
    app = api.create_app(config=cfg)
    with TestClient(app, base_url=URL) as client:
        for u in USERS[:2]:
            assert session(client, u)("GET", "/api/campaigns").status_code == 200
        services = list(app.state.pool.live.values())
        assert len(services) == 2
    assert app.state.pool.closed and not app.state.pool.live
    for s in services:
        with pytest.raises(Exception):
            s.cache.ping()


def test_scheduled_retention_purges_live_and_idle_users(remote):
    app, cfg, client, ids = remote
    pool, acc = app.state.pool, Accounts(cfg.data_root, cfg.secret_key)
    call = session(client, "user1")
    new_campaign(call)
    live = pool.live[ids["user1"]]
    live.cache.x("INSERT INTO events (campaign_id, at, message) VALUES ('c', ?, 'old')", (time.time() - 400 * 86400,))
    # user2 is not live: build its database on disk, then age an event
    path = acc.user_dir(ids["user2"]) / "cache.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    idle_cache = Cache(path)
    idle_cache.x("INSERT INTO events (campaign_id, at, message) VALUES ('c', ?, 'old')", (time.time() - 400 * 86400,))
    idle_cache.x("INSERT INTO events (campaign_id, at, message) VALUES ('c', ?, 'new')", (time.time(),))
    idle_cache.close()
    pool_mod.purge_users(acc, pool, cfg.retention_days)
    acc.close()
    assert live.cache.q("SELECT count(*) AS n FROM events WHERE message='old'")[0]["n"] == 0
    idle_cache = Cache(path)
    assert [r["message"] for r in idle_cache.q("SELECT message FROM events")] == ["new"]
    idle_cache.close()
    assert ids["user2"] not in pool.live  # retention did not spin up a service


def test_oauth_start_records_bounded_state(remote, monkeypatch):
    app, cfg, client, ids = remote
    import app.gmail as gmail

    class Flow:
        n = 0
        code_verifier = "verifier"

        def authorization_url(self, **kw):
            Flow.n += 1
            return f"https://accounts.example/auth?n={Flow.n}", f"state-{Flow.n}"

    creds = SimpleNamespace(exists=lambda: True)
    monkeypatch.setattr(gmail, "credentials_paths", lambda: (creds, creds))
    monkeypatch.setattr(gmail, "web_flow", lambda uri: Flow())
    call = session(client, "user1")
    for _ in range(6):
        assert call("GET", "/auth/gmail/start", follow_redirects=False).status_code == 303
    assert len(app.state.oauth) == pool_mod.MAX_PENDING_PER_USER
    assert app.state.oauth.pop("state-1") is None and app.state.oauth.pop("state-6") == (ids["user1"], "verifier")


def test_background_maintenance_evicts_idle_services(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "TOKEN_FILE", tmp_path / "app_token")
    monkeypatch.setattr(pool_mod, "MAINTENANCE_INTERVAL", 0.05)
    monkeypatch.setattr(pool_mod, "IDLE_EVICT_SECONDS", 0.1)
    cfg = Config(
        mode="remote", data_root=tmp_path / "d", public_url=URL, secret_key=Fernet.generate_key().decode(),
        shutdown_grace=0.5,
    )  # fmt: skip
    cfg.data_root.mkdir()
    acc = Accounts(cfg.data_root, cfg.secret_key)
    acc.add_user("user1", PW)
    acc.close()
    app = api.create_app(config=cfg)
    with TestClient(app, base_url=URL) as client:
        assert session(client, "user1")("GET", "/api/campaigns").status_code == 200
        assert len(app.state.pool) == 1
        deadline = time.time() + 5
        while app.state.pool.live and time.time() < deadline:
            time.sleep(0.05)
        assert not app.state.pool.live
