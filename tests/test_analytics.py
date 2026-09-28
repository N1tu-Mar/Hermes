"""Acceptance flow for analytics and notifications (demo fixtures, no network)."""

import csv
import io
import time
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app import analytics, demo
from app.api import create_app
from app.cache import Cache
from app.campaigns import CampaignService
from app.storage import CampaignStore
from app.workspace import Workspace

BIO = "I'm Nitu, a Rutgers undergraduate studying computer science and cognitive science."


def service(root):
    store, cache = CampaignStore(root), Cache(root / "cache.sqlite3")
    return CampaignService(store, cache, demo.DemoModel(cache), demo.demo_fetcher(cache), None, Workspace(cache, root))


def wait(client, cid, until, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        p = client.get(f"/api/campaigns/{cid}/progress").json()
        if not p["jobs"].get("queued") and not p["jobs"].get("running") and until(p):
            return p
        time.sleep(0.03)
    raise AssertionError(p)


def campaign(client, text, subtype):
    intake = {
        **client.post("/api/parse", json={"text": text, "subtype": subtype}).json()["intake"],
        "sender_background": BIO,
    }
    cid = client.post("/api/campaigns", json={"intake": intake, "budget": 40}).json()["campaign_id"]
    client.post(f"/api/campaigns/{cid}/discover")
    wait(client, cid, lambda p: p["candidates"].get("discovered"))
    ids = [c["candidate_id"] for c in client.get(f"/api/campaigns/{cid}").json()["candidates"]]
    client.post(f"/api/campaigns/{cid}/select", json={"candidate_ids": ids, "action": "select"})
    client.post(f"/api/campaigns/{cid}/research", json={})
    wait(client, cid, lambda p: not p["candidates"].get("selected") and not p["candidates"].get("researching"))
    client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": ids})
    wait(client, cid, lambda p: p["drafts"].get("needs_review"))
    return cid, ids


def send(client, cid, cand):
    assert client.post(f"/api/campaigns/{cid}/drafts/{cand}/approve").status_code == 200
    assert client.post(f"/api/campaigns/{cid}/drafts/{cand}/mark-contacted").status_code == 200


def outcome(client, cid, cand, kind):
    r = client.post(f"/api/campaigns/{cid}/candidates/{cand}/outcomes", json={"outcome": kind})
    assert r.status_code == 200, r.text


def expected_counts(svc, cid):
    """Recount straight from the stored records: campaign JSON, drafts table, contact interactions."""
    cands = svc.store.candidates(cid)["candidates"]
    profiles = svc.store.research(cid)["profiles"].values()
    drafts = svc.cache.list_drafts(cid)
    inter = lambda kind: len(
        {
            r["candidate_id"]
            for r in svc.cache.q("SELECT candidate_id FROM interactions WHERE campaign_id=? AND kind=?", (cid, kind))
        }
    )
    return {
        "discovered": len(cands),
        "researched": sum(p["status"] in ("researched", "needs_contact_review") for p in profiles),
        "contactable": sum(bool(p.get("email_verified_on_page")) for p in profiles),
        "research_failed": sum(p["status"] == "research_failed" for p in profiles),
        "drafted": len({d["candidate_id"] for d in drafts if d["status"] != "blocked"}),
        "approved": len({d["candidate_id"] for d in drafts if d["status"] in ("approved", "gmail_draft_created")}),
        "scheduled": None,
        "sent": len({d["candidate_id"] for d in drafts if d.get("invited_at")}),
        "replied": inter("reply"),
        "interested": inter("interested"),
        "declined": inter("decline"),
        "bounced": inter("bounce"),
        "meeting_booked": inter("meeting"),
    }


def plus(a, b):
    return {k: None if a[k] is None else a[k] + b[k] for k in a}


def test_analytics_and_notifications_acceptance(tmp_path):
    root = tmp_path / "data"
    svc = service(root)
    with TestClient(create_app(service=svc, token="t"), headers={"x-app-token": "t"}) as client:
        # ---- fixture campaigns through multiple outcomes
        a, (avery, jordan, sam) = campaign(
            client,
            "Rutgers/Princeton professors working on computational neurodevelopment who may work with undergraduates",
            None,
        )
        b, (mira, theo) = campaign(client, "startup founders working on climate tech", "startup")
        for cid, cand in ((a, avery), (a, jordan), (b, mira), (b, theo)):
            send(client, cid, cand)
        # sent 5 hours ago, so time-to-reply is measurable
        svc.cache.x(
            "UPDATE milestones SET at=at-5*3600 WHERE campaign_id=? AND candidate_id=? AND stage='sent'", (a, avery)
        )
        for kind in ("replied", "interested", "meeting_booked"):
            outcome(client, a, avery, kind)
        outcome(client, a, jordan, "bounced")
        outcome(client, b, mira, "replied")
        outcome(client, b, mira, "declined")
        # outcomes need a recorded send first
        assert (
            client.post(f"/api/campaigns/{a}/candidates/{sam}/outcomes", json={"outcome": "replied"}).status_code == 409
        )

        # a failing job shows up as a failure and a notification
        async def boom(*_):
            raise RuntimeError("fixture failure")

        svc.jobs.handlers["discover"] = boom
        client.post(f"/api/campaigns/{b}/discover")
        wait(client, b, lambda p: p["jobs"].get("failed"))

        # ---- every dashboard count equals a recount of stored records
        exp_a, exp_b = expected_counts(svc, a), expected_counts(svc, b)
        assert exp_a["replied"] == 1 and exp_a["bounced"] == 1 and exp_b["declined"] == 1  # outcomes really stored
        rep = client.get("/api/analytics").json()
        assert rep["counts"] == plus(exp_a, exp_b)
        by = {x["group"]: x for x in rep["breakdown"]}
        assert by[a]["counts"] == exp_a and by[b]["counts"] == exp_b
        assert rep["rates"] == {
            "verified_contact_rate": round(2 / 4, 4),
            "research_failure_rate": round(1 / 5, 4),
            "approval_rate": 1.0,
            "reply_rate": 0.5,
            "positive_response_rate": 0.5,
        }
        assert 4.9 < by[a]["time_to_reply_hours"]["median"] < 5.1
        assert rep["counts"]["scheduled"] is None and rep["usage"]["estimated_cost_usd"] is None  # unknown, not 0
        assert rep["usage"]["api_calls"] == sum(svc.cache.usage(c)["api_calls"] for c in (a, b)) > 0
        assert rep["usage"]["processing_seconds"] is not None
        assert {f["type"] for f in rep["failures"]} >= {"job_failed", "research_failed"}
        assert all(r["campaign_id"] in (a, b) and r["candidate_id"] for r in rep["rows"])  # links back to records
        org = client.get("/api/analytics", params={"group_by": "organization"}).json()
        assert all(x["usage"] is None for x in org["breakdown"])  # not attributable: unknown
        empty = client.get("/api/analytics", params={"campaign_id": a, "group_by": "sender"}).json()
        assert [x["group"] for x in empty["breakdown"]] == [None]  # no identity attached: unknown sender

        timeline = client.get(f"/api/campaigns/{a}/candidates/{avery}").json()["timeline"]
        assert {t["stage"] for t in timeline} == {
            "discovered",
            "researched",
            "contactable",
            "drafted",
            "approved",
            "sent",
            "replied",
            "interested",
            "meeting_booked",
        }

        # ---- repeated processing changes nothing
        notes_before = {n["dedupe_key"] for n in client.get("/api/notifications").json()["items"]}
        svc.jobs.handlers["discover"] = svc._do_discover
        client.post(f"/api/campaigns/{a}/drafts/generate", json={"candidate_ids": [avery, jordan, sam]})
        wait(client, a, lambda p: True)
        client.post(f"/api/campaigns/{a}/research", json={"candidate_ids": [avery, jordan]})
        wait(client, a, lambda p: True)
        client.post(f"/api/campaigns/{a}/drafts/{avery}/mark-contacted")
        outcome(client, a, avery, "replied")
        analytics.backfill(svc)
        assert client.get("/api/analytics").json()["counts"] == rep["counts"]
        assert {n["dedupe_key"] for n in client.get("/api/notifications").json()["items"]} == notes_before
        kinds = [n["kind"] for n in client.get("/api/notifications").json()["items"]]
        assert kinds.count("reply") == 2 and "research_complete" in kinds and "review_needed" in kinds
        assert "job_failed" in kinds

        # follow-up due: theo sent 8 days ago with no response -> exactly one reminder
        svc.cache.x(
            "UPDATE milestones SET at=at-8*86400 WHERE campaign_id=? AND candidate_id=? AND stage='sent'", (b, theo)
        )
        client.get("/api/notifications")
        client.get("/api/notifications")
        due = [n for n in client.get("/api/notifications").json()["items"] if n["kind"] == "followup_due"]
        assert [(n["campaign_id"], n["candidate_id"]) for n in due] == [(b, theo)]

        # ---- filters: campaign and date range
        only_a = client.get("/api/analytics", params={"campaign_id": a}).json()
        assert only_a["counts"] == exp_a and {r["campaign_id"] for r in only_a["rows"]} == {a}
        today = date.today().isoformat()
        assert client.get("/api/analytics", params={"start": today, "end": today}).json()["counts"]["discovered"] == 5
        future = (date.today() + timedelta(days=2)).isoformat()
        nothing = client.get("/api/analytics", params={"start": future}).json()
        assert nothing["counts"]["discovered"] == 0 and nothing["rates"]["reply_rate"] is None
        jan = datetime(2026, 1, 15).timestamp()
        svc.cache.x("UPDATE milestones SET at=? WHERE campaign_id=?", (jan, b))
        in_jan = client.get("/api/analytics", params={"start": "2026-01-01", "end": "2026-01-31"}).json()
        assert in_jan["counts"] == exp_b and {r["campaign_id"] for r in in_jan["rows"]} == {b}
        assert (
            client.get("/api/analytics", params={"campaign_id": a, "start": "2026-01-01", "end": "2026-01-31"}).json()[
                "counts"
            ]["discovered"]
            == 0
        )
        assert client.get("/api/analytics", params={"start": "2026-02-01", "end": "2026-01-01"}).status_code == 400

        # ---- CSV export matches the JSON report
        full = client.get("/api/analytics").json()
        r = client.get("/api/analytics/export", params={"kind": "rows"})
        assert r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(r.text)))
        assert {(x["campaign_id"], x["candidate_id"]) for x in rows} == {
            (x["campaign_id"], x["candidate_id"]) for x in full["rows"]
        }
        assert sum(bool(x["replied_at"]) for x in rows) == full["counts"]["replied"]
        agg = list(csv.DictReader(io.StringIO(client.get("/api/analytics/export", params={"kind": "aggregate"}).text)))
        assert {x["group"]: int(x["sent"]) for x in agg} == {x["group"]: x["counts"]["sent"] for x in full["breakdown"]}
        assert all(x["scheduled"] == "" and x["estimated_cost_usd"] == "" for x in agg)  # unknown stays blank

        # ---- read / dismiss
        items = client.get("/api/notifications").json()["items"]
        read_id, dismiss_id = items[0]["id"], items[1]["id"]
        assert client.post(f"/api/notifications/{read_id}/read").status_code == 200
        assert client.post(f"/api/notifications/{dismiss_id}/dismiss").status_code == 200
        assert client.post("/api/notifications/999999/read").status_code == 404
        unread = client.get("/api/notifications").json()["unread"]

    # ---- restart over the same files: notification state persists
    svc2 = service(root)
    with TestClient(create_app(service=svc2, token="t"), headers={"x-app-token": "t"}) as client:
        n = client.get("/api/notifications").json()
        assert n["unread"] == unread
        assert dismiss_id not in {x["id"] for x in n["items"]}
        assert next(x for x in n["items"] if x["id"] == read_id)["read_at"]
        assert dismiss_id in {
            x["id"] for x in client.get("/api/notifications", params={"include_dismissed": True}).json()["items"]
        }
        assert client.get("/api/analytics", params={"campaign_id": a}).json()["counts"] == exp_a
        client.post("/api/notifications/read-all")
        assert client.get("/api/notifications").json()["unread"] == 0
