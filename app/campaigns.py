"""Campaign service: lifecycle, selection, approval policy, budget, idempotency.

The only place that decides state transitions. HTTP and MCP both call this
through the API routes; domain modules below it know nothing about either.
"""
import asyncio
import re

from . import outlines, research, writer
from .contracts import clean_intake, dedupe_key, normalize_url
from .jobs import Coordinator
from .openai_client import BudgetExceeded, ModelError
from .storage import now_iso


class Rejected(Exception):
    """Request not allowed in current state (maps to HTTP 409/400)."""


KNOWN_ORGS = ["Rutgers", "Princeton", "Columbia", "UPenn", "Penn", "NYU", "Cornell", "MIT", "Stanford",
              "Harvard", "Yale", "Rutgers Entrepreneur Society", "Road to Silicon Valley", "NJIT", "Stevens"]
LOCATIONS = ["New Jersey", "NJ", "New York", "NYC", "Philadelphia", "Boston", "San Francisco", "Bay Area",
             "Silicon Valley", "remote", "New Brunswick", "Princeton"]


def parse_request(text, mode_hint=None, subtype_hint=None):
    """Deterministic first pass at the intake fields; the user corrects them in the UI."""
    t = text or ""
    low = t.lower()
    subtype = subtype_hint
    if not subtype:
        if re.search(r"\b(speaker|mentor|panel|club|event|invite|workshop|fireside)", low):
            subtype = "speaker_mentor"
        elif re.search(r"\b(startup|founder|company|companies|ceo|cto)", low):
            subtype = "startup"
        elif re.search(r"\b(professor|faculty|lab|phd|research)", low):
            subtype = "research_professor"
    mode = mode_hint or ("research" if subtype is None and "research" in low else "outreach")
    if mode == "research" and not subtype_hint:
        subtype = "research_professor" if subtype in (None, "research_professor") else subtype

    orgs = [o for o in KNOWN_ORGS if re.search(rf"\b{re.escape(o)}\b", t, re.I)]
    orgs = [o for o in orgs if not any(o != p and o in p for p in orgs)]  # "Rutgers" inside the club name
    locs = [l for l in LOCATIONS if re.search(rf"\b{re.escape(l)}\b", t, re.I) and l not in orgs]
    topic = None
    m = re.search(r"\b(?:working on|work on|in|focused on|focus on|about|doing)\s+([a-z][a-z \-/]{3,60}?)(?=\s+(?:who|that|and|at|for|in)\b|[.,;]|$)", low)
    if m:
        topic = m.group(1).strip()
    ask = None
    m = re.search(r"\b(?:ask|asking|want to|would like to|hoping to)\s+(.{5,120}?)(?:[.;]|$)", t, re.I)
    if m:
        ask = m.group(1).strip()
    work_style = next((w for w in ("remote", "in-person", "hybrid") if w in low), None)
    event = None
    if subtype == "speaker_mentor":
        m = re.search(r"\b(panel|fireside chat|workshop|talk|keynote|office hours|mentorship)\b", low)
        event = m.group(1) if m else None

    intake = {
        "mode": mode, "subtype": subtype, "raw_request": t.strip(),
        "organizations": orgs, "locations": locs,
        "research_areas": [topic] if topic and subtype != "startup" else [],
        "industries": [topic] if topic and subtype == "startup" else [],
        "work_style": work_style, "other_criteria": "undergraduates welcome" if "undergrad" in low else None,
        "outreach_goal": ask, "event_details": event, "sender_background": None,
    }
    missing = essential_missing(intake)
    return intake, missing


def essential_missing(intake):
    """Ask only the one question we cannot proceed without."""
    if not (intake.get("research_areas") or intake.get("industries") or intake.get("other_criteria")):
        return "What topic or industry should the people work in?"
    if intake.get("subtype") == "speaker_mentor" and not intake.get("organizations"):
        return "Which organization is inviting them (e.g. Rutgers Entrepreneur Society, Road to Silicon Valley)?"
    return None


class CampaignService:
    def __init__(self, store, cache, model, fetcher, gmail=None):
        self.store, self.cache, self.model, self.fetcher, self.gmail = store, cache, model, fetcher, gmail
        self.jobs = Coordinator(cache, {"discover": self._do_discover, "research": self._do_research,
                                        "write": self._do_write})

    # ---------------------------------------------------------------- lifecycle
    def startup(self):
        """Mark interrupted jobs resumable and un-stick 'researching' candidates."""
        self.cache.mark_interrupted()
        for cid in self.store.list_ids():
            def unstick(doc):
                for c in doc["candidates"]:
                    if c["status"] == "researching":
                        c["status"] = "selected"
            try:
                self.store.update_candidates(cid, unstick)
            except Exception:
                pass
        self.jobs.start()

    def create(self, intake, max_candidates=20, budget=60):
        intake = clean_intake(intake)
        intake["max_candidates"] = max(1, min(int(max_candidates), 100))
        cid = self.store.create(intake)
        self.cache.set_budget(cid, max(1, min(int(budget), 1000)))
        self.cache.event(cid, "campaign created")
        return cid

    def _require(self, campaign_id):
        if not self.store.exists(campaign_id):
            raise KeyError(campaign_id)

    def update_intake(self, campaign_id, patch):
        self._require(campaign_id)

        def apply(doc):
            merged = {**doc["intake"], **{k: v for k, v in patch.items() if k != "max_candidates"}}
            new = clean_intake(merged)
            new["max_candidates"] = max(1, min(int(patch.get("max_candidates") or doc["intake"].get("max_candidates", 20)), 100))
            doc["intake"] = new
            return new
        intake = self.store.update_candidates(campaign_id, apply)
        if "budget" in patch:
            self.cache.set_budget(campaign_id, int(patch["budget"]))
        return intake

    def list(self):
        out = []
        for cid in self.store.list_ids()[:50]:
            try:
                doc = self.store.candidates(cid)
                out.append({"campaign_id": cid, "subtype": doc["intake"].get("subtype"),
                            "request": (doc["intake"].get("raw_request") or "")[:120],
                            "candidates": len(doc["candidates"]), "created_at": doc.get("created_at")})
            except Exception as e:
                out.append({"campaign_id": cid, "error": str(e)[:100]})
        return out
