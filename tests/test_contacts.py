"""Contact memory, identities, campaign management, CSV, migrations. Offline (demo mode)."""
import csv
import io
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import demo
from app.api import create_app
from app.cache import MIGRATIONS, SCHEMA_V1, Cache
from app.campaigns import CampaignService
from app.gmail import GmailDrafts
from app.ledger import Ledger, canonical_url, name_key
from app.storage import CampaignStore
from test_core import BIO, FakeGmailService, idle, make_campaign, wait

TEXT = "Rutgers/Princeton professors working on computational neurodevelopment who may work with undergraduates"
MIXED_CSV = """Name,Organization,Email,Profile URL,Tags,Notes,Do Not Contact
Ada Park,Columbia University,ada@columbia.example.edu,https://columbia.example.edu/ada,ml; mentor,Met at HackRU,
,Nobody Inc,nobody@x.example.com,,,,
Ben Ode,NYU,not-an-email,,,,
Cy Tan,Stevens,cy@stevens.example.edu,ftp://bad,,,
Dee Roy,Rutgers,dee@rutgers.example.edu,,alumni,,yes
Ada Park,Columbia University,ada2@columbia.example.edu,,,,
"""


def boot(root, gmail=None):
    store = CampaignStore(root)
    cache = Cache(root / "cache.sqlite3")
    svc = CampaignService(store, cache, demo.DemoModel(cache), demo.demo_fetcher(cache), gmail)
    return TestClient(create_app(service=svc, token="t"), headers={"x-app-token": "t"}), svc


def people_of(client, cid):
    return {c["name"]: c for c in client.get(f"/api/campaigns/{cid}").json()["candidates"]}


# ------------------------------------------------------------------ migrations
def test_migrations_upgrade_legacy_db_and_refuse_newer(tmp_path):
    legacy = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(legacy)
    db.executescript(SCHEMA_V1)  # a pre-versioning database: tables exist, user_version 0
    db.execute("INSERT INTO usage (campaign_id, budget) VALUES ('cmp_x', 7)")
    db.commit()
    db.close()
    c = Cache(legacy)
    assert c.q("PRAGMA user_version")[0]["user_version"] == len(MIGRATIONS)
    assert c.q("SELECT budget FROM usage")[0]["budget"] == 7
    assert c.q("SELECT COUNT(*) n FROM people")[0]["n"] == 0
    Cache(legacy)  # reopening is a no-op
    c.db.execute(f"PRAGMA user_version={len(MIGRATIONS) + 1}")
    with pytest.raises(RuntimeError):
        Cache(legacy)


# ------------------------------------------------------------------ matching rules
def test_matching_order_and_ambiguity(tmp_path):
    led = Ledger(Cache(tmp_path / "c.sqlite3"))
    a = led.create_contact({"name": "Dr. Jane Doe", "organization": "Rutgers", "email": "jane@r.edu",
                            "profile_url": "http://www.r.edu/jane/"})
    assert canonical_url("http://www.r.edu/jane/") == canonical_url("https://r.edu/jane")
    assert name_key("Dr. Jane Doe (demo)", "Rutgers (demo)") == name_key("jane doe", "RUTGERS")
    assert led.match("J. Doe", email="JANE@r.edu") == ("match", a)  # verified email wins over name
    assert led.match("Jane Doe", profile_url="https://r.edu/jane") == ("match", a)
    assert led.match("Jane Doe", "Rutgers") == ("match", a)  # name + org fallback
    assert led.match("Someone Else", profile_url="https://r.edu/jane")[0] == "review"  # shared lab page
    assert led.match("Jane Doe", "Rutgers", email="other@r.edu")[0] == "review"  # conflicting email
    b = led.create_contact({"name": "Jane Doe", "organization": "Rutgers"})
    m = led.match("Jane Doe", "Rutgers")
    assert m[0] == "review" and set(m[1]) == {a, b}  # never silently pick one
    assert led.match("Nobody New", "Nowhere") == ("new", None)
    with pytest.raises(ValueError):
        led.create_contact({"name": "Dup", "email": "jane@r.edu"})  # unique email enforced


def test_ambiguous_candidate_goes_to_review_and_inherits_dnc(tmp_path):
    client, svc = boot(tmp_path / "data")
    with client:
        # two different people already in the ledger share Avery's name + organization
        for email in ("a1@x.example.com", "a2@x.example.com"):
            svc.ledger.create_contact({"name": "Dr. Avery Lin (demo)", "organization": "Rutgers University (demo)",
                                       "email": email})
        dnc_id = svc.ledger.match("x", email="a2@x.example.com")[1]
        client.patch(f"/api/contacts/{dnc_id}", json={"do_not_contact": True, "dnc_reason": "asked not to be emailed"})
        cid = make_campaign(client, TEXT)
        client.post(f"/api/campaigns/{cid}/discover")
        wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
        avery = people_of(client, cid)["Dr. Avery Lin (demo)"]
        assert avery["contact_id"] is None and avery["contact_review"] is True
        reviews = client.get("/api/contact-reviews").json()
        assert len(reviews) == 1 and len(reviews[0]["options"]) == 2
        # unresolved review that includes a DNC contact blocks drafting
        client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": [avery["candidate_id"]]})
        wait(client, cid, lambda p: idle(p) and p["candidates"].get("researched"))
        r = client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": [avery["candidate_id"]]}).json()
        assert "resolve the contact review" in r["skipped_do_not_contact"][avery["candidate_id"]]
        # human says: this is a new, different person
        client.post(f"/api/contact-reviews/{reviews[0]['id']}/resolve", json={"contact_id": None})
        avery = people_of(client, cid)["Dr. Avery Lin (demo)"]
        assert avery["contact_id"] not in (None, dnc_id) and not avery["contact_review"]
        r = client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": [avery["candidate_id"]]}).json()
        assert r["queued"] and not r["skipped_do_not_contact"]


# ------------------------------------------------------------------ acceptance flow
def test_acceptance_flow_persists_across_restart(tmp_path):
    root = tmp_path / "data"
    fake = FakeGmailService()
    client, svc = boot(root, GmailDrafts(fake))
    with client:
        # sender identities
        bad = client.post("/api/identities", json={"display_name": "X", "biography": "b", "reply_to": "nope"})
        assert bad.status_code == 400
        me = client.post("/api/identities", json={
            "display_name": "Nitu", "biography": BIO, "organization": "Rutgers University", "role": "Student",
            "signature": "Nitu M.\nRutgers CS '28", "links": ["https://nitu.example.com"],
            "default_ask": "whether you might have room for an undergraduate researcher",
            "reply_to": "nitu@example.com"}).json()
        club = client.post("/api/identities", json={
            "display_name": "Rutgers Entrepreneur Society",
            "biography": "I'm Nitu, president of the Rutgers Entrepreneur Society.",
            "organization": "Rutgers Entrepreneur Society", "role": "President",
            "signature": "Nitu, President, RES"}).json()
        assert [i["display_name"] for i in client.get("/api/identities").json()] == ["Nitu", "Rutgers Entrepreneur Society"]

        # two campaigns containing the same people, each with its own identity
        parsed = client.post("/api/parse", json={"text": TEXT}).json()["intake"]
        cids = []
        for ident, name in ((me, "Neuro labs"), (club, "Neuro speakers")):
            intake = {**parsed, "sender_background": None, "sender_identity_id": str(ident["id"])}
            cids.append(client.post("/api/campaigns", json={"intake": intake, "name": name, "budget": 40}).json()["campaign_id"])
        c1, c2 = cids
        assert client.post("/api/campaigns", json={"intake": {**parsed, "sender_identity_id": "999"}}).status_code == 404
        for cid in cids:
            client.post(f"/api/campaigns/{cid}/discover")
            wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
        p1, p2 = people_of(client, c1), people_of(client, c2)
        assert set(p1) == set(p2) and len(p1) == 3
        for name in p1:  # one global contact per person
            assert p1[name]["contact_id"] and p1[name]["contact_id"] == p2[name]["contact_id"]
        avery1, avery2 = p1["Dr. Avery Lin (demo)"], p2["Dr. Avery Lin (demo)"]
        contact_id = avery1["contact_id"]
        assert len(client.get("/api/contacts").json()) == 3
        detail = client.get(f"/api/contacts/{contact_id}").json()
        assert {c["campaign_id"] for c in detail["campaigns"]} == {c1, c2}
        assert {c["name"] for c in detail["campaigns"]} == {"Neuro labs", "Neuro speakers"}

        # notes + tags visible from both campaigns
        client.patch(f"/api/contacts/{contact_id}", json={"notes": "Prefers short emails.", "tags": "neuro, Priority"})
        for cid, cand in ((c1, avery1), (c2, avery2)):
            d = client.get(f"/api/campaigns/{cid}/candidates/{cand['candidate_id']}").json()
            assert d["contact"]["contact"]["notes"] == "Prefers short emails."
            assert d["contact"]["contact"]["tags"] == ["neuro", "priority"]
        assert people_of(client, c2)["Dr. Avery Lin (demo)"]["tags"] == ["neuro", "priority"]
        assert client.patch(f"/api/contacts/{contact_id}", json={"relationship": "bff"}).status_code == 400

        # draft with identity 1: biography as sender context, signature appended, reply-to on the Gmail draft
        client.post(f"/api/campaigns/{c1}/research", json={"candidate_ids": [avery1["candidate_id"]]})
        wait(client, c1, lambda p: idle(p) and p["candidates"].get("researched"))
        assert client.get(f"/api/contacts/{contact_id}").json()["contact"]["email"] == "avery.lin@demo.example.edu"
        client.post(f"/api/campaigns/{c1}/drafts/generate", json={"candidate_ids": [avery1["candidate_id"]]})
        wait(client, c1, lambda p: idle(p) and p["drafts"].get("needs_review"))
        d = client.get(f"/api/campaigns/{c1}/candidates/{avery1['candidate_id']}").json()["draft"]
        assert d["body"].startswith("Dear Professor Lin") and d["body"].endswith("Nitu M.\nRutgers CS '28")
        assert "undergraduate researcher" in d["body"] and d["issues"] == []
        assert client.post(f"/api/campaigns/{c1}/drafts/{avery1['candidate_id']}/approve").status_code == 200
        res = client.post(f"/api/campaigns/{c1}/gmail-drafts", json={"candidate_ids": [avery1["candidate_id"]]}).json()
        assert res["results"][0]["result"] == "created"
        assert fake.store["d1"]["Reply-To"] == "nitu@example.com"

        # manual outcome + chronological timeline
        assert client.post(f"/api/contacts/{contact_id}/interactions", json={"kind": "draft"}).status_code == 400
        client.post(f"/api/contacts/{contact_id}/interactions",
                    json={"kind": "reply", "detail": "Said to follow up in May"})
        tl = client.get(f"/api/contacts/{contact_id}").json()
        kinds = [e["kind"] for e in tl["timeline"]]
        assert kinds == ["draft", "approval", "gmail_draft", "reply"]
        assert tl["timeline"][2]["meta"]["gmail_draft_id"] == "d1"
        assert tl["contact"]["relationship"] == "replied"

        # identity 2 drafts with a different sender, then do-not-contact blocks everything
        client.post(f"/api/campaigns/{c2}/research", json={"candidate_ids": [avery2["candidate_id"]]})
        wait(client, c2, lambda p: idle(p) and p["candidates"].get("researched"))
        client.post(f"/api/campaigns/{c2}/drafts/generate", json={"candidate_ids": [avery2["candidate_id"]]})
        wait(client, c2, lambda p: idle(p) and p["drafts"].get("needs_review"))
        d2 = client.get(f"/api/campaigns/{c2}/candidates/{avery2['candidate_id']}").json()["draft"]
        assert "president of the Rutgers Entrepreneur Society" in d2["body"] and d2["body"].endswith("Nitu, President, RES")

        client.patch(f"/api/contacts/{contact_id}", json={"do_not_contact": True, "dnc_reason": "asked to stop"})
        r = client.post(f"/api/campaigns/{c2}/drafts/generate", json={"candidate_ids": [avery2["candidate_id"]]}).json()
        assert r["queued"] == [] and "do-not-contact" in r["skipped_do_not_contact"][avery2["candidate_id"]]
        a = client.post(f"/api/campaigns/{c2}/drafts/{avery2['candidate_id']}/approve")
        assert a.status_code == 409 and "do-not-contact" in a.json()["detail"]
        g = client.post(f"/api/campaigns/{c2}/gmail-drafts", json={"candidate_ids": [avery2["candidate_id"]]}).json()
        assert "do-not-contact" in g["results"][0]["result"] and len(fake.store) == 1
        # a job queued before the flag was set is also stopped at the worker
        before = client.get(f"/api/campaigns/{c2}/progress").json()["usage"]["api_calls"]
        svc.jobs.submit(c2, "write", avery2["candidate_id"])
        p = wait(client, c2, idle)
        assert p["usage"]["api_calls"] == before and "draft blocked" in p["recent"][0]["message"]

        # duplicate copies configuration only; archive hides; delete needs archive + confirmation
        dup = client.post(f"/api/campaigns/{c1}/duplicate").json()["campaign_id"]
        dview = client.get(f"/api/campaigns/{dup}").json()
        assert dview["name"] == "Neuro labs (copy)" and dview["candidates"] == []
        assert dview["intake"] == client.get(f"/api/campaigns/{c1}").json()["intake"]
        dp = client.get(f"/api/campaigns/{dup}/progress").json()
        assert dp["drafts"] == {} and dp["jobs"] == {} and dp["usage"]["budget"] == 40
        assert client.patch(f"/api/campaigns/{dup}", json={"name": "Neuro labs fall"}).json()["name"] == "Neuro labs fall"
        assert client.post(f"/api/campaigns/{dup}/delete", json={"confirm": dup}).status_code == 409  # not archived
        client.patch(f"/api/campaigns/{c1}", json={"archived": True})
        assert c1 not in [c["campaign_id"] for c in client.get("/api/campaigns").json()]
        assert [c["campaign_id"] for c in client.get("/api/campaigns?status=archived").json()] == [c1]
        assert client.post(f"/api/campaigns/{c1}/discover").status_code == 409
        assert [c["campaign_id"] for c in client.get("/api/campaigns?q=fall").json()] == [dup]
        assert client.get("/api/campaigns?subtype=startup").json() == []
        client.patch(f"/api/campaigns/{dup}", json={"archived": True})
        assert client.post(f"/api/campaigns/{dup}/delete", json={}).status_code == 409  # no confirmation
        assert client.post(f"/api/campaigns/{dup}/delete", json={"confirm": dup}).json() == {"deleted": dup}
        assert client.get(f"/api/campaigns/{dup}").status_code == 404
        assert client.delete(f"/api/identities/{me['id']}").status_code == 409  # still used by c1

        # mixed CSV: preview, commit, export only valid records
        prev = client.post("/api/contacts/import", json={"csv": MIXED_CSV}).json()
        assert [r["row"] for r in prev["valid"]] == [2, 6]
        errs = {r["row"]: " ".join(r["errors"]) for r in prev["invalid"]}
        assert set(errs) == {3, 4, 5, 7}
        assert "name is required" in errs[3] and "invalid email" in errs[4] and "profile_url" in errs[5]
        assert "duplicate of row 2" in errs[7]
        assert len(client.get("/api/contacts").json()) == 3  # preview writes nothing
        done = client.post("/api/contacts/import", json={"csv": MIXED_CSV, "commit": True}).json()
        assert [r["result"] for r in done["results"]] == ["created", "created"]
        exported = list(csv.DictReader(io.StringIO(client.get("/api/contacts/export.csv").text)))
        names = {r["name"] for r in exported}
        assert {"Ada Park", "Dee Roy"} <= names and not names & {"Ben Ode", "Cy Tan"}
        dee = next(r for r in exported if r["name"] == "Dee Roy")
        assert dee["do_not_contact"] == "yes" and dee["tags"] == "alumni"
        # candidate import into a campaign: known person is skipped, new one linked to the CSV contact
        cand_csv = "name,organization,email\nAda Park,Columbia University,ada@columbia.example.edu\n" \
                   "Dr. Avery Lin (demo),Rutgers University (demo),\n"
        done = client.post(f"/api/campaigns/{c2}/import", json={"csv": cand_csv, "commit": True}).json()
        assert done["invalid"][0]["errors"] == ["already in this campaign"]
        ada = people_of(client, c2)["Ada Park"]
        assert ada["contact_id"] == done["results"][0]["contact_id"]
        rows = list(csv.DictReader(io.StringIO(client.get(f"/api/campaigns/{c2}/export.csv").text)))
        avery_row = next(r for r in rows if r["name"] == "Dr. Avery Lin (demo)")
        assert avery_row["status"] == "researched" and avery_row["do_not_contact"] == "yes"
        assert avery_row["draft_status"] == "needs_review" and avery_row["relationship"] == "replied"

    # restart: brand-new service over the same files
    client, svc = boot(root, GmailDrafts(fake))
    with client:
        c = client.get(f"/api/contacts/{contact_id}").json()
        assert c["contact"]["notes"] == "Prefers short emails." and c["contact"]["do_not_contact"] is True
        assert [e["kind"] for e in c["timeline"]][:4] == ["draft", "approval", "gmail_draft", "reply"]
        assert len(client.get("/api/identities").json()) == 2
        assert [x["campaign_id"] for x in client.get("/api/campaigns?status=archived").json()] == [c1]
        assert client.get(f"/api/campaigns/{c2}").json()["name"] == "Neuro speakers"
        assert len(client.get("/api/contacts").json()) == 5
        r = client.post(f"/api/campaigns/{c2}/drafts/generate", json={"candidate_ids": [avery2["candidate_id"]]}).json()
        assert r["queued"] == [] and r["skipped_do_not_contact"]


def test_existing_campaigns_backfilled_and_keep_approvals(tmp_path):
    """A campaign from before the ledger (no links, no identity) keeps working after upgrade."""
    root = tmp_path / "data"
    client, svc = boot(root)
    with client:
        cid = make_campaign(client, TEXT)
        client.post(f"/api/campaigns/{cid}/discover")
        wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
        first = next(iter(people_of(client, cid).values()))["candidate_id"]
        client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": [first]})
        wait(client, cid, lambda p: idle(p) and p["candidates"].get("researched"))
        client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": [first]})
        wait(client, cid, lambda p: idle(p) and p["drafts"].get("needs_review"))
        client.post(f"/api/campaigns/{cid}/drafts/{first}/approve")
        svc.cache.x("DELETE FROM person_links")  # simulate data created before contact memory existed
        svc.cache.x("DELETE FROM people")
    client, svc = boot(root)
    with client:
        view = people_of(client, cid)
        assert all(c["contact_id"] for c in view.values())
        assert client.get(f"/api/contacts/{view['Dr. Avery Lin (demo)']['contact_id']}").json()["contact"]["email"] \
            == "avery.lin@demo.example.edu"
        res = client.post(f"/api/campaigns/{cid}/gmail-drafts", json={"candidate_ids": [first]}).json()["results"]
        assert res[0]["result"].startswith("gmail not connected")  # approval still valid, not "inputs changed"


def test_mcp_tools_go_through_the_api(tmp_path, monkeypatch):
    from app import mcp_server
    client, svc = boot(tmp_path / "data")
    with client:
        monkeypatch.setattr(mcp_server.httpx, "request", lambda method, url, json=None, params=None, **kw:
                            client.request(method, url.replace(mcp_server.BASE, "/api"), json=json, params=params))
        prev = mcp_server.import_csv(MIXED_CSV)
        assert len(prev["valid"]) == 2 and mcp_server.search_contacts()["contacts"] == []
        mcp_server.import_csv(MIXED_CSV, commit=True)
        ada = mcp_server.search_contacts("Ada")["contacts"][0]
        mcp_server.update_contact(ada["id"], do_not_contact=True, dnc_reason="test")
        assert mcp_server.search_contacts(do_not_contact="yes")["contacts"][0]["name"] in ("Ada Park", "Dee Roy")
        assert mcp_server.log_interaction(ada["id"], "meeting", "Coffee")["contact"]["relationship"] == "meeting"
        assert "error" in mcp_server.log_interaction(ada["id"], "gmail_draft")
        assert "Ada Park" in mcp_server.export_csv()["text"]
