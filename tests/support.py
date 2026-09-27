"""Shared test doubles and helpers (imported by conftest and test modules)."""
import base64
import email
import time

BIO = "I'm Nitu, a Rutgers undergraduate studying computer science and cognitive science."


class FakeGmailService:
    """Mimics service.users().drafts().create/get/list(...).execute()."""

    def __init__(self):
        self.store, self.fail_next, self.closed = {}, False, False

    def users(self):
        return self

    def drafts(self):
        return self

    def close(self):
        self.closed = True

    def create(self, userId, body):
        return _Exec(lambda: self._create(body))

    def _create(self, body):
        if self.fail_next:
            self.fail_next = False
            # Simulate timeout AFTER Gmail stored it: the dangerous duplicate case.
            self._store(body)
            raise TimeoutError("socket timeout")
        return {"id": self._store(body)}

    def _store(self, body):
        msg = email.message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]))
        did = f"d{len(self.store) + 1}"
        self.store[did] = msg
        return did

    def get(self, userId, id, format):
        m = self.store[id]
        headers = [{"name": k, "value": v} for k, v in m.items()]
        return _Exec(lambda: {"id": id, "message": {"payload": {"headers": headers}}})

    def list(self, userId, maxResults):
        return _Exec(lambda: {"drafts": [{"id": d} for d in self.store]})


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


def wait(client, cid, until, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        p = client.get(f"/api/campaigns/{cid}/progress").json()
        if until(p):
            return p
        time.sleep(0.05)
    raise AssertionError(f"timeout: {p}")


def idle(p):
    return not p["jobs"].get("queued") and not p["jobs"].get("running")


def make_campaign(client, text, subtype=None, budget=40):
    parsed = client.post("/api/parse", json={"text": text, "subtype": subtype}).json()
    intake = {**parsed["intake"], "sender_background": BIO}
    body = {"intake": intake, "max_candidates": 20, "budget": budget}
    return client.post("/api/campaigns", json=body).json()["campaign_id"]


def run_research_flow(client, cid):
    """discover -> select all -> research -> draft all. Returns candidate ids."""
    client.post(f"/api/campaigns/{cid}/discover")
    wait(client, cid, lambda p: p["candidates"].get("discovered") and idle(p))
    ids = [c["candidate_id"] for c in client.get(f"/api/campaigns/{cid}").json()["candidates"]]
    client.post(f"/api/campaigns/{cid}/select", json={"candidate_ids": ids, "action": "select"})
    client.post(f"/api/campaigns/{cid}/research", json={})
    wait(client, cid, lambda p: idle(p) and not p["candidates"].get("selected") and not p["candidates"].get("researching"))
    client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": ids})
    wait(client, cid, lambda p: idle(p) and p["drafts"].get("needs_review"))
    return ids
