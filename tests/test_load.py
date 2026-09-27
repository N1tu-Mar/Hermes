"""Realistic campaign sizes under concurrency: 3 campaigns x 100 people researched and drafted at once,
while other clients read and edit. Checks throughput, no lost JSON updates, and clean files."""
import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import demo
from app.api import create_app
from app.cache import Cache
from app.campaigns import CampaignService
from app.storage import CampaignStore
from tests.support import BIO, idle, wait

N, CAMPAIGNS = 100, 3


class FastModel(demo.DemoModel):
    """Answers every person with sourced evidence, after a small simulated latency."""

    async def structured(self, campaign_id, instructions, user_input, schema_name, schema, web_search=False):
        await asyncio.sleep(0.002)
        if schema_name != "profile":
            return await super().structured(campaign_id, instructions, user_input, schema_name, schema, web_search)
        self.cache.bump_usage(campaign_id, api_calls=1)
        url = user_input.split('"profile_url": "', 1)[1].split('"', 1)[0]
        name = user_input.split('"name": "', 1)[1].split('"', 1)[0]
        return ({"contact_email": f"{url.rsplit('/', 1)[1]}@load.example.edu", "contact_source_url": url,
                 "summary": "s", "research_interests": ["load"], "fit_reason": "fits",
                 "evidence": [{"claim": f"{name} studies load testing.", "source_url": url}]}, {url})


class FakeFetcher:
    async def fetch(self, url):
        who = url.rsplit("/", 1)[1]
        return f"Contact: {who}@load.example.edu", False


@pytest.mark.load
def test_hundreds_of_people_concurrently(tmp_path):
    store = CampaignStore(tmp_path / "data")
    cache = Cache(tmp_path / "data" / "cache.sqlite3")
    svc = CampaignService(store, cache, FastModel(cache), FakeFetcher())
    with TestClient(create_app(service=svc, token="t"), headers={"x-app-token": "t"}) as client:
        cids = []
        for k in range(CAMPAIGNS):
            intake = {"mode": "research", "subtype": "research_professor", "research_areas": ["load"],
                      "sender_background": BIO, "outreach_goal": "a chat"}
            cid = client.post("/api/campaigns", json={"intake": intake, "max_candidates": N, "budget": 1000}).json()["campaign_id"]
            people = [{"candidate_id": f"c_{i:03d}", "name": f"Person{k} Load{i}", "organization": "Load U",
                       "role": "Professor", "profile_url": f"https://load.example.edu/p{k}x{i}",
                       "discovery_source_url": f"https://load.example.edu/p{k}x{i}", "status": "selected"}
                      for i in range(1, N + 1)]
            store.update_candidates(cid, lambda d, people=people: d["candidates"].extend(people))
            cids.append(cid)

        stop, errors = threading.Event(), []

        def reader():  # concurrent UI polling plus intake edits during the run
            while not stop.is_set():
                for cid in cids:
                    for path in ("", "/progress"):
                        r = client.get(f"/api/campaigns/{cid}{path}")
                        if r.status_code != 200:
                            errors.append(r.status_code)
                    client.patch(f"/api/campaigns/{cid}/intake", json={"other_criteria": "undergrads"})

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()
        started = time.monotonic()
        # all campaigns research at once, from parallel request threads
        posts = [threading.Thread(target=lambda c=cid: client.post(f"/api/campaigns/{c}/research", json={}))
                 for cid in cids]
        for t in posts:
            t.start()
        for t in posts:
            t.join()
        for cid in cids:
            wait(client, cid, lambda p: idle(p) and p["candidates"].get("researched") == N, timeout=90)
        research_s = time.monotonic() - started
        ids = [f"c_{i:03d}" for i in range(1, N + 1)]
        for cid in cids:
            client.post(f"/api/campaigns/{cid}/drafts/generate", json={"candidate_ids": ids})
        for cid in cids:
            wait(client, cid, lambda p: idle(p) and p["drafts"].get("needs_review") == N, timeout=90)
        stop.set()
        for t in readers:
            t.join()
        total_s = time.monotonic() - started

        assert not errors
        for cid in cids:
            assert client.get(f"/api/campaigns/{cid}/validate").json()["problems"] == []
            prog = client.get(f"/api/campaigns/{cid}/progress").json()
            assert prog["jobs"] == {"done": 2 * N}  # nothing lost, failed, or duplicated
            assert len(store.research(cid)["profiles"]) == N  # no lost read-modify-write updates
            assert store.candidates(cid)["intake"]["other_criteria"] == "undergrads"
        print(f"\n  load: {CAMPAIGNS * N} people researched in {research_s:.1f}s, "
              f"researched+drafted in {total_s:.1f}s")
        assert total_s < 120
