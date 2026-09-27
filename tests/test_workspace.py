import base64
import email
import time

from fastapi.testclient import TestClient

from app import demo
from app.api import create_app
from app.cache import Cache
from app.campaigns import CampaignService
from app.gmail import GmailDrafts
from app.storage import CampaignStore
from app.workspace import Workspace


BIO = "I'm Nitu, a Rutgers undergraduate organizing student founder programs."


class _Exec:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class CapturingGmail:
    def __init__(self): self.store = {}
    def users(self): return self
    def drafts(self): return self
    def create(self, userId, body):
        def save():
            message = email.message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]))
            did = f"d{len(self.store) + 1}"; self.store[did] = message
            return {"id": did}
        return _Exec(save)
    def get(self, userId, id, format):
        msg = self.store[id]
        return _Exec(lambda: {"message": {"payload": {"headers": [
            {"name": k, "value": v} for k, v in msg.items()]}}})
    def list(self, userId, maxResults):
        return _Exec(lambda: {"drafts": [{"id": x} for x in self.store]})


def wait(client, cid, test, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        progress = client.get(f"/api/campaigns/{cid}/progress").json()
        if not progress["jobs"].get("queued") and not progress["jobs"].get("running") and test(progress):
            return
        time.sleep(.03)
    raise AssertionError(progress)


def test_messaging_workspace_acceptance_flow(tmp_path):
    data_root = tmp_path / "outside-git-data"
    store, cache = CampaignStore(data_root), Cache(data_root / "workspace.sqlite3")
    gmail_service = CapturingGmail()
    workspace = Workspace(cache, data_root)
    service = CampaignService(store, cache, demo.DemoModel(cache), demo.demo_fetcher(cache),
                              GmailDrafts(gmail_service), workspace)
    with TestClient(create_app(service=service, token="t"), headers={"x-app-token": "t"}) as client:
        intake = {"mode": "outreach", "subtype": "speaker_mentor", "raw_request": "Rutgers AI founders panel",
                  "organizations": ["Rutgers Entrepreneur Society"], "locations": [], "research_areas": [],
                  "industries": ["AI"], "work_style": "remote", "other_criteria": None,
                  "outreach_goal": "join our student founder panel", "event_details": "virtual panel in October",
                  "sender_background": BIO, "source_urls": []}
        cid = client.post("/api/campaigns", json={"intake": intake, "budget": 40}).json()["campaign_id"]
        client.post(f"/api/campaigns/{cid}/discover")
        wait(client, cid, lambda p: bool(p["candidates"].get("discovered")))
        ids = [x["candidate_id"] for x in client.get(f"/api/campaigns/{cid}").json()["candidates"]]
        client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": ids[:2]})
        wait(client, cid, lambda p: sum(p["candidates"].get(x, 0) for x in ("researched", "needs_contact_review")) >= 2)

        # Generate and approve against immutable speaker template v1.
        client.post(f"/api/campaigns/{cid}/drafts/generate",
                    json={"candidate_ids": [ids[0]], "template_id": "speaker_invitation", "template_version": 1})
        wait(client, cid, lambda p: p["drafts"].get("needs_review") == 1)
        assert client.post(f"/api/campaigns/{cid}/drafts/{ids[0]}/approve").status_code == 200

        # Editing creates v2 and immediately withdraws the approval for v1.
        v1 = client.get("/api/templates/speaker_invitation").json()
        v2 = client.put("/api/templates/speaker_invitation", json={**v1,
            "subject": "Rutgers speaker invitation for {{first_name}}", "change_note": "acceptance test edit"}).json()
        assert v2["version"] == 2 and v2["invalidated_approvals"] == 1
        old = cache.get_draft(cid, ids[0], "speaker_invitation.v1")
        assert old["status"] == "needs_review"

        # A club deck is stored outside Git, selected for the campaign, and captured by v2.
        deck = client.post("/api/attachments", json={"filename": "rutgers-club-deck.pdf",
            "media_type": "application/pdf", "kind": "club_deck",
            "content_base64": base64.b64encode(b"%PDF-1.4\nclub deck\n%%EOF").decode()}).json()
        assert (data_root / "attachments" / deck["stored_name"]).is_file()
        client.put(f"/api/campaigns/{cid}/assets", json={"attachment_ids": [deck["attachment_id"]], "content_ids": []})
        client.post(f"/api/campaigns/{cid}/drafts/generate",
                    json={"candidate_ids": [ids[0]], "template_id": "speaker_invitation", "template_version": 2})
        wait(client, cid, lambda p: len(cache.list_drafts(cid)) == 2)
        approval = client.post(f"/api/campaigns/{cid}/drafts/{ids[0]}/approve").json()
        assert [x["display_name"] for x in approval["attachments"]] == ["rutgers-club-deck.pdf"]

        # Attachment changes invalidate; restoring and re-reviewing produces the exact Gmail MIME attachment.
        client.put(f"/api/campaigns/{cid}/assets", json={"attachment_ids": [], "content_ids": []})
        assert cache.get_draft(cid, ids[0])["status"] == "needs_review"
        client.put(f"/api/campaigns/{cid}/assets", json={"attachment_ids": [deck["attachment_id"]], "content_ids": []})
        assert client.post(f"/api/campaigns/{cid}/drafts/{ids[0]}/approve").status_code == 200
        result = client.post(f"/api/campaigns/{cid}/gmail-drafts", json={"candidate_ids": [ids[0]]}).json()["results"][0]
        assert result["result"].startswith("created")
        assert [part.get_filename() for part in gmail_service.store["d1"].walk() if part.get_filename()] == ["rutgers-club-deck.pdf"]

        # Marking the invitation records prior contact. A rule must be previewed, records causality, then applies.
        client.post(f"/api/campaigns/{cid}/drafts/{ids[0]}/mark-invited")
        rule = client.post(f"/api/campaigns/{cid}/rules",
                           json={"kind": "exclude_contacted", "config": {"limit": 10}}).json()
        assert client.post(f"/api/campaigns/{cid}/rules/{rule['rule_id']}/run",
                           json={"dry_run": False}).status_code == 409
        preview = client.post(f"/api/campaigns/{cid}/rules/{rule['rule_id']}/run",
                              json={"dry_run": True}).json()
        assert any(a["candidate_id"] == ids[0] and a["action"] == "exclude" for a in preview["actions"])
        applied = client.post(f"/api/campaigns/{cid}/rules/{rule['rule_id']}/run",
                              json={"dry_run": False}).json()
        assert any(a["candidate_id"] == ids[0] and a["status"] == "applied" for a in applied["actions"])

        # Neither a bulk rule nor direct approval/Gmail can override a durable DNC decision.
        client.put(f"/api/campaigns/{cid}/candidates/{ids[1]}/do-not-contact",
                   json={"do_not_contact": True, "reason": "recipient requested no contact"})
        blocked = client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": [ids[1]]}).json()
        assert blocked["blocked_do_not_contact"] == [ids[1]]
        override = client.post(f"/api/campaigns/{cid}/rules", json={"kind": "draft_verified",
            "config": {"limit": 10, "override_do_not_contact": True, "auto_approve": True}}).json()
        assert client.post(f"/api/campaigns/{cid}/rules/{override['rule_id']}/run",
                           json={"dry_run": True}).status_code == 409
        assert client.get("/api/policies").json()["overridable"] is False


def test_template_and_attachment_validation(tmp_path):
    cache = Cache(tmp_path / "db.sqlite3")
    workspace = Workspace(cache, tmp_path)
    try:
        workspace.create_template({"name": "Bad", "category": "speaker_invitation",
                                   "subject": "Hi {{made_up}}", "body": "Body"})
        assert False, "unknown placeholder accepted"
    except ValueError as e:
        assert "unknown template variables" in str(e)
    try:
        workspace.add_attachment({"filename": "../deck.pdf", "media_type": "application/pdf",
                                  "kind": "club_deck", "content_base64": base64.b64encode(b"x").decode()})
        assert False, "path traversal accepted"
    except ValueError as e:
        assert "unsafe attachment filename" in str(e)
