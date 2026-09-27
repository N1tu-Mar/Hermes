"""Reply-aware outreach acceptance flow against a mocked Gmail mailbox (no credentials, no network)."""
import base64
import email
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import demo
from app.api import create_app
from app.cache import Cache
from app.campaigns import CampaignService
from app.gmail import GmailAuthError, GmailDrafts, _run
from app.outlines import OutlineBlocked, build_followup_outline
from app.storage import CampaignStore
from test_core import BIO, idle, make_campaign, wait

DAY = 86400


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = SimpleNamespace(status=status)


class _Req:
    def __init__(self, fn):
        self.execute = fn


class FakeMailbox:
    """drafts.create/get/list and threads.get over an in-memory mailbox. Logs every thread read."""

    def __init__(self):
        self.msgs, self.draft_store, self.thread_reads, self.revoked, self.n = {}, {}, [], False, 0

    def users(self):
        return self

    def drafts(self):
        return SimpleNamespace(create=self._d_create, get=self._d_get, list=self._d_list)

    def threads(self):
        return SimpleNamespace(get=self._t_get)

    def add(self, thread_id, labels, headers, at=None):
        self.n += 1
        mid = f"m{self.n}"
        self.msgs[mid] = {"id": mid, "threadId": thread_id or f"t{self.n}", "labelIds": labels,
                          "internalDate": str(int((at or time.time()) * 1000)),
                          "headers": {"Message-ID": f"<{mid}@fake>", **headers}}
        return self.msgs[mid]

    def send(self, draft_id):
        self.draft_store.pop(draft_id)["labelIds"] = ["SENT"]

    def reply(self, thread_id, frm, **headers):
        return self.add(thread_id, ["INBOX", "UNREAD"], {"From": frm, "Subject": "Re: hi", **headers})

    def _meta(self, m):
        return {"id": m["id"], "threadId": m["threadId"], "labelIds": m["labelIds"], "internalDate": m["internalDate"],
                "payload": {"headers": [{"name": k, "value": v} for k, v in m["headers"].items()]}}

    def _d_create(self, userId, body):
        raw = email.message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]))
        m = self.add(body["message"].get("threadId"), ["DRAFT"], dict(raw.items()))
        did = f"d{len(self.draft_store) + 1}"
        self.draft_store[did] = m
        return _Req(lambda: {"id": did, "message": {"id": m["id"], "threadId": m["threadId"]}})

    def _d_get(self, userId, id, format):
        return _Req(lambda: {"id": id, "message": self._meta(self.draft_store[id])})

    def _d_list(self, userId, maxResults):
        return _Req(lambda: {"drafts": [{"id": d} for d in self.draft_store]})

    def _t_get(self, userId, id, format, metadataHeaders):
        assert format == "metadata"

        def run():
            self.thread_reads.append(id)
            if self.revoked:
                raise HttpError(401)
            msgs = [self._meta(m) for m in self.msgs.values() if m["threadId"] == id]
            if not msgs:
                raise HttpError(404)
            return {"id": id, "messages": msgs}
        return _Req(run)


def build(tmp_path, mailbox, now=None):
    store = CampaignStore(tmp_path / "data")
    cache = Cache(tmp_path / "data" / "cache.sqlite3")
    svc = CampaignService(store, cache, demo.DemoModel(cache), demo.demo_fetcher(cache), GmailDrafts(mailbox))
    if now:
        svc.now = now
    return svc, TestClient(create_app(service=svc, token="t"), headers={"x-app-token": "t"})


def initial_draft(client):
    """Research one professor, approve their draft, create the Gmail draft. Returns (cid, cand)."""
    cid = make_campaign(client, "Rutgers/Princeton professors working on computational neurodevelopment who may work with undergraduates")
    client.post(f"/api/campaigns/{cid}/discover")
    wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
    cand = client.get(f"/api/campaigns/{cid}").json()["candidates"][0]["candidate_id"]
    client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": [cand]})
    wait(client, cid, lambda p: idle(p) and p["candidates"].get("researched"))
    client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": [cand]})
    wait(client, cid, lambda p: idle(p) and p["drafts"].get("needs_review"))
    assert client.post(f"/api/campaigns/{cid}/drafts/{cand}/approve").status_code == 200
    res = client.post(f"/api/campaigns/{cid}/gmail-drafts", json={"candidate_ids": [cand]}).json()["results"]
    assert res[0]["result"] == "created"
    return cid, cand


def contact(client, cid, cand):
    return client.get(f"/api/campaigns/{cid}/candidates/{cand}").json()["contact"]


def sync(client, cid):
    return client.post(f"/api/campaigns/{cid}/gmail-sync").json()


def test_reply_stops_sequence_and_unrelated_mail_is_ignored(tmp_path):
    box = FakeMailbox()
    svc, client = build(tmp_path, box)
    with client:
        cid, cand = initial_draft(client)
        c = contact(client, cid, cand)
        draft_id = next(iter(box.draft_store))
        assert c["gmail_thread_id"] == box.draft_store[draft_id]["threadId"] and c["outcome"] is None
        thread = c["gmail_thread_id"]

        # Unrelated inbox thread with the very same subject: must never be read or stored.
        box.add(None, ["INBOX"], {"From": "stranger@else.com", "Subject": "Undergraduate research interest"})

        box.send(draft_id)
        sync(client, cid)
        c = contact(client, cid, cand)
        assert c["outcome"] == "awaiting_reply" and c["sent_source"] == "gmail" and c["rfc_message_id"]
        q = client.get(f"/api/campaigns/{cid}/followups").json()
        assert [f["step"] for f in q["upcoming"]] == [0]

        sync(client, cid)
        assert set(box.thread_reads) == {thread}
        assert svc.cache.q("SELECT * FROM gmail_seen") == []
        assert contact(client, cid, cand)["outcome"] == "awaiting_reply"

        box.reply(thread, "Avery Lin <avery.lin@demo.example.edu>")
        r = sync(client, cid)
        assert r["replies"] == 1
        assert contact(client, cid, cand)["outcome"] == "replied"
        q = client.get(f"/api/campaigns/{cid}/followups").json()
        assert not q["due"] and not q["upcoming"] and [f["status"] for f in q["completed"]] == ["cancelled"]
        seen = svc.cache.q("SELECT * FROM gmail_seen")
        assert [(s["kind"], s["from_addr"], s["thread_id"]) for s in seen] == [("reply", "avery.lin@demo.example.edu", thread)]

        # Even far past the due date, nothing is generated for a replied contact.
        svc.now = lambda: time.time() + 30 * DAY
        assert sync(client, cid)["followups_queued"] == 0
        kinds = [e["kind"] for e in client.get(f"/api/campaigns/{cid}/candidates/{cand}").json()["timeline"]]
        assert kinds[:2] == ["gmail_draft", "sent"] and "reply" in kinds and "sequence_stopped" in kinds

        row = client.get(f"/api/campaigns/{cid}").json()["candidates"][0]
        assert row["outcome"] == "replied"


def test_no_reply_followup_due_needs_review_once_across_restart(tmp_path):
    box = FakeMailbox()
    later = lambda: time.time() + 8 * DAY
    svc, client = build(tmp_path, box)
    with client:
        cid, cand = initial_draft(client)
        box.send(next(iter(box.draft_store)))
        assert sync(client, cid)["followups_queued"] == 0  # not due yet
        svc.now = later
        assert sync(client, cid)["followups_queued"] == 1
        wait(client, cid, idle)
        q = client.get(f"/api/campaigns/{cid}/followups").json()
        (f,) = q["due"]
        assert f["status"] == "needs_review" and f["subject"] == "Re: Undergraduate research interest"
        assert "following up on my email from" in f["body"] and f["issues"] == []
        assert sync(client, cid)["followups_queued"] == 0
        calls = client.get(f"/api/campaigns/{cid}/progress").json()["usage"]["api_calls"]

    # Restart over the same files: nothing is generated again.
    svc2, client2 = build(tmp_path, box, now=later)
    with client2:
        assert sync(client2, cid)["followups_queued"] == 0
        wait(client2, cid, idle)
        assert client2.get(f"/api/campaigns/{cid}/progress").json()["usage"]["api_calls"] == calls
        rows = svc2.cache.q("SELECT step, status FROM followups WHERE campaign_id=?", (cid,))
        assert rows == [{"step": 0, "status": "needs_review"}]

        # Approve: Gmail draft lands in the same thread, next step is scheduled; approving twice is refused.
        r = client2.post(f"/api/campaigns/{cid}/followups/{cand}/0/approve").json()
        assert r["status"] == "gmail_draft_created"
        fu = box.draft_store[r["gmail_draft_id"]]
        thread = contact(client2, cid, cand)["gmail_thread_id"]
        assert fu["threadId"] == thread and fu["headers"]["In-Reply-To"].startswith("<m")
        assert client2.post(f"/api/campaigns/{cid}/followups/{cand}/0/approve").status_code == 400
        q = client2.get(f"/api/campaigns/{cid}/followups").json()
        assert [f["step"] for f in q["upcoming"]] == [1]


def test_crash_mid_generation_reuses_same_step_and_caps_attempts(tmp_path):
    box = FakeMailbox()
    svc, client = build(tmp_path, box)
    with client:
        cid, cand = initial_draft(client)
        box.send(next(iter(box.draft_store)))
        sync(client, cid)
    # Simulate a crash while step 0 was generating, twice (max_attempts=2).
    svc.cache.x("UPDATE followups SET status='generating', attempts=2 WHERE campaign_id=?", (cid,))
    svc2, client2 = build(tmp_path, box, now=lambda: time.time() + 8 * DAY)
    with client2:
        assert sync(client2, cid)["followups_queued"] == 0
        (f,) = client2.get(f"/api/campaigns/{cid}/followups").json()["blocked"]
        assert "max attempts" in f["issues"][0]
        # Human reschedules: attempts reset, one generation, still a single row.
        assert client2.post(f"/api/campaigns/{cid}/followups/{cand}/0/reschedule",
                            json={"due_at": time.time()}).status_code == 200
        assert sync(client2, cid)["followups_queued"] == 1
        wait(client2, cid, idle)
        assert svc2.cache.q("SELECT count(*) n FROM followups")[0]["n"] == 1


def test_bounce_manual_outcome_audit_dnc_and_revoked_credentials(tmp_path):
    box = FakeMailbox()
    svc, client = build(tmp_path, box)
    with client:
        cid, cand = initial_draft(client)
        thread = contact(client, cid, cand)["gmail_thread_id"]
        box.send(next(iter(box.draft_store)))
        box.reply(thread, "Mail Delivery Subsystem <mailer-daemon@googlemail.com>")
        assert sync(client, cid)["bounces"] == 1
        assert contact(client, cid, cand)["outcome"] == "bounced"

        # Manual correction is audited.
        r = client.post(f"/api/campaigns/{cid}/contacts/{cand}/outcome",
                        json={"outcome": "meeting_booked", "note": "they called instead"})
        assert r.json()["outcome"] == "meeting_booked"
        assert client.post(f"/api/campaigns/{cid}/contacts/{cand}/outcome", json={"outcome": "maybe"}).status_code == 400
        tl = client.get(f"/api/campaigns/{cid}/candidates/{cand}").json()["timeline"]
        audit = [e for e in tl if e["kind"] == "outcome" and e["source"] == "manual"]
        assert audit[-1]["detail"] == "bounced -> meeting_booked: they called instead"

        client.post(f"/api/campaigns/{cid}/contacts/{cand}/sequence", json={"action": "do_not_contact"})
        res = client.post(f"/api/campaigns/{cid}/gmail-drafts", json={"candidate_ids": [cand]}).json()["results"]
        assert res[0]["result"] == "skipped: marked do not contact"

        # Revoked token: sync reports it, nothing crashes, the UI status carries the message.
        # (stopped/closed contacts are never synced, so reopen this one directly)
        svc.cache.x("UPDATE contacts SET outcome='awaiting_reply', sequence_state='active', do_not_contact=0")
        box.revoked = True
        r = sync(client, cid)
        assert "python -m app.gmail" in r["error"]
        assert "python -m app.gmail" in client.get("/api/status").json()["gmail_error"]


def test_followup_never_implies_unrecorded_contact_and_manual_send():
    prof = {"name": "A B", "evidence": [{"claim": "Studies x.", "source_url": "https://a.org/p"}]}
    step = {"template": "followup_nudge", "tone": "brief"}
    with pytest.raises(OutlineBlocked):
        build_followup_outline({"subtype": "startup"}, prof, BIO, step, None)
    o = build_followup_outline({"subtype": "startup"}, prof, BIO, step, {"sent_on": "May 1", "original_subject": "Hi"})
    assert o["prior_contact"]["sent_on"] == "May 1" and o["template_version"] == "followup_nudge.v1"


def test_gmail_error_mapping():
    with pytest.raises(GmailAuthError):
        _run(_Req(lambda: (_ for _ in ()).throw(HttpError(401))))
    with pytest.raises(KeyError):
        _run(_Req(lambda: (_ for _ in ()).throw(HttpError(404))))
    with pytest.raises(HttpError):  # a rate limit is not an auth failure
        _run(_Req(lambda: (_ for _ in ()).throw(HttpError(429))))
