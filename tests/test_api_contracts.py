"""Destructive and external-action routes: strict bodies, one error shape, safe deletion."""

import pytest

from tests.support import make_campaign

TEXT = "startup founders working on climate tech"


@pytest.fixture
def cid(env):
    client, _, _ = env
    return make_campaign(client, TEXT, subtype="startup")


def test_immediate_delete_endpoint_is_gone(env, cid):
    client, svc, _ = env
    assert client.delete(f"/api/campaigns/{cid}").status_code == 405
    assert svc.ledger.campaign_meta(cid)  # still there


def test_delete_needs_archive_and_exact_confirmation(env, cid):
    client, svc, _ = env
    url = f"/api/campaigns/{cid}/delete"
    assert client.post(url, json={"confirm": cid}).status_code == 409  # active
    assert client.patch(f"/api/campaigns/{cid}", json={"archived": True}).status_code == 200
    assert client.post(url, json={}).status_code == 409  # no confirmation
    assert client.post(url).status_code == 409  # no body at all
    assert client.post(url, json={"confirm": cid + "x"}).status_code == 409  # wrong confirmation
    assert client.post(url, json={"confirm": cid.upper()}).status_code == 409
    assert client.get(f"/api/campaigns/{cid}").status_code == 200
    assert client.post(url, json={"confirm": cid}).json() == {"deleted": cid}
    assert client.get(f"/api/campaigns/{cid}").status_code == 404


def test_string_boolean_and_wrong_types_rejected(env, cid):
    client, svc, _ = env
    r = client.patch(f"/api/campaigns/{cid}", json={"archived": "false"})
    assert r.status_code == 422 and "archived" in r.json()["detail"]
    assert not svc.ledger.campaign_meta(cid)["archived"]
    assert client.patch(f"/api/campaigns/{cid}", json={"archived": 1}).status_code == 422
    assert client.patch(f"/api/campaigns/{cid}", json={"name": 5}).status_code == 422
    assert client.patch(f"/api/campaigns/{cid}/sending", json={"enabled": "yes"}).status_code == 422
    assert client.post(f"/api/campaigns/{cid}/delete", json={"confirm": 5}).status_code == 422
    assert client.patch(f"/api/campaigns/{cid}", json={"archived": True}).json()["archived"]


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("patch", "/api/campaigns/{cid}", {"archived": True, "extra": 1}),
        ("post", "/api/campaigns/{cid}/delete", {"confirm": "x", "force": True}),
        ("patch", "/api/campaigns/{cid}/sending", {"enabled": True, "x": 1}),
        ("post", "/api/campaigns/{cid}/sends/preview", {"candidate_id": "c_001", "x": 1}),
        ("post", "/api/campaigns/{cid}/sends", {"candidate_id": "c", "approval_hash": "h", "x": 1}),
        ("post", "/api/campaigns/{cid}/gmail-drafts", {"candidate_ids": [], "x": 1}),
        ("patch", "/api/sending/settings", {"bogus": 1}),
        ("post", "/api/suppressions", {"email": "a@b.co", "x": 1}),
        ("post", "/api/sends/nope/outcome", {"outcome": "sent", "x": 1}),
        ("put", "/api/campaigns/{cid}/candidates/c_001/do-not-contact", {"nope": 1}),
        ("post", "/api/campaigns/{cid}/rules/r/run", {"dryrun": False}),
        ("post", "/api/templates/t/archive", {"archive": True}),
    ],
)
def test_unknown_fields_rejected(env, cid, method, path, body):
    client, _, _ = env
    r = getattr(client, method)(path.format(cid=cid), json=body)
    assert r.status_code == 422, r.text
    assert "extra" in r.json()["detail"].lower() or "not permitted" in r.json()["detail"].lower()


def test_string_boolean_rejected_on_external_routes(env, cid):
    client, _, _ = env
    assert client.patch("/api/sending/settings", json={"enabled": "true"}).status_code == 422
    assert client.put(f"/api/campaigns/{cid}/candidates/c/do-not-contact", json={"do_not_contact": "no"}).status_code == 422
    assert client.post(f"/api/campaigns/{cid}/rules/r/run", json={"dry_run": "false"}).status_code == 422
    assert client.post("/api/templates/t/archive", json={"archived": "false"}).status_code == 422
    assert client.post(f"/api/campaigns/{cid}/gmail-drafts", json={"candidate_ids": "c_001"}).status_code == 422


def test_malformed_json_is_400_everywhere(env, cid):
    client, _, _ = env
    hdr = {"content-type": "application/json"}
    for method, path in [
        ("patch", f"/api/campaigns/{cid}"),
        ("post", f"/api/campaigns/{cid}/delete"),
        ("post", f"/api/campaigns/{cid}/gmail-drafts"),
        ("post", "/api/campaigns"),  # untyped dict route
    ]:
        r = getattr(client, method)(path, content=b'{"archived": tru', headers=hdr)
        assert r.status_code == 400 and r.json() == {"detail": "malformed JSON body"}, (path, r.text)


def test_valid_partial_settings_still_work(env):
    client, _, _ = env
    assert client.patch("/api/sending/settings", json={"daily_limit": 7}).json()["daily_limit"] == 7
    assert client.patch("/api/sending/settings", json={"timezone": "Nowhere/Land"}).status_code == 400
