import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import demo
from app.api import create_app
from app.cache import Cache
from app.campaigns import CampaignService, parse_request
from app.contracts import validate_files
from app.gmail import GmailDrafts
from app.outlines import OutlineBlocked, build_outline, route_template
from app.research import finalize_profile
from app.storage import CampaignStore, atomic_write_json
from app.writer import check_draft

BIO = "I'm Nitu, a Rutgers undergraduate studying computer science and cognitive science."


class FakeGmailService:
    """Mimics service.users().drafts().create/get/list(...).execute()."""

    def __init__(self):
        self.store, self.fail_next = {}, False

    def users(self):
        return self

    def drafts(self):
        return self

    def create(self, userId, body):
        return _Exec(lambda: self._create(body))

    def _create(self, body):
        import base64, email
        if self.fail_next:
            self.fail_next = False
            # Simulate timeout AFTER Gmail stored it: the dangerous duplicate case.
            self._store(body)
            raise TimeoutError("socket timeout")
        return {"id": self._store(body)}

    def _store(self, body):
        import base64, email
        msg = email.message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]))
        did = f"d{len(self.store) + 1}"
        self.store[did] = msg
        return did

    def get(self, userId, id, format):
        m = self.store[id]
        return _Exec(lambda: {"id": id, "message": {"payload": {"headers": [{"name": k, "value": v} for k, v in m.items()]}}})

    def list(self, userId, maxResults):
        return _Exec(lambda: {"drafts": [{"id": d} for d in self.store]})


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


@pytest.fixture
def env(tmp_path):
    store = CampaignStore(tmp_path / "data")
    cache = Cache(tmp_path / "data" / "cache.sqlite3")
    fake = FakeGmailService()
    svc = CampaignService(store, cache, demo.DemoModel(cache), demo.demo_fetcher(cache), GmailDrafts(fake))
    app = create_app(service=svc, token="t")
    with TestClient(app, headers={"x-app-token": "t"}) as client:
        yield client, svc, fake


def wait(client, cid, until, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        p = client.get(f"/api/campaigns/{cid}/progress").json()
        if until(p):
            return p
        time.sleep(0.05)
    raise AssertionError(f"timeout: {p}")


def idle(p):
    return not p["jobs"].get("queued") and not p["jobs"].get("running")


def make_campaign(client, text, subtype=None):
    parsed = client.post("/api/parse", json={"text": text, "subtype": subtype}).json()
    intake = {**parsed["intake"], "sender_background": BIO}
    return client.post("/api/campaigns", json={"intake": intake, "max_candidates": 20, "budget": 40}).json()["campaign_id"]


# ------------------------------------------------------------------ units
def test_route_template():
    assert route_template({"mode": "research", "subtype": "research_professor"}) == "research_professor"
    assert route_template({"mode": "outreach", "subtype": "startup"}) == "startup"
    assert route_template({"mode": "outreach", "subtype": "speaker_mentor"}) == "speaker_invite"
    assert route_template({"mode": "outreach", "subtype": "speaker_mentor"}, followup=True) == "rsvp_followup"
    with pytest.raises(OutlineBlocked):
        route_template({"mode": "outreach", "subtype": "startup"}, followup=True)


def test_followup_blocked_without_recorded_invite():
    prof = {"name": "A B", "evidence": [{"claim": "x", "source_url": "https://a.org/p"}], "fit_reason": ""}
    with pytest.raises(OutlineBlocked):
        build_outline({"mode": "outreach", "subtype": "speaker_mentor"}, prof, BIO, followup=True)


def test_unsourced_claims_and_unverified_email_rejected():
    cand = {"candidate_id": "c_001", "name": "A", "organization": "O", "role": "R"}
    data = {"contact_email": "a@o.edu", "contact_source_url": "https://o.edu/a", "summary": "s",
            "research_interests": [], "fit_reason": "f",
            "evidence": [{"claim": "sourced", "source_url": "https://o.edu/a"},
                         {"claim": "made up", "source_url": "https://nowhere.example/x"},
                         {"claim": "no url", "source_url": ""}]}
    p = finalize_profile(cand, data, known_urls={"https://o.edu/a"}, pages={"https://o.edu/a": "no email here"})
    assert [e["claim"] for e in p["evidence"]] == ["sourced"]
    assert p["email_verified_on_page"] is False and p["status"] == "needs_contact_review"
    p = finalize_profile(cand, data, known_urls={"https://o.edu/a"}, pages={"https://o.edu/a": "Email: a@o.edu"})
    assert p["email_verified_on_page"] is True and p["status"] == "researched"


def test_draft_checks_flag_invented_details():
    outline = {"evidence": [{"claim": "Studies infant attention."}], "sender_context": BIO, "event_details": None,
               "earlier_invite": None, "ask": "a chat", "maximum_length": 150}
    issues = check_draft("Hi", "As we discussed on March 3, see https://x.io and your 2019 paper.", outline, None, ["e0"])
    joined = " ".join(issues)
    assert "raw URL" in joined and "date" in joined and "year" in joined and "prior relationship" in joined
    assert check_draft("Hi", "I read that you study infant attention.", outline, None, ["e0"]) == []


def test_atomic_write_and_campaign_isolation(tmp_path):
    store = CampaignStore(tmp_path)
    a = store.create({"mode": "research"})
    b = store.create({"mode": "outreach"})
    assert a != b
    store.update_candidates(a, lambda d: d["candidates"].append({"candidate_id": "c_001", "name": "X", "status": "discovered"}))
    assert store.candidates(b)["candidates"] == []
    assert store.research(a)["campaign_id"] == a
    assert not list((tmp_path / a).glob("*.tmp"))
    with pytest.raises(KeyError):
        store.dir("../etc")
    with pytest.raises(KeyError):
        store.dir("cmp_20260926_deadbeef/../../x")
    # root templates stay untouched
    root = json.loads((store.template_root / "candidates.json").read_text())
    assert root["campaign_id"] is None and root["candidates"] == []


def test_parse_request():
    intake, q = parse_request("Rutgers/Princeton professors working on computational neurodevelopment who may work with undergraduates")
    assert intake["subtype"] == "research_professor"
    assert {"Rutgers", "Princeton"} <= set(intake["organizations"])
    assert intake["research_areas"] == ["computational neurodevelopment"]
    assert q is None


# ------------------------------------------------------------------ end to end (demo mode)
def test_full_flow_research(env):
    client, svc, fake = env
    assert client.get("/api/campaigns", headers={"x-app-token": "wrong"}).status_code == 401
    cid = make_campaign(client, "Rutgers/Princeton professors working on computational neurodevelopment who may work with undergraduates")
    client.post(f"/api/campaigns/{cid}/discover")
    p = wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
    view = client.get(f"/api/campaigns/{cid}").json()
    ids = [c["candidate_id"] for c in view["candidates"]]
    assert len(ids) == 3

    client.post(f"/api/campaigns/{cid}/select", json={"candidate_ids": ids, "action": "select"})
    client.post(f"/api/campaigns/{cid}/research", json={})
    p = wait(client, cid, lambda p: idle(p) and not p["candidates"].get("selected") and not p["candidates"].get("researching"))
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

    client.patch(f"/api/campaigns/{cid}/drafts/{ids[0]}", json={"subject": d["subject"], "body": d["body"] + "\nThank you!"})
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
    cid = make_campaign(client, "Find startup founders and investors to speak at a Rutgers Entrepreneur Society panel about pre-seed investing")
    client.post(f"/api/campaigns/{cid}/discover")
    wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
    ids = [c["candidate_id"] for c in client.get(f"/api/campaigns/{cid}").json()["candidates"]]
    client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": ids[:1]})
    wait(client, cid, lambda p: idle(p) and p["candidates"].get("researched"))
    # follow-up before any invitation: blocked, not improvised
    client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": ids[:1], "followup": True})
    p = wait(client, cid, lambda p: idle(p) and p["drafts"].get("blocked"))
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
    store = CampaignStore(tmp_path / "data")
    cache = Cache(tmp_path / "data" / "cache.sqlite3")
    svc = CampaignService(store, cache, demo.DemoModel(cache), demo.demo_fetcher(cache))
    with TestClient(create_app(service=svc, token="t"), headers={"x-app-token": "t"}) as client:
        cid = make_campaign(client, "startup founders working on climate tech", subtype="startup")
        client.post(f"/api/campaigns/{cid}/discover")
        wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
        ids = [c["candidate_id"] for c in client.get(f"/api/campaigns/{cid}").json()["candidates"]]
        client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": ids[:1]})
        wait(client, cid, lambda p: idle(p) and p["candidates"].get("researched"))
        calls = client.get(f"/api/campaigns/{cid}/progress").json()["usage"]["api_calls"]
        # simulate a crash mid-run: a job left 'running', a candidate left 'researching'
        cache.put_job("job_crash", cid, "research", ids[1], "running")
        store.update_candidates(cid, lambda d: d["candidates"][1].update(status="researching"))
    # "restart" with a fresh service over the same files
    cache2 = Cache(tmp_path / "data" / "cache.sqlite3")
    svc2 = CampaignService(store, cache2, demo.DemoModel(cache2), demo.demo_fetcher(cache2))
    with TestClient(create_app(service=svc2, token="t"), headers={"x-app-token": "t"}) as client:
        assert client.get(f"/api/campaigns/{cid}/progress").json()["jobs"].get("interrupted") == 1
        r = client.post(f"/api/campaigns/{cid}/resume").json()
        assert r["research"]["candidates"] == [ids[1]]  # only the unfinished one
        p = wait(client, cid, lambda p: idle(p) and p["candidates"].get("researched") == 1 and not p["candidates"].get("selected"))
        assert p["usage"]["api_calls"] == calls + 1
