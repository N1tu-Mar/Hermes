"""Integrated outbound delivery state, MIME fidelity, and projection convergence."""

import base64
import email
import email.policy
import json
from types import SimpleNamespace

from app import demo, writer
from app.cache import Cache
from app.campaigns import CampaignService, parse_request
from app.gmail import GmailDrafts
from app.storage import CampaignStore
from app.workspace import Workspace

DAY = 86400
BIO = "I'm Nitu, a Rutgers undergraduate studying computer science and cognitive science."


class Clock:
    def __init__(self, value=1_800_000_000):
        self.value = value

    def __call__(self):
        return self.value


class ApiError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = SimpleNamespace(status=status)


class Request:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class Provider:
    """Gmail service double with distinct draft and message resources."""

    def __init__(self, clock):
        self.clock = clock
        self.draft_store = {}
        self.message_store = {}
        self.mode = None
        self.read_error = False
        self.send_calls = 0
        self.created_mime = []

    def users(self):
        return self

    def drafts(self):
        return SimpleNamespace(create=self._draft_create, get=self._draft_get, list=self._draft_list,
                               send=self._draft_send)

    def messages(self):
        return SimpleNamespace(get=self._message_get)

    def threads(self):
        return SimpleNamespace(get=self._thread_get)

    def getProfile(self, userId):
        return Request(lambda: {"emailAddress": "sender@example.edu"})

    @staticmethod
    def _metadata(record):
        return {"id": record["id"], "threadId": record["threadId"], "labelIds": record["labelIds"],
                "internalDate": str(int(record["at"] * 1000)),
                "payload": {"headers": [{"name": k, "value": v} for k, v in record["mime"].items()]}}

    def _draft_create(self, userId, body):
        def run():
            msg = email.message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]), policy=email.policy.default)
            number = len(self.message_store) + 1
            did, mid, thread = f"d{number}", f"m{number}", body["message"].get("threadId") or f"t{number}"
            record = {"id": mid, "threadId": thread, "labelIds": ["DRAFT"], "at": self.clock(), "mime": msg}
            self.draft_store[did] = record
            self.message_store[mid] = record
            self.created_mime.append(msg)
            return {"id": did, "message": {"id": mid, "threadId": thread}}
        return Request(run)

    def _draft_get(self, userId, id, format):
        def run():
            if self.read_error:
                raise TimeoutError("provider read unavailable")
            if id not in self.draft_store:
                raise ApiError(404)
            return {"id": id, "message": self._metadata(self.draft_store[id])}
        return Request(run)

    def _draft_list(self, userId, maxResults):
        return Request(lambda: {"drafts": [{"id": did} for did in self.draft_store]})

    def _draft_send(self, userId, body):
        def run():
            self.send_calls += 1
            mode, self.mode = self.mode, None
            if mode == "timeout_before":
                raise TimeoutError("before acceptance")
            if body["id"] not in self.draft_store:
                raise ApiError(404)
            record = self.draft_store.pop(body["id"])
            record.update(labelIds=["SENT"], at=self.clock())
            if mode == "timeout_after":
                raise TimeoutError("after acceptance")
            return {"id": record["id"], "threadId": record["threadId"], "labelIds": ["SENT"]}
        return Request(run)

    def _message_get(self, userId, id, format, metadataHeaders):
        def run():
            if self.read_error:
                raise TimeoutError("provider read unavailable")
            if id not in self.message_store:
                raise ApiError(404)
            return self._metadata(self.message_store[id])
        return Request(run)

    def _thread_get(self, userId, id, format, metadataHeaders):
        def run():
            if self.read_error:
                raise TimeoutError("provider read unavailable")
            records = [self._metadata(record) for record in self.message_store.values()
                       if record["threadId"] == id]
            if not records:
                raise ApiError(404)
            return {"id": id, "messages": records}
        return Request(run)

    def delete_draft(self, draft_id):
        record = self.draft_store.pop(draft_id)
        self.message_store.pop(record["id"], None)


def world(tmp_path):
    root, clock = tmp_path / "data", Clock()
    root.mkdir(parents=True)
    cache = Cache(root / "cache.sqlite3")
    workspace = Workspace(cache, root)
    provider = Provider(clock)
    svc = CampaignService(CampaignStore(root), cache, demo.DemoModel(cache), demo.demo_fetcher(cache),
                          GmailDrafts(provider, can_send=True), workspace, clock=clock, send_every=None)
    identity = workspace.create_identity({"display_name": "Nitu", "biography": BIO,
                                          "reply_to": "approved-replies@example.edu"})
    intake, _ = parse_request("Rutgers professors working on computational neurodevelopment")
    cid = svc.create({**intake, "sender_background": BIO, "sender_identity_id": str(identity["id"])})
    attachment = workspace.add_attachment({"filename": "resume.pdf", "media_type": "application/pdf",
                                           "kind": "resume", "identity_id": identity["id"],
                                           "content_base64": base64.b64encode(b"approved resume bytes").decode()})
    workspace.set_assets(cid, "attachment", [attachment["attachment_id"]])

    cand = "c_delivery"
    svc.store.update_candidates(cid, lambda doc: doc["candidates"].append(
        {"candidate_id": cand, "name": "Avery Lin", "organization": "Rutgers", "role": "Professor",
         "profile_url": "https://example.edu/avery", "discovery_source_url": None, "status": "researched"}))
    profile = {"candidate_id": cand, "name": "Avery Lin", "organization": "Rutgers", "role": "Professor",
               "profile_url": "https://example.edu/avery", "status": "researched",
               "contact_email": "avery@example.edu", "email_verified_on_page": True,
               "fit_reason": "Studies infant attention.",
               "evidence": [{"claim": "Studies infant attention.", "source_url": "https://example.edu/avery"}]}
    svc.store.update_research(cid, lambda doc: doc["profiles"].__setitem__(cand, profile))
    outline, _ = svc._outline(cid, cand, False, workspace.default_template(svc.store.candidates(cid)["intake"], False))
    svc.cache.upsert_draft(cid, cand, outline["template_version"], input_hash=writer.input_hash(outline),
                           subject="Research question", body="Hello Avery", evidence_ids=["e0"], outline=outline,
                           issues=[], attachment_ids=[attachment["attachment_id"]],
                           template_id=outline["template"], template_number=1, status="approved")
    svc._link_all(cid)
    svc.outbox.update_settings({"enabled": True, "spacing_seconds": 0, "quiet_start": "00:00", "quiet_end": "00:00"})
    svc.outbox.set_campaign_enabled(cid, True)
    return svc, provider, clock, workspace, identity, attachment, cid, cand


def schedule(svc, cid, cand):
    preview = svc.preview_send(cid, cand)
    return svc.confirm_send(cid, cand, preview["approval_hash"])


def test_exact_mime_and_delivery_projects_once_from_actual_delivery(tmp_path):
    svc, provider, clock, workspace, identity, attachment, cid, cand = world(tmp_path)
    row = schedule(svc, cid, cand)
    svc.ledger.update_identity(identity["id"], {"reply_to": "changed-after-approval@example.edu"})

    assert svc.outbox.tick() == row["send_id"]
    sent = svc.outbox.row(row["send_id"])
    assert sent["status"] == "sent"
    assert (sent["gmail_draft_id"], sent["gmail_draft_message_id"], sent["gmail_message_id"],
            sent["gmail_thread_id"]) == ("d1", "m1", "m1", "t1")
    assert sent["rfc_message_id"].startswith("<m-hermes-")

    mime = provider.created_mime[0]
    assert mime["Reply-To"] == "approved-replies@example.edu"
    parts = list(mime.iter_attachments())
    assert [(part.get_filename(), part.get_content_type(), part.get_payload(decode=True)) for part in parts] == [
        ("resume.pdf", "application/pdf", b"approved resume bytes")]

    assert svc.cache.q("SELECT at FROM milestones WHERE campaign_id=? AND candidate_id=? AND stage='sent'",
                       (cid, cand)) == [{"at": clock.value}]
    contact = svc.outreach.contact(cid, cand)
    assert contact["sent_at"] == clock.value and contact["gmail_message_id"] == "m1"
    followup = svc.cache.q("SELECT step,due_at,status FROM followups WHERE campaign_id=? AND candidate_id=?",
                           (cid, cand))
    assert followup == [{"step": 0, "due_at": clock.value + 7 * DAY, "status": "scheduled"}]
    interactions = svc.cache.q("SELECT meta,at FROM interactions WHERE campaign_id=? AND candidate_id=?",
                               (cid, cand))
    delivered = [item for item in interactions if json.loads(item["meta"] or "{}").get("send_id") == row["send_id"]]
    assert delivered == [{"meta": delivered[0]["meta"], "at": clock.value}]

    for _ in range(3):
        svc.outbox.tick()
        svc.outbox.recover()
    assert len(svc.cache.q("SELECT 1 FROM milestones WHERE campaign_id=? AND candidate_id=? AND stage='sent'",
                           (cid, cand))) == 1
    assert len([item for item in svc.cache.q("SELECT meta FROM interactions WHERE campaign_id=? AND candidate_id=?",
                                             (cid, cand))
                if json.loads(item["meta"] or "{}").get("send_id") == row["send_id"]]) == 1


def test_attachment_hash_is_revalidated_before_any_gmail_draft(tmp_path):
    svc, provider, clock, workspace, identity, attachment, cid, cand = world(tmp_path)
    row = schedule(svc, cid, cand)
    (workspace.attachment_root / attachment["stored_name"]).write_bytes(b"tampered bytes")

    svc.outbox.tick()
    current = svc.outbox.row(row["send_id"])
    assert current["status"] == "cancelled"
    assert "integrity check" in current["error"]
    assert provider.created_mime == [] and provider.send_calls == 0


def test_reconciliation_distinguishes_sent_deleted_and_unresolved(tmp_path):
    # Timeout after acceptance: provider SENT evidence reconciles exactly once.
    svc, provider, clock, workspace, identity, attachment, cid, cand = world(tmp_path / "sent")
    row = schedule(svc, cid, cand)
    provider.mode = "timeout_after"
    svc.outbox.tick()
    assert svc.outbox.row(row["send_id"])["status"] == "uncertain"
    svc.outbox.tick()
    assert svc.outbox.row(row["send_id"])["status"] == "sent" and provider.send_calls == 1

    # Timeout before acceptance followed by manual draft deletion is not delivery and is never resent.
    svc, provider, clock, workspace, identity, attachment, cid, cand = world(tmp_path / "deleted")
    row = schedule(svc, cid, cand)
    provider.mode = "timeout_before"
    svc.outbox.tick()
    provider.delete_draft(svc.outbox.row(row["send_id"])["gmail_draft_id"])
    svc.outbox.tick()
    assert svc.outbox.row(row["send_id"])["status"] == "deleted" and provider.send_calls == 1
    assert svc.cache.q("SELECT 1 FROM milestones WHERE campaign_id=? AND candidate_id=? AND stage='sent'", (cid, cand)) == []

    # An inconclusive provider read leaves the row uncertain across ticks; it cannot trigger another send.
    svc, provider, clock, workspace, identity, attachment, cid, cand = world(tmp_path / "uncertain")
    row = schedule(svc, cid, cand)
    provider.mode = "timeout_before"
    svc.outbox.tick()
    provider.read_error = True
    for _ in range(3):
        clock.value += 60
        svc.outbox.tick()
    assert svc.outbox.row(row["send_id"])["status"] == "uncertain" and provider.send_calls == 1


def test_actual_followup_delivery_and_all_outcomes_converge(tmp_path):
    svc, provider, clock, workspace, identity, attachment, cid, cand = world(tmp_path)
    row = schedule(svc, cid, cand)
    svc.outbox.tick()

    # The next step may be visible while awaiting delivery, but its due time starts at SENT.
    svc.cache.x("UPDATE followups SET status='gmail_draft_created',gmail_message_id='f1' WHERE campaign_id=? "
                "AND candidate_id=? AND step=0", (cid, cand))
    svc.outreach._await_delivery(cid, cand, 1)
    delivered = clock.value + 3 * DAY
    svc.outreach.record_followup_sent(cid, cand, 0,
        {"id": "f1", "thread_id": "t1", "at": delivered, "headers": {"message-id": "<followup-1>"}})
    nxt = svc.cache.q("SELECT status,due_at FROM followups WHERE campaign_id=? AND candidate_id=? AND step=1",
                      (cid, cand))[0]
    assert nxt == {"status": "scheduled", "due_at": delivered + 14 * DAY}

    bounce_at = delivered + 10
    svc.outreach.set_outcome(cid, cand, "bounced", "gmail", at=bounce_at)
    assert svc.outbox.row(row["send_id"])["status"] == "bounced"
    assert svc.outreach.contact(cid, cand)["outcome"] == "bounced"
    assert svc.cache.q("SELECT reason FROM suppressions WHERE email='avery@example.edu'") == [{"reason": "bounced"}]
    assert len(svc.cache.q("SELECT 1 FROM milestones WHERE campaign_id=? AND candidate_id=? AND stage='bounced'",
                           (cid, cand))) == 1

    svc.outreach.set_outcome(cid, cand, "bounced", "gmail", at=bounce_at)
    assert len(svc.cache.q("SELECT 1 FROM interactions WHERE campaign_id=? AND candidate_id=? AND kind='bounce'",
                           (cid, cand))) == 1
    svc.set_outcome(cid, cand, "meeting_booked", "manual correction")
    assert svc.outreach.contact(cid, cand)["outcome"] == "meeting_booked"
    assert svc.outbox.row(row["send_id"])["status"] == "replied"

    svc.sequence(cid, cand, "do_not_contact")
    contact_id = svc.ledger.linked(cid, cand)
    assert svc.outbox.row(row["send_id"])["status"] == "do_not_contact"
    assert svc.ledger.contact(contact_id)["do_not_contact"] is True
    assert svc.cache.q("SELECT reason FROM suppressions WHERE email='avery@example.edu'") == [{"reason": "do_not_contact"}]
    assert {f["status"] for f in svc.cache.q("SELECT status FROM followups WHERE campaign_id=? AND candidate_id=?",
                                             (cid, cand))} <= {"sent", "cancelled"}
