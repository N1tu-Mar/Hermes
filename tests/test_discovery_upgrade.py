import base64
import asyncio
import io
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import demo
from app.api import create_app
from app.cache import Cache
from app.campaigns import CampaignService
from app.ranking import DEFAULT_WEIGHTS, score_candidate
from app.research import (FETCH_TIMEOUT_SECONDS, MAX_PAGE_BYTES, MAX_PDF_BYTES, MAX_PDF_PAGES, PAGES_PER_PERSON, FetchError,
                          Fetcher, html_to_text, pdf_to_text, research_candidate)
from app.storage import CampaignStore

FIXTURES = Path(__file__).parent / "fixtures"


class EvidenceModel:
    def __init__(self, evidence):
        self.evidence = evidence

    async def structured(self, campaign_id, instructions, user_input, schema_name, schema, web_search=False):
        return ({"contact_email": None, "contact_source_url": None, "summary": "Fixture researcher.",
                 "research_interests": ["trustworthy robotics"], "fit_reason": "Topical match.",
                 "evidence": self.evidence}, set())


def test_ambiguous_intake_asks_one_material_question_and_shows_all_fields(tmp_path):
    cache = Cache(tmp_path / "cache.db")
    service = CampaignService(CampaignStore(tmp_path / "data"), cache, demo.DemoModel(cache), demo.demo_fetcher(cache))
    with TestClient(create_app(service, token="t"), headers={"x-app-token": "t"}) as client:
        result = client.post("/api/parse", json={"text": "Find impressive people near me"}).json()
    assert result["question"] == "What topic or industry should the people work in?"
    assert result["requires_confirmation"] is True
    assert result["extraction"] == "deterministic_fallback"
    assert set(result["intake"]) >= {"mode", "subtype", "organizations", "locations", "research_areas",
                                     "industries", "work_style", "other_criteria", "outreach_goal",
                                     "event_details", "sender_background", "source_urls", "raw_request"}


def test_five_candidate_ranking_is_explained_and_missing_never_scores_positive():
    intake = {"subtype": "research_professor", "research_areas": ["trustworthy robotics"],
              "organizations": ["Example University"], "locations": ["New Jersey"]}
    rows = []
    for i in range(5):
        cand = {"name": f"Person {i}", "organization": "Example University", "role": "Professor",
                "fit_hint": "trustworthy robotics in New Jersey", "manual_score_adjustment": 0}
        profile = {"research_interests": ["trustworthy robotics"], "contact_email": f"p{i}@example.edu",
                   "email_verified_on_page": True,
                   "evidence": [{"claim": "Studies robotics", "source_type": "official", "provenance": "web"}]}
        rows.append(score_candidate(cand, profile, intake, DEFAULT_WEIGHTS))
    assert len(rows) == 5 and all(r["score"] > 0 and len(r["explanation"]) == 7 for r in rows)
    blank = score_candidate({"name": "Unknown"}, {}, intake, DEFAULT_WEIGHTS)
    assert blank["criteria"]["contact_availability"]["value"] == 0
    assert blank["criteria"]["evidence_quality"]["value"] == 0
    assert blank["criteria"]["prior_contact_state"]["value"] == 0


def test_html_evidence_is_cited_and_prompt_injection_cannot_become_claim(tmp_path):
    asyncio.run(_html_injection_case(tmp_path))


async def _html_injection_case(tmp_path):
    html = (FIXTURES / "profile.html").read_bytes()
    url = "https://example.edu/faculty/fixture"
    async def handler(request):
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})
    cache = Cache(tmp_path / "cache.db")
    cache.set_budget("c", 10)
    fetcher = Fetcher(cache, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    evidence = [
        {"claim": "Dr. Fixture leads the Trustworthy Robotics Lab and studies human oversight.",
         "source_url": url, "source_locator": "faculty profile"},
        {"claim": "Dr. Fixture won a Nobel Prize.", "source_url": url, "source_locator": None},
    ]
    profile = await research_candidate(EvidenceModel(evidence), fetcher, "c", {"research_areas": ["robotics"]},
        {"candidate_id": "c_1", "name": "Dr. Fixture", "organization": "Example University",
         "role": "Professor", "profile_url": url, "discovery_source_url": url})
    assert [e["claim"] for e in profile["evidence"]] == [evidence[0]["claim"]]
    assert profile["evidence"][0]["source_url"] == url
    assert "IGNORE ALL" not in html_to_text(html.decode())
    assert profile["notes"]["dropped_unsourced_claims"] == 1


def test_fixture_pdf_preserves_page_and_fetch_limits(tmp_path):
    asyncio.run(_pdf_limit_case(tmp_path))


async def _pdf_limit_case(tmp_path):
    body = base64.b64decode((FIXTURES / "profile.pdf.b64").read_text())
    text, pages = pdf_to_text(body)
    assert pages == 1 and "[[page 1]]" in text and "trustworthy robotics" in text

    pdf_url = "https://example.edu/publication.pdf"
    async def pdf_handler(request):
        return httpx.Response(200, content=body, headers={"content-type": "application/pdf"})
    cache = Cache(tmp_path / "cache.db")
    cache.set_budget("c", 10)
    fetcher = Fetcher(cache, httpx.AsyncClient(transport=httpx.MockTransport(pdf_handler)))
    claim = "Dr. Fixture studies trustworthy robotics and human oversight."
    profile = await research_candidate(EvidenceModel([{"claim": claim, "source_url": pdf_url,
                                                        "source_locator": None}]), fetcher, "c",
        {"research_areas": ["robotics"]}, {"candidate_id": "c_1", "name": "Dr. Fixture",
        "organization": "Example University", "role": "Professor", "profile_url": pdf_url,
        "discovery_source_url": pdf_url})
    assert profile["evidence"][0]["source_locator"] == "page 1"

    async def huge_handler(request):
        return httpx.Response(200, content=b"x" * (MAX_PAGE_BYTES + 1), headers={"content-type": "text/html"})
    huge = Fetcher(Cache(tmp_path / "huge.db"), httpx.AsyncClient(transport=httpx.MockTransport(huge_handler)))
    with pytest.raises(FetchError, match="byte limit"):
        await huge.fetch("https://example.edu/huge")

    async def huge_pdf_handler(request):
        return httpx.Response(200, content=b"%PDF-1.4\n" + b"x" * MAX_PDF_BYTES,
                              headers={"content-type": "application/pdf"})
    huge_pdf = Fetcher(Cache(tmp_path / "huge-pdf.db"),
                       httpx.AsyncClient(transport=httpx.MockTransport(huge_pdf_handler)))
    with pytest.raises(FetchError, match=str(MAX_PDF_BYTES)):
        await huge_pdf.fetch("https://example.edu/huge.pdf")

    async def timeout_handler(request):
        raise httpx.ReadTimeout("fixture timeout", request=request)
    timed = Fetcher(Cache(tmp_path / "timeout.db"),
                    httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)))
    with pytest.raises(FetchError, match="ReadTimeout"):
        await timed.fetch("https://example.edu/slow")
    assert FETCH_TIMEOUT_SECONDS == 12.0

    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(MAX_PDF_PAGES + 2):
        writer.add_blank_page(width=100, height=100)
    many = io.BytesIO(); writer.write(many)
    limited, count = pdf_to_text(many.getvalue())
    assert count == MAX_PDF_PAGES and f"truncated after {MAX_PDF_PAGES}" in limited
    assert PAGES_PER_PERSON == 2


def _wait(client, cid):
    import time
    end = time.time() + 5
    while time.time() < end:
        p = client.get(f"/api/campaigns/{cid}/progress").json()
        if not p["jobs"].get("queued") and not p["jobs"].get("running"):
            return
        time.sleep(.02)
    raise AssertionError("jobs did not finish")


def _campaign(client):
    parsed = client.post("/api/parse", json={"text": "professors working on computational neurodevelopment"}).json()
    return client.post("/api/campaigns", json={"intake": parsed["intake"], "budget": 20}).json()["campaign_id"]


def test_manual_contact_correction_is_reused_but_never_web_verified(tmp_path):
    cache = Cache(tmp_path / "cache.db")
    service = CampaignService(CampaignStore(tmp_path / "data"), cache, demo.DemoModel(cache), demo.demo_fetcher(cache))
    with TestClient(create_app(service, token="t"), headers={"x-app-token": "t"}) as client:
        first = _campaign(client); client.post(f"/api/campaigns/{first}/discover"); _wait(client, first)
        cid1 = client.get(f"/api/campaigns/{first}").json()["candidates"][0]["candidate_id"]
        result = client.patch(f"/api/campaigns/{first}/candidates/{cid1}/corrections",
                              json={"field": "contact_email", "value": "corrected@example.edu"}).json()
        assert result["detail"]["profile"]["email_verified_on_page"] is False

        second = _campaign(client); client.post(f"/api/campaigns/{second}/discover"); _wait(client, second)
        cid2 = client.get(f"/api/campaigns/{second}").json()["candidates"][0]["candidate_id"]
        client.post(f"/api/campaigns/{second}/research", json={"candidate_ids": [cid2]}); _wait(client, second)
        detail = client.get(f"/api/campaigns/{second}/candidates/{cid2}").json()
        assert detail["profile"]["contact_email"] == "corrected@example.edu"
        assert detail["profile"]["email_verified_on_page"] is False
        assert detail["profile"]["provenance"]["contact_email"]["kind"] == "manual_correction"
        assert detail["corrections"][0]["original_value"] is None
        assert detail["corrections"][0]["corrected_value"] == "corrected@example.edu"


def test_ranking_controls_and_comparison_api_are_visible(tmp_path):
    cache = Cache(tmp_path / "cache.db")
    service = CampaignService(CampaignStore(tmp_path / "data"), cache, demo.DemoModel(cache), demo.demo_fetcher(cache))
    with TestClient(create_app(service, token="t"), headers={"x-app-token": "t"}) as client:
        cid = _campaign(client); client.post(f"/api/campaigns/{cid}/discover"); _wait(client, cid)
        ids = [c["candidate_id"] for c in client.get(f"/api/campaigns/{cid}").json()["candidates"]]
        configured = client.patch(f"/api/campaigns/{cid}/ranking",
                                  json={"weights": {"topical_relevance": 50, "evidence_quality": 30}}).json()
        assert configured["weights"]["topical_relevance"] == 50
        adjusted = client.patch(f"/api/campaigns/{cid}/candidates/{ids[0]}/ranking",
                                json={"pinned": True, "manual_score_adjustment": 7}).json()
        assert adjusted["pinned"] is True and adjusted["ranking"]["manual_adjustment"] == 7
        compared = client.post(f"/api/campaigns/{cid}/compare", json={"candidate_ids": ids[:2]}).json()
        assert len(compared["candidates"]) == 2
        assert {"freshest_source_at", "official_sources", "third_party_sources", "missing_fields",
                "confidence_limitations"} <= set(compared["candidates"][0])


def test_budget_and_page_counters_remain_enforced(tmp_path):
    cache = Cache(tmp_path / "cache.db")
    service = CampaignService(CampaignStore(tmp_path / "data"), cache, demo.DemoModel(cache), demo.demo_fetcher(cache))
    with TestClient(create_app(service, token="t"), headers={"x-app-token": "t"}) as client:
        parsed = client.post("/api/parse", json={"text": "professors working on robotics https://one.example/x https://two.example/y"}).json()
        cid = client.post("/api/campaigns", json={"intake": parsed["intake"], "budget": 2}).json()["campaign_id"]
        client.post(f"/api/campaigns/{cid}/discover"); _wait(client, cid)
        ids = [c["candidate_id"] for c in client.get(f"/api/campaigns/{cid}").json()["candidates"]]
        rejected = client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": ids[:2]})
        assert rejected.status_code == 409 and "only 1 left" in rejected.json()["detail"]
        client.post(f"/api/campaigns/{cid}/research", json={"candidate_ids": ids[:1]}); _wait(client, cid)
        usage = client.get(f"/api/campaigns/{cid}/progress").json()["usage"]
        assert usage["api_calls"] == usage["budget"] == 2
        assert usage["pages_skipped"] >= 1
        again = client.post(f"/api/campaigns/{cid}/discover")
        assert again.status_code == 409 and "budget exhausted" in again.json()["detail"]
