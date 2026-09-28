"""Opt-in sending: default-off, approval, durable queue, reconciliation, stops, time rules.

Offline and deterministic: a fake Gmail client (same call shape as googleapiclient)
and an injected clock. Scheduler ticks are driven by hand (send_every=None).
"""

import base64
import email
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httplib2
import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from app import demo, writer
from app.api import create_app
from app.cache import Cache
from app.campaigns import CampaignService, Rejected, parse_request
from app.gmail import GmailDrafts
from app.sending import Blocked
from app.storage import CampaignStore

NY = ZoneInfo("America/New_York")
BIO = "I'm Nitu, a Rutgers undergraduate studying computer science and cognitive science."
SENDER = "nitu@example.com"


def at(*args, tz=NY):
    return datetime(*args, tzinfo=tz).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Crash(BaseException):
    """Process death: not an Exception, so nothing in the app catches it."""


class FakeGmail:
    """Mimics service.users().drafts().create/get/list/send(...).execute() and users().getProfile()."""

    def __init__(self):
        self.drafts_, self.sent, self.n = {}, [], 0
        self.send_mode = None  # None | "timeout_after" | "timeout_before" | "crash_after" | "reject"
        self.on_create = None

    def users(self):
        return self

    def drafts(self):
        return self

    def getProfile(self, userId):
        return _Exec(lambda: {"emailAddress": SENDER})

    def create(self, userId, body):
        def run():
            if self.on_create:
                self.on_create()
            self.n += 1
            did = f"r{self.n}"
            self.drafts_[did] = email.message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]))
            return {"id": did}

        return _Exec(run)

    def get(self, userId, id, format):
        def run():
            if id not in self.drafts_:
                raise HttpError(httplib2.Response({"status": "404"}), b"not found")
            return {
                "id": id,
                "message": {"payload": {"headers": [{"name": k, "value": v} for k, v in self.drafts_[id].items()]}},
            }

        return _Exec(run)

    def list(self, userId, maxResults):
        return _Exec(lambda: {"drafts": [{"id": d} for d in self.drafts_]})

    def send(self, userId, body):
        def run():
            mode, self.send_mode = self.send_mode, None
            if mode == "timeout_before":
                raise TimeoutError("socket timeout")
            if mode == "reject":
                raise HttpError(httplib2.Response({"status": "400"}), b"invalid To header")
            if body["id"] not in self.drafts_:
                raise HttpError(httplib2.Response({"status": "404"}), b"draft not found")
            msg = self.drafts_.pop(body["id"])  # Gmail consumes the draft
            self.sent.append(msg)
            if mode == "timeout_after":
                raise TimeoutError("socket timeout after Gmail accepted")
            if mode == "crash_after":
                raise Crash()
            return {"id": f"m{len(self.sent)}", "threadId": f"t{len(self.sent)}", "labelIds": ["SENT"]}

        return _Exec(run)


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


def make_service(root, fake, clock, can_send=True):
    store = CampaignStore(root / "data")
    cache = Cache(root / "data" / "cache.sqlite3")
    return CampaignService(
        store,
        cache,
        demo.DemoModel(cache),
        demo.demo_fetcher(cache),
        GmailDrafts(fake, can_send=can_send) if fake else None,
        clock=clock,
        send_every=None,
    )


def add_person(svc, cid, n, email_addr="avery.lin@demo.example.edu", verified=True):
    """A researched candidate with an approved draft whose inputs are current."""
    cand = f"c_{n:03d}"
    svc.store.update_candidates(
        cid,
        lambda d: d["candidates"].append(
            {
                "candidate_id": cand,
                "name": f"Avery Lin{n}",
                "organization": "Rutgers",
                "role": "Professor",
                "profile_url": f"https://demo.example.edu/{n}",
                "discovery_source_url": None,
                "status": "researched",
            }
        ),
    )
    profile = {
        "candidate_id": cand,
        "name": f"Avery Lin{n}",
        "status": "researched",
        "contact_email": email_addr,
        "email_verified_on_page": verified,
        "fit_reason": "Studies infant attention.",
        "evidence": [{"claim": "Studies infant attention.", "source_url": f"https://demo.example.edu/{n}"}],
    }
    svc.store.update_research(cid, lambda d: d["profiles"].__setitem__(cand, profile))
    outline, _ = svc._outline(cid, cand, False)
    svc.cache.upsert_draft(
        cid,
        cand,
        outline["template_version"],
        input_hash=writer.input_hash(outline),
        subject=f"Question about your lab {n}",
        body="Hello, I read about your work.",
        evidence_ids=["e0"],
        outline=outline,
        issues=[],
        status="approved",
    )
    return cand


def make_campaign(svc):
    intake, _ = parse_request("Rutgers professors working on computational neurodevelopment")
    return svc.create({**intake, "sender_background": BIO})


def enable(svc, cid):
    svc.outbox.update_settings({"enabled": True, "spacing_seconds": 60})
    svc.outbox.set_campaign_enabled(cid, True)


def schedule(svc, cid, cand, when=None):
    prev = svc.preview_send(cid, cand, when)
    return svc.confirm_send(cid, cand, prev["approval_hash"], when)


def events(svc, send_id):
    return [a["event"] for a in svc.outbox.detail(send_id)["audit"]]


@pytest.fixture
def world(tmp_path):
    fake, clock = FakeGmail(), Clock(at(2026, 9, 28, 14, 0))  # Monday 2pm New York
    svc = make_service(tmp_path, fake, clock)
    svc.outbox.recover()
    cid = make_campaign(svc)
    return svc, fake, clock, cid, tmp_path


# ------------------------------------------------------------------ default: impossible
def test_default_configuration_cannot_send(world):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    s = svc.outbox.settings()
    assert s["enabled"] is False and not svc.outbox.campaign_enabled(cid)
    prev = svc.preview_send(cid, cand)  # preview still works in draft-only mode
    assert prev["message"]["recipient"] == "avery.lin@demo.example.edu" and prev["message"]["sender"] == SENDER
    assert any("disabled globally" in b for b in prev["blockers"])
    assert any("this campaign" in b for b in prev["blockers"])
    with pytest.raises(Blocked):
        svc.confirm_send(cid, cand, prev["approval_hash"])
    # even a row forced into the queue past every approval check does not go out
    svc.cache.x(
        """INSERT INTO sends (send_id, idempotency_key, campaign_id, candidate_id, template_version, sender, recipient,
                   subject, body, attachments, scheduled_at, approval, approval_hash, approved_at, status)
                   VALUES ('snd_forced','k',?,?,'x',?,?,'s','b','[]',?,'{}','h',?,'scheduled')""",
        (cid, cand, SENDER, "avery.lin@demo.example.edu", clock.t - 10, clock.t - 10),
    )
    for _ in range(3):
        clock.t += 600
        assert svc.outbox.tick() is None
    assert fake.sent == [] and fake.drafts_ == {}
    assert "avery.lin@demo.example.edu" in svc.export(cid)  # export keeps working


def test_campaign_switch_and_send_scope_are_each_required(world):
    svc, fake, clock, cid, tmp = world
    cand = add_person(svc, cid, 1)
    svc.outbox.update_settings({"enabled": True})
    with pytest.raises(Blocked, match="this campaign"):
        schedule(svc, cid, cand)
    svc.outbox.set_campaign_enabled(cid, True)
    svc.outbox.gmail.can_send = False  # token granted only gmail.compose
    with pytest.raises(Blocked, match="gmail.send"):
        schedule(svc, cid, cand)
    no_gmail = make_service(tmp / "other", None, clock)
    c2 = make_campaign(no_gmail)
    cand2 = add_person(no_gmail, c2, 1)
    assert any("not connected" in b for b in no_gmail.preview_send(c2, cand2)["blockers"])


# ------------------------------------------------------------------ schedule, restart, once
def test_scheduled_send_survives_restart_and_runs_once(world):
    svc, fake, clock, cid, tmp = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    row = schedule(svc, cid, cand, "2026-09-28T15:00")  # naive = configured timezone
    assert row["status"] == "scheduled" and row["scheduled_at"] == at(2026, 9, 28, 15, 0)
    assert svc.outbox.tick() is None and fake.sent == []  # not due yet
    # restart: a brand-new process over the same files
    svc2 = make_service(tmp, fake, clock)
    svc2.outbox.recover()
    clock.t = at(2026, 9, 28, 15, 0, 5)
    assert svc2.outbox.tick() == row["send_id"]
    for _ in range(5):
        clock.t += 3600
        svc2.outbox.tick()
    assert len(fake.sent) == 1 and fake.drafts_ == {}
    msg = fake.sent[0]
    assert (msg["To"], msg["Subject"]) == ("avery.lin@demo.example.edu", "Question about your lab 1")
    d = svc2.outbox.detail(row["send_id"])
    assert d["status"] == "sent" and d["gmail_message_id"] == "m1"
    assert events(svc2, row["send_id"]) == ["approved", "scheduled", "attempt", "gmail_draft_created", "sent"]
    assert d["audit"][0]["detail"]["payload"]["body"] == "Hello, I read about your work."
    assert svc2.outbox.locked(cid, cand)
    with pytest.raises(Rejected):  # a sent message cannot be edited
        svc2.edit_draft(cid, cand, "x", "y")


def test_double_confirm_is_idempotent_and_one_live_send_per_draft(world):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    prev = svc.preview_send(cid, cand, "2026-09-28T16:00")
    a = svc.confirm_send(cid, cand, prev["approval_hash"], "2026-09-28T16:00")
    b = svc.confirm_send(cid, cand, prev["approval_hash"], "2026-09-28T16:00")
    assert a["send_id"] == b["send_id"]
    with pytest.raises(Blocked, match="already has"):
        schedule(svc, cid, cand, "2026-09-28T17:00")
    svc.outbox.cancel(a["send_id"])
    assert schedule(svc, cid, cand, "2026-09-28T17:00")["status"] == "scheduled"  # re-approval after cancel is fine


# ------------------------------------------------------------------ timeouts and crashes
def test_timeout_after_gmail_accepted_is_reconciled_not_resent(world):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    row = schedule(svc, cid, cand)
    fake.send_mode = "timeout_after"
    svc.outbox.tick()
    assert svc.outbox.row(row["send_id"])["status"] == "uncertain"
    for _ in range(4):
        clock.t += 600
        svc.outbox.tick()
    assert len(fake.sent) == 1
    assert svc.outbox.row(row["send_id"])["status"] == "sent"
    assert "uncertain" in events(svc, row["send_id"]) and events(svc, row["send_id"])[-1] == "sent"


def test_timeout_before_gmail_accepted_retries_exactly_once(world):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    row = schedule(svc, cid, cand)
    fake.send_mode = "timeout_before"
    svc.outbox.tick()
    clock.t += 61
    svc.outbox.tick()  # reconciles (draft still there = not sent) and resends the same draft
    clock.t += 61
    svc.outbox.tick()
    assert len(fake.sent) == 1 and svc.outbox.row(row["send_id"])["status"] == "sent"
    assert "reconciled_not_sent" in events(svc, row["send_id"])
    assert fake.n == 1  # no second Gmail draft was created


def test_crash_between_gmail_and_local_write_recovers_without_duplicate(world):
    svc, fake, clock, cid, tmp = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    row = schedule(svc, cid, cand)
    fake.send_mode = "crash_after"
    with pytest.raises(Crash):
        svc.outbox.tick()
    assert svc.outbox.row(row["send_id"])["status"] == "sending"
    svc2 = make_service(tmp, fake, clock)  # restart
    svc2.outbox.recover()
    assert svc2.outbox.row(row["send_id"])["status"] == "uncertain"
    for _ in range(3):
        clock.t += 600
        svc2.outbox.tick()
    assert len(fake.sent) == 1 and svc2.outbox.row(row["send_id"])["status"] == "sent"
    assert "recovered_after_crash" in events(svc2, row["send_id"])


def test_gmail_rejection_fails_without_retry(world):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    row = schedule(svc, cid, cand)
    fake.send_mode = "reject"
    svc.outbox.tick()
    clock.t += 600
    svc.outbox.tick()
    r = svc.outbox.row(row["send_id"])
    assert r["status"] == "failed" and "HttpError" in r["error"] and r["attempts"] == 1
    assert fake.sent == []


# ------------------------------------------------------------------ stops
def test_emergency_stop_cancels_pending_and_blocks_in_flight(world):
    svc, fake, clock, cid, _ = world
    a, b = add_person(svc, cid, 1), add_person(svc, cid, 2, "b@demo.example.edu")
    enable(svc, cid)
    ra = schedule(svc, cid, a, "2026-09-28T15:00")
    rb = schedule(svc, cid, b, "2026-09-28T15:30")
    out = svc.outbox.emergency_stop()
    assert set(out["cancelled"]) == {ra["send_id"], rb["send_id"]}
    assert out["enabled"] is False and out["emergency_stop"] is True
    clock.t = at(2026, 9, 28, 16, 0)
    svc.outbox.tick()
    svc.outbox.update_settings({"enabled": True})  # re-enabling does not revive cancelled messages
    svc.outbox.pause(False)
    svc.outbox.tick()
    assert fake.sent == []
    assert events(svc, ra["send_id"])[-1] == "cancelled"

    # emergency stop landing between draft creation and the send call
    c = add_person(svc, cid, 3, "c@demo.example.edu")
    rc = schedule(svc, cid, c)
    fake.on_create = svc.outbox.emergency_stop
    svc.outbox.tick()
    assert fake.sent == [] and svc.outbox.row(rc["send_id"])["status"] == "cancelled"
    assert "held_before_send" in events(svc, rc["send_id"])


def test_pause_holds_queue_until_unpaused(world):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    row = schedule(svc, cid, cand)
    svc.outbox.pause(True)
    svc.outbox.tick()
    assert fake.sent == [] and svc.outbox.row(row["send_id"])["status"] == "scheduled"
    svc.outbox.pause(False)
    svc.outbox.tick()
    assert len(fake.sent) == 1


def test_campaign_disabled_after_scheduling_holds_message(world):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    schedule(svc, cid, cand)
    svc.outbox.set_campaign_enabled(cid, False)
    svc.outbox.tick()
    assert fake.sent == []


# ------------------------------------------------------------------ time rules
def test_quiet_hours_follow_configured_timezone_and_dst(world):
    svc, fake, clock, cid, _ = world
    q = svc.outbox.quiet  # default 20:00-08:00 America/New_York
    assert q(at(2026, 9, 28, 7, 59)) and not q(at(2026, 9, 28, 8, 0))
    assert not q(at(2026, 9, 28, 19, 59)) and q(at(2026, 9, 28, 20, 0)) and q(at(2026, 9, 29, 2, 0))
    utc = ZoneInfo("UTC")
    assert not q(at(2026, 7, 1, 12, 30, tz=utc))  # 08:30 EDT
    assert q(at(2026, 12, 1, 12, 30, tz=utc))  # 07:30 EST: same UTC time, quiet after DST ends
    svc.outbox.update_settings({"timezone": "Asia/Kolkata"})
    assert not q(at(2026, 12, 1, 12, 30, tz=utc))  # 18:00 IST
    assert svc.outbox.parse_time("2026-12-01T09:00") == at(2026, 12, 1, 9, 0, tz=ZoneInfo("Asia/Kolkata"))
    svc.outbox.update_settings({"timezone": "America/New_York", "quiet_start": "09:00", "quiet_end": "17:00"})
    assert q(at(2026, 9, 28, 12, 0)) and not q(at(2026, 9, 28, 18, 0))
    with pytest.raises(ValueError):
        svc.outbox.update_settings({"timezone": "Mars/Olympus"})
    with pytest.raises(ValueError):
        svc.outbox.update_settings({"quiet_start": "25:00"})


def test_quiet_hours_reject_scheduling_and_defer_due_messages(world):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    with pytest.raises(Blocked, match="quiet hours"):
        schedule(svc, cid, cand, "2026-09-28T22:00")
    with pytest.raises(Blocked, match="past"):
        schedule(svc, cid, cand, "2026-09-28T09:00")
    row = schedule(svc, cid, cand, "2026-09-28T19:59")
    svc.outbox.update_settings({"quiet_start": "19:00"})  # quiet hours changed after approval
    clock.t = at(2026, 9, 28, 20, 30)
    assert svc.outbox.tick() is None
    clock.t = at(2026, 9, 29, 7, 59)
    assert svc.outbox.tick() is None
    clock.t = at(2026, 9, 29, 8, 0)
    assert svc.outbox.tick() == row["send_id"] and len(fake.sent) == 1


def test_rate_limits_and_spacing(world):
    svc, fake, clock, cid, _ = world
    ids = [add_person(svc, cid, n, f"p{n}@demo.example.edu") for n in range(1, 5)]
    enable(svc, cid)
    svc.outbox.update_settings({"spacing_seconds": 300, "hourly_limit": 2, "daily_limit": 3})
    for c in ids[:3]:
        schedule(svc, cid, c)
    schedule(svc, cid, ids[3], "2026-09-28T19:00")
    assert svc.outbox.tick() and len(fake.sent) == 1
    clock.t += 299
    assert svc.outbox.tick() is None  # spacing
    clock.t += 1
    assert svc.outbox.tick() and len(fake.sent) == 2
    clock.t += 600
    assert svc.outbox.tick() is None  # hourly limit
    clock.t += 3600
    assert svc.outbox.tick() and len(fake.sent) == 3
    clock.t += 3600
    assert svc.outbox.tick() is None  # daily limit
    clock.t = at(2026, 9, 29, 14, 5)
    assert svc.outbox.tick() and len(fake.sent) == 4


def test_long_overdue_message_needs_new_approval(world):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    row = schedule(svc, cid, cand, "2026-09-28T15:00")
    svc.outbox.update_settings({"enabled": False})
    clock.t = at(2026, 9, 30, 15, 0)
    svc.outbox.update_settings({"enabled": True})
    svc.outbox.tick()
    assert fake.sent == [] and svc.outbox.row(row["send_id"])["status"] == "failed"


# ------------------------------------------------------------------ recipients and approval integrity
@pytest.mark.parametrize(
    "addr,verified,why",
    [(None, False, "missing"), ("not-an-address", True, "malformed"), ("x@demo.example.edu", False, "not verified")],
)
def test_bad_recipients_are_refused(world, addr, verified, why):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1, addr, verified)
    enable(svc, cid)
    assert any(why in b for b in svc.preview_send(cid, cand)["blockers"])
    with pytest.raises(Blocked, match=why):
        schedule(svc, cid, cand)


def test_suppressed_addresses_blocked_and_outcomes_suppress(world):
    svc, fake, clock, cid, _ = world
    a, b = add_person(svc, cid, 1), add_person(svc, cid, 2, "b@demo.example.edu")
    enable(svc, cid)
    svc.outbox.suppress("AVERY.LIN@demo.example.edu", "do_not_contact")
    with pytest.raises(Blocked, match="do_not_contact"):
        schedule(svc, cid, a)
    rb = schedule(svc, cid, b)
    svc.outbox.tick()
    svc.outbox.record_outcome(rb["send_id"], "bounced")
    assert svc.outbox.row(rb["send_id"])["status"] == "bounced"
    assert svc.outbox.recipient_problem("b@demo.example.edu", True) == "recipient is on the bounced list"
    # suppressing after scheduling cancels the pending message
    c = add_person(svc, cid, 3, "c@demo.example.edu")
    rc = schedule(svc, cid, c, "2026-09-28T16:00")
    svc.outbox.suppress("c@demo.example.edu", "declined")
    assert svc.outbox.row(rc["send_id"])["status"] == "cancelled"
    with pytest.raises(Blocked):
        svc.outbox.record_outcome(rc["send_id"], "replied")


def test_changed_message_needs_new_confirmation_and_edits_cancel(world):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    prev = svc.preview_send(cid, cand, "2026-09-28T16:00")
    with pytest.raises(Blocked, match="changed"):  # different time than confirmed
        svc.confirm_send(cid, cand, prev["approval_hash"], "2026-09-28T16:30")
    row = svc.confirm_send(cid, cand, prev["approval_hash"], "2026-09-28T16:00")
    svc.edit_draft(cid, cand, "New subject", "New body")
    assert svc.outbox.row(row["send_id"])["status"] == "cancelled"
    # a draft changed behind the queue's back is caught right before sending
    svc.cache.upsert_draft(cid, cand, row["template_version"], status="approved")
    row2 = schedule(svc, cid, cand)
    svc.cache.upsert_draft(cid, cand, row["template_version"], body="sneaky change")
    svc.outbox.tick()
    assert fake.sent == [] and svc.outbox.row(row2["send_id"])["error"].startswith("draft changed")


def test_approved_content_and_audit_are_immutable(world):
    svc, fake, clock, cid, _ = world
    cand = add_person(svc, cid, 1)
    enable(svc, cid)
    row = schedule(svc, cid, cand)
    with pytest.raises(sqlite3.IntegrityError):
        svc.cache.x("UPDATE sends SET body='changed' WHERE send_id=?", (row["send_id"],))
    with pytest.raises(sqlite3.IntegrityError):
        svc.cache.x("DELETE FROM send_audit")
    with pytest.raises(sqlite3.IntegrityError):
        svc.cache.x("UPDATE send_audit SET event='x'")
    assert [a["event"] for a in svc.outbox.global_audit()][:2] == ["campaign_sending_enabled", "settings_changed"]


# ------------------------------------------------------------------ HTTP + MCP surfaces
def test_http_flow(tmp_path):
    fake, clock = FakeGmail(), Clock(at(2026, 9, 28, 14, 0))
    svc = make_service(tmp_path, fake, clock)
    with TestClient(create_app(service=svc, token="t"), headers={"x-app-token": "t"}) as client:
        cid = make_campaign(svc)
        cand = add_person(svc, cid, 1)
        st = client.get("/api/sending").json()
        assert st["enabled"] is False and st["gmail_can_send"] is True
        prev = client.post(f"/api/campaigns/{cid}/sends/preview", json={"candidate_id": cand}).json()
        body = {"candidate_id": cand, "approval_hash": prev["approval_hash"]}
        assert client.post(f"/api/campaigns/{cid}/sends", json=body).status_code == 409
        assert client.get(f"/api/campaigns/{cid}/export").status_code == 200
        assert client.patch("/api/sending/settings", json={"enabled": True}).json()["enabled"] is True
        assert client.patch("/api/sending/settings", json={"bogus": 1}).status_code == 400
        client.patch(f"/api/campaigns/{cid}/sending", json={"enabled": True})
        assert client.get(f"/api/campaigns/{cid}").json()["sending_enabled"] is True
        prev = client.post(
            f"/api/campaigns/{cid}/sends/preview", json={"candidate_id": cand, "scheduled_at": "2026-09-28T16:00"}
        ).json()
        assert prev["blockers"] == [] and prev["scheduled_local"].startswith("2026-09-28 16:00")
        r = client.post(
            f"/api/campaigns/{cid}/sends",
            json={**body, "approval_hash": prev["approval_hash"], "scheduled_at": "2026-09-28T16:00"},
        ).json()
        assert client.get(f"/api/campaigns/{cid}/sends").json()[0]["send_id"] == r["send_id"]
        assert client.get(f"/api/campaigns/{cid}").json()["candidates"][0]["send_status"] == "scheduled"
        assert client.post(f"/api/sends/{r['send_id']}/cancel").json()["status"] == "cancelled"
        assert client.post(f"/api/sends/{r['send_id']}/cancel").status_code == 409
        assert [a["event"] for a in client.get(f"/api/sends/{r['send_id']}").json()["audit"]] == [
            "approved",
            "scheduled",
            "cancelled",
        ]
        assert client.post("/api/sending/pause").json()["paused"] is True
        assert client.post("/api/sending/unpause").json()["paused"] is False
        client.post("/api/suppressions", json={"email": "z@x.org", "reason": "do_not_contact"})
        assert client.get("/api/suppressions").json()[0]["email"] == "z@x.org"
        assert client.post("/api/sending/emergency-stop").json()["emergency_stop"] is True
        assert client.get("/api/sends/snd_nope").status_code == 404
    assert fake.sent == []


def test_mcp_exposes_only_safe_sending_tools():
    from app import mcp_server

    for name in ("sending_status", "preview_send", "list_sends", "cancel_send", "pause_sending", "emergency_stop"):
        assert callable(getattr(mcp_server, name))
    for name in ("confirm_send", "send_now", "enable_sending", "unpause_sending", "approve_draft"):
        assert not hasattr(mcp_server, name)
