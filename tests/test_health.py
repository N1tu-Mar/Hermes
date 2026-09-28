"""Health, readiness, diagnostics, and response hardening. None of them reveal secrets."""

from fastapi.testclient import TestClient

from app.api import create_app
from app.cache import MIGRATIONS
from tests.conftest import make_service


def test_health_ready_and_diagnostics(env):
    client, svc, _ = env
    bare = TestClient(client.app)  # no token: probes are public
    assert bare.get("/healthz").json() == {"status": "ok"}
    r = bare.get("/readyz")
    assert r.status_code == 200 and r.json()["ready"] is True
    assert bare.get("/api/diagnostics").status_code == 401  # diagnostics need auth

    diag = client.get("/api/diagnostics").json()
    assert (
        diag["mode"] == "local" and diag["workers_alive"] and diag["schema"] == {"sqlite": len(MIGRATIONS), "json": 1}
    )
    text = str(diag).lower()
    for word in ("token", "key", "secret", "password", "@", "sk-"):
        assert word not in text, word


def test_security_headers(env):
    client, _, _ = env
    h = client.get("/").headers
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert client.get("/api/campaigns").headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in h  # only in remote mode


def test_readyz_reports_dead_workers(tmp_path):
    svc = make_service(tmp_path / "data")
    with TestClient(create_app(service=svc, token="t")) as client:
        for t in svc.jobs.tasks:
            t.cancel()
        import time

        time.sleep(0.1)
        r = client.get("/readyz")
        assert r.status_code == 503 and r.json()["checks"]["workers"] is False
