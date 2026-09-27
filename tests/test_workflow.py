"""End-to-end campaign workflow in demo mode, including restart and shutdown recovery."""

import asyncio

from fastapi.testclient import TestClient

from app import demo
from app.api import create_app
from app.config import Config
from app.gmail import GmailDrafts
from tests.conftest import make_service
from tests.support import FakeGmailService, idle, make_campaign, wait


def test_full_flow_research(env):
    client, svc, fake = env
    assert client.get("/api/campaigns", headers={"x-app-token": "wrong"}).status_code == 401
    cid = make_campaign(
        client,
        "Rutgers/Princeton professors working on computational neurodevelopment who may work with undergraduates",
    )
    client.post(f"/api/campaigns/{cid}/discover")
    p = wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
    view = client.get(f"/api/campaigns/{cid}").json()
    ids = [c["candidate_id"] for c in view["candidates"]]
    assert len(ids) == 3

    client.post(f"/api/campaigns/{cid}/select", json={"candidate_ids": ids, "action": "select"})
    client.post(f"/api/campaigns/{cid}/research", json={})
    p = wait(
        client,
        cid,
        lambda p: idle(p) and not p["candidates"].get("selected") and not p["candidates"].get("researching"),
    )
    # one verified, one missing email, one unreachable page isolated to its own candidate
    assert p["candidates"] == {"researched": 1, "needs_contact_review": 1, "research_failed": 1}
    calls_after_research = p["usage"]["api_calls"]

    # rerun research: nothing fresh is repeated
    r = client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": ids[:2]}).json()
    assert r["candidates"] == []

    client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": ids})
    p = wait(client, cid, lambda p: idle(p) and p["drafts"].get("needs_review") == 2)
    d = client.get(f"/api/campaigns/{cid}/candidates/{ids[0]}").json()["draft"]
    assert d["template_version"].startswith("research_professor") and d["issues"] == []

    # Gmail refuses unapproved drafts
    res = client.post(f"/api/campaigns/{cid}/gmail-drafts", json={"candidate_ids": [ids[0]]}).json()["results"]
    assert res[0]["result"].startswith("skipped") and not fake.store

    client.patch(
        f"/api/campaigns/{cid}/drafts/{ids[0]}", json={"subject": d["subject"], "body": d["body"] + "\nThank you!"}
    )
    assert client.post(f"/api/campaigns/{cid}/drafts/{ids[0]}/approve").status_code == 200

    # First Gmail attempt times out after Gmail stored it; retry reconciles instead of duplicating.
    fake.fail_next = True
    res = client.post(f"/api/campaigns/{cid}/gmail-drafts", json={"candidate_ids": [ids[0]]}).json()["results"]
    assert res[0]["result"].startswith("failed")
    res = client.post(f"/api/campaigns/{cid}/gmail-drafts", json={"candidate_ids": [ids[0]]}).json()["results"]
    assert res[0]["result"] == "reconciled existing draft"
    res = client.post(f"/api/campaigns/{cid}/gmail-drafts", json={"candidate_ids": [ids[0]]}).json()["results"]
    assert res[0]["result"] == "already created"
    assert len(fake.store) == 1
    assert fake.store["d1"]["To"] == "avery.lin@demo.example.edu"

    # regenerate: no new API calls for unchanged inputs, no overwrite of the Gmail draft
    before = client.get(f"/api/campaigns/{cid}/progress").json()["usage"]["api_calls"]
    client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": ids})
    p = wait(client, cid, idle)
    assert p["usage"]["api_calls"] == before
    assert calls_after_research < before

    assert client.get(f"/api/campaigns/{cid}/validate").json()["problems"] == []


def test_speaker_outline_differs_and_followup_rule(env):
    client, svc, fake = env
    cid = make_campaign(
        client,
        "Find startup founders and investors to speak at a Rutgers Entrepreneur Society panel about pre-seed investing",
    )
    client.post(f"/api/campaigns/{cid}/discover")
    wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
    ids = [c["candidate_id"] for c in client.get(f"/api/campaigns/{cid}").json()["candidates"]]
    client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": ids[:1]})
    wait(client, cid, lambda p: idle(p) and p["candidates"].get("researched"))
    # follow-up before any invitation: blocked, not improvised
    client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": ids[:1], "followup": True})
    wait(client, cid, lambda p: idle(p) and p["drafts"].get("blocked"))
    client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": ids[:1]})
    wait(client, cid, lambda p: idle(p) and p["drafts"].get("needs_review"))
    d = client.get(f"/api/campaigns/{cid}/candidates/{ids[0]}").json()["draft"]
    assert d["template_version"].startswith("speaker_invite")
    assert client.post(f"/api/campaigns/{cid}/drafts/{ids[0]}/mark-invited").status_code == 409
    client.post(f"/api/campaigns/{cid}/drafts/{ids[0]}/approve")
    assert client.post(f"/api/campaigns/{cid}/drafts/{ids[0]}/mark-invited").status_code == 200
    client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": ids[:1], "followup": True})
    wait(client, cid, lambda p: idle(p) and p["drafts"].get("needs_review"))
    d = client.get(f"/api/campaigns/{cid}/candidates/{ids[0]}").json()["draft"]
    assert d["template_version"].startswith("rsvp_followup")


def test_restart_resumes_without_repeating(tmp_path):
    root = tmp_path / "data"
    svc = make_service(root)
    with TestClient(create_app(service=svc, token="t"), headers={"x-app-token": "t"}) as client:
        cid = make_campaign(client, "startup founders working on climate tech", subtype="startup")
        client.post(f"/api/campaigns/{cid}/discover")
        wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
        ids = [c["candidate_id"] for c in client.get(f"/api/campaigns/{cid}").json()["candidates"]]
        client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": ids[:1]})
        wait(client, cid, lambda p: idle(p) and p["candidates"].get("researched"))
        calls = client.get(f"/api/campaigns/{cid}/progress").json()["usage"]["api_calls"]
        # simulate a crash mid-run: a job left 'running', a candidate left 'researching'
        svc.cache.put_job("job_crash", cid, "research", ids[1], "running")
        svc.store.update_candidates(cid, lambda d: d["candidates"][1].update(status="researching"))
    # "restart" with a fresh service over the same files
    with TestClient(create_app(service=make_service(root), token="t"), headers={"x-app-token": "t"}) as client:
        assert client.get(f"/api/campaigns/{cid}/progress").json()["jobs"].get("interrupted") == 1
        r = client.post(f"/api/campaigns/{cid}/resume").json()
        assert r["research"]["candidates"] == [ids[1]]  # only the unfinished one
        p = wait(
            client,
            cid,
            lambda p: idle(p) and p["candidates"].get("researched") == 1 and not p["candidates"].get("selected"),
        )
        assert p["usage"]["api_calls"] == calls + 1


class SlowModel(demo.DemoModel):
    async def structured(self, campaign_id, instructions, user_input, schema_name, schema, web_search=False):
        if schema_name == "profile":
            await asyncio.sleep(30)
        return await super().structured(campaign_id, instructions, user_input, schema_name, schema, web_search)


def test_graceful_shutdown_checkpoints_and_recovers(tmp_path):
    root = tmp_path / "data"
    fake = FakeGmailService()
    svc = make_service(root, GmailDrafts(fake))
    svc.model = SlowModel(svc.cache)
    cfg = Config(shutdown_grace=0.2)
    with TestClient(create_app(service=svc, token="t", config=cfg), headers={"x-app-token": "t"}) as client:
        cid = make_campaign(client, "startup founders working on climate tech", subtype="startup")
        client.post(f"/api/campaigns/{cid}/discover")
        wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
        ids = [c["candidate_id"] for c in client.get(f"/api/campaigns/{cid}").json()["candidates"]]
        client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": ids[:2]})
        wait(client, cid, lambda p: p["jobs"].get("running"))
    # shutdown did not wait 30s, checkpointed every unfinished job, and closed provider clients
    assert fake.closed
    with TestClient(create_app(service=make_service(root), token="t"), headers={"x-app-token": "t"}) as client:
        p = client.get(f"/api/campaigns/{cid}/progress").json()
        assert p["jobs"].get("interrupted") == 2 and not p["jobs"].get("running")
        assert "researching" not in p["candidates"]
        client.post(f"/api/campaigns/{cid}/resume")
        done = lambda p: p["candidates"].get("researched", 0) + p["candidates"].get("needs_contact_review", 0)
        p = wait(client, cid, lambda p: idle(p) and done(p) == 2)
        assert client.get(f"/api/campaigns/{cid}/validate").json()["problems"] == []
