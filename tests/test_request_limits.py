"""Body limits: 413 comes from the size alone, before any parsing, auth, or base64 decoding."""

from fastapi.testclient import TestClient

from app import demo
from app import http_contracts as hc
from app.api import create_app
from app.cache import Cache
from app.campaigns import CampaignService
from app.storage import CampaignStore
from app.workspace import Workspace


def big(n):
    return b'{"x":"' + b"a" * n + b'"}'


def test_normal_json_over_limit_is_413_before_auth(env):
    client, _, _ = env
    r = client.post("/api/campaigns", content=big(hc.JSON_LIMIT), headers={"content-type": "application/json"})
    assert r.status_code == 413 and "limit" in r.json()["detail"]
    r = client.post(
        "/api/campaigns",
        content=big(hc.JSON_LIMIT),
        headers={"content-type": "application/json", "x-app-token": "wrong"},
    )
    assert r.status_code == 413  # size is checked first


def test_streamed_body_without_content_length_is_cut_off(env):
    client, _, _ = env
    chunk = b"a" * 65536

    def body():
        yield b'{"x":"'
        for _ in range(hc.JSON_LIMIT // len(chunk) + 2):
            yield chunk

    r = client.post("/api/campaigns", content=body(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_bad_content_length_is_400(env):
    client, _, _ = env
    r = client.post("/api/campaigns", content=b"{}", headers={"content-length": "abc"})
    assert r.status_code == 400


def test_csv_route_allows_more_than_normal_json(env):
    client, _, _ = env
    rows = "name,email\n" + "\n".join(f"Person {i},p{i}@example.edu" for i in range(20000))
    assert hc.JSON_LIMIT < len(rows) < hc.CSV_LIMIT
    r = client.post("/api/contacts/import", json={"csv": rows})
    assert r.status_code != 413
    r = client.post("/api/contacts/import", content=big(hc.CSV_LIMIT), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_attachment_limit_holds_and_rejection_precedes_decoding(tmp_path):
    cache = Cache(tmp_path / "cache.sqlite3")
    svc = CampaignService(
        CampaignStore(tmp_path),
        cache,
        demo.DemoModel(cache),
        demo.demo_fetcher(cache),
        None,
        Workspace(cache, tmp_path),
    )
    called = []
    svc.workspace.add_attachment = lambda body: called.append(body) or {}
    with TestClient(create_app(service=svc, token="t"), headers={"x-app-token": "t"}) as client:
        check_attachments(client, called)


def check_attachments(client, called):
    r = client.post("/api/attachments", content=big(hc.ATTACHMENT_LIMIT), headers={"content-type": "application/json"})
    assert r.status_code == 413 and not called
    r = client.post("/api/attachments", content=big(hc.JSON_LIMIT + 1024), headers={"content-type": "application/json"})
    assert r.status_code != 413 and called  # encoded attachments get the larger budget


def test_small_requests_unaffected(env):
    client, _, _ = env
    assert client.get("/api/status").status_code == 200
    assert client.post("/api/parse", json={"text": "startup founders working on climate tech"}).status_code == 200


def test_limit_table():
    assert hc.body_limit("/api/attachments") == hc.ATTACHMENT_LIMIT
    assert hc.body_limit("/api/campaigns/c1/import") == hc.CSV_LIMIT
    assert hc.body_limit("/api/contacts/import") == hc.CSV_LIMIT
    assert hc.body_limit("/api/campaigns") == hc.JSON_LIMIT
