"""Campaign service: lifecycle, selection, approval policy, budget, idempotency.

The only place that decides state transitions. HTTP and MCP both call this
through the API routes; domain modules below it know nothing about either.
"""
import asyncio
import re
import time
from datetime import datetime

from . import outlines, research, writer
from .contracts import clean_intake, dedupe_key, normalize_url
from .jobs import Coordinator
from .openai_client import BudgetExceeded, ModelError


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

    def _remaining(self, campaign_id):
        u = self.cache.usage(campaign_id)
        return u["budget"] - u["api_calls"]

    def _active(self, campaign_id, kind):
        return [j for j in self.cache.jobs(campaign_id) if j["kind"] == kind and j["status"] in ("queued", "running")]

    # ---------------------------------------------------------------- discovery
    def discover(self, campaign_id):
        self._require(campaign_id)
        if self._active(campaign_id, "discover"):
            raise Rejected("discovery already running")
        doc = self.store.candidates(campaign_id)
        if len(doc["candidates"]) >= doc["intake"]["max_candidates"]:
            raise Rejected("candidate limit reached; raise max_candidates to discover more")
        if self._remaining(campaign_id) < 1:
            raise Rejected("API budget exhausted for this campaign")
        return self.jobs.submit(campaign_id, "discover")

    async def _do_discover(self, campaign_id, _):
        doc = self.store.candidates(campaign_id)
        intake, existing = doc["intake"], doc["candidates"]
        keys = {dedupe_key(c["name"], c.get("organization")) for c in existing} | \
               {c["profile_url"] for c in existing if c.get("profile_url")}
        people = await research.discover(self.model, campaign_id, intake,
                                         intake["max_candidates"] - len(existing), keys)

        def add(doc):
            nums = [int(c["candidate_id"][2:]) for c in doc["candidates"] if re.fullmatch(r"c_\d+", c["candidate_id"])]
            n = max(nums, default=0)
            for p in people:
                n += 1
                doc["candidates"].append({"candidate_id": f"c_{n:03d}", "name": p["name"],
                                          "organization": p["organization"], "role": p["role"],
                                          "profile_url": p["profile_url"],
                                          "discovery_source_url": p["discovery_source_url"],
                                          "status": "discovered", "fit_hint": p["fit_hint"]})
        self.store.update_candidates(campaign_id, add)
        self.cache.event(campaign_id, f"discovered {len(people)} new candidates")

    # ---------------------------------------------------------------- selection
    def select(self, campaign_id, candidate_ids, action):
        self._require(campaign_id)
        if action not in ("select", "exclude", "include"):
            raise Rejected("action must be select, exclude, or include")
        ids = set(candidate_ids)

        def apply(doc):
            changed = []
            for c in doc["candidates"]:
                if c["candidate_id"] not in ids:
                    continue
                if action == "exclude":
                    c["status"] = "excluded"
                elif action == "include" and c["status"] == "excluded":
                    c["status"] = "discovered"
                elif action == "select" and c["status"] == "discovered":
                    c["status"] = "selected"
                else:
                    continue
                changed.append(c["candidate_id"])
            return changed
        return self.store.update_candidates(campaign_id, apply)

    # ---------------------------------------------------------------- research
    def _fresh_profile(self, profiles, cand_id):
        p = profiles.get(cand_id)
        return p and p.get("status") in ("researched", "needs_contact_review")

    def research(self, campaign_id, candidate_ids=None, refresh=False):
        """Select + enqueue research. Skips excluded and already-researched unless refresh."""
        self._require(campaign_id)
        cands = self.store.candidates(campaign_id)["candidates"]
        profiles = self.store.research(campaign_id)["profiles"]
        busy = {j["candidate_id"] for j in self._active(campaign_id, "research")}
        wanted = set(candidate_ids) if candidate_ids else None
        todo = [c["candidate_id"] for c in cands
                if c["status"] != "excluded" and c["candidate_id"] not in busy
                and (c["candidate_id"] in wanted if wanted else c["status"] in ("selected", "research_failed"))
                and (refresh or not self._fresh_profile(profiles, c["candidate_id"]))]
        if len(todo) > self._remaining(campaign_id):
            raise Rejected(f"{len(todo)} research calls projected but only {self._remaining(campaign_id)} left in budget")
        self.store.update_candidates(campaign_id, lambda d: [c.update(status="selected") for c in d["candidates"]
                                                             if c["candidate_id"] in todo])
        for cid in todo:
            if refresh:
                self.cache.drop_research(f"{campaign_id}:{cid}:")
        return {"queued": [self.jobs.submit(campaign_id, "research", cid) for cid in todo], "candidates": todo}

    def _set_status(self, campaign_id, cand_id, status, error=None):
        def apply(doc):
            for c in doc["candidates"]:
                if c["candidate_id"] == cand_id:
                    c["status"] = status
                    if error:
                        c["error"] = error
                    else:
                        c.pop("error", None)
        self.store.update_candidates(campaign_id, apply)

    async def _do_research(self, campaign_id, cand_id):
        doc = self.store.candidates(campaign_id)
        cand = next((c for c in doc["candidates"] if c["candidate_id"] == cand_id), None)
        if not cand or cand["status"] == "excluded":
            return
        compact = {k: cand.get(k) for k in ("candidate_id", "name", "organization", "role",
                                            "profile_url", "discovery_source_url")}
        key = f"{campaign_id}:{cand_id}:{research.criteria_hash(doc['intake'])}:{cand.get('profile_url')}"
        self._set_status(campaign_id, cand_id, "researching")
        profile = self.cache.get_research(key)
        if profile:
            self.cache.bump_usage(campaign_id, cache_hits=1)
        else:
            try:
                profile = await research.research_candidate(self.model, self.fetcher, campaign_id, doc["intake"], compact)
            except (BudgetExceeded, ModelError) as e:
                category = "budget" if isinstance(e, BudgetExceeded) else "model"
                self._set_status(campaign_id, cand_id, "research_failed", f"{category}: {str(e)[:120]}")
                self.cache.event(campaign_id, f"{cand['name']}: research failed ({category})")
                raise
            if profile["status"] != "research_failed":
                self.cache.put_research(key, profile)

        def save(rdoc):
            rdoc["profiles"][cand_id] = profile
        self.store.update_research(campaign_id, save)  # persisted immediately: a stop never loses it
        err = None if profile["status"] != "research_failed" else "no sourced evidence (" + \
            ("; ".join(profile.get("notes", {}).get("fetch_errors", [])) or "nothing verifiable") + ")"
        self._set_status(campaign_id, cand_id, profile["status"], err and err[:200])
        self.cache.event(campaign_id, f"{cand['name']}: {profile['status']}")

    # ---------------------------------------------------------------- drafting
    def _outline(self, campaign_id, cand_id, followup):
        intake = self.store.candidates(campaign_id)["intake"]
        profile = self.store.research(campaign_id)["profiles"].get(cand_id)
        if not profile or profile.get("status") not in ("researched", "needs_contact_review"):
            raise outlines.OutlineBlocked("no completed research profile")
        earlier = None
        if followup:
            inv = self.cache.get_draft(campaign_id, cand_id, outlines.TEMPLATES["speaker_invite"]["version"])
            if inv and inv.get("invited_at"):
                earlier = {"invitation_subject": inv["subject"],
                           "invited_on": datetime.fromtimestamp(inv["invited_at"]).strftime("%B %d").replace(" 0", " ")}
        return outlines.build_outline(intake, profile, intake.get("sender_background"), followup, earlier), profile

    def generate(self, campaign_id, candidate_ids, followup=False):
        self._require(campaign_id)
        if not candidate_ids:
            raise Rejected("pass explicit candidate_ids")
        profiles = self.store.research(campaign_id)["profiles"]
        ready = [c for c in candidate_ids if self._fresh_profile(profiles, c)]
        return {"queued": [self.jobs.submit(campaign_id, "write", f"{c}#followup" if followup else c) for c in ready],
                "skipped_not_researched": sorted(set(candidate_ids) - set(ready))}

    async def _do_write(self, campaign_id, arg):
        cand_id, _, flag = arg.partition("#")
        followup = flag == "followup"
        intake = self.store.candidates(campaign_id)["intake"]
        try:
            tname = outlines.route_template(intake, followup)
            version = outlines.TEMPLATES[tname]["version"]
            outline, profile = self._outline(campaign_id, cand_id, followup)
        except outlines.OutlineBlocked as e:
            version = locals().get("version") or "blocked"
            self.cache.upsert_draft(campaign_id, cand_id, version, status="blocked", issues=[str(e)])
            self.cache.event(campaign_id, f"{cand_id}: draft blocked ({e})")
            return
        h = writer.input_hash(outline)
        old = self.cache.get_draft(campaign_id, cand_id, version)
        if old and old["status"] == "gmail_draft_created":
            return  # never regenerate over an existing Gmail draft
        if old and old.get("input_hash") == h and old["status"] in ("needs_review", "approved"):
            self.cache.bump_usage(campaign_id, cache_hits=1)
            return  # same inputs: keep existing draft and its approval
        d = await writer.write_draft(self.model, campaign_id, outline, profile)
        self.cache.upsert_draft(campaign_id, cand_id, version, input_hash=h, subject=d["subject"], body=d["body"],
                                evidence_ids=d["evidence_ids"], outline=outline, issues=d["issues"],
                                status="needs_review")
        self.cache.event(campaign_id, f"{profile['name']}: draft ready for review"
                         + (f" ({len(d['issues'])} flags)" if d["issues"] else ""))

    def edit_draft(self, campaign_id, cand_id, subject, body):
        d = self._draft(campaign_id, cand_id)
        if d["status"] in ("gmail_draft_created", "blocked"):
            raise Rejected(f"cannot edit a draft in state {d['status']}")
        issues = writer.check_draft(subject, body, d["outline"], None, d["evidence_ids"])
        self.cache.upsert_draft(campaign_id, cand_id, d["template_version"], subject=subject, body=body,
                                issues=issues, status="needs_review")
        return self._draft(campaign_id, cand_id)

    def _draft(self, campaign_id, cand_id):
        self._require(campaign_id)
        d = self.cache.get_draft(campaign_id, cand_id)
        if not d:
            raise KeyError(f"no draft for {cand_id}")
        return d

    def _still_current(self, campaign_id, d):
        """Approval is only valid while the outline inputs (template, evidence, ask, bio) are unchanged."""
        try:
            outline, _ = self._outline(campaign_id, d["candidate_id"], d["template_version"].startswith("rsvp_followup"))
        except outlines.OutlineBlocked:
            return False
        return outline["template_version"] == d["template_version"] and writer.input_hash(outline) == d["input_hash"]

    def approve(self, campaign_id, cand_id):
        d = self._draft(campaign_id, cand_id)
        if d["status"] != "needs_review":
            raise Rejected(f"only drafts in needs_review can be approved (is {d['status']})")
        if not self._still_current(campaign_id, d):
            self.cache.upsert_draft(campaign_id, cand_id, d["template_version"], issues=d["issues"] + ["inputs changed since generation; regenerate"])
            raise Rejected("evidence, template, ask, or background changed since this draft was generated; regenerate first")
        self.cache.upsert_draft(campaign_id, cand_id, d["template_version"], status="approved")
        self.cache.event(campaign_id, f"{cand_id}: draft approved")
        return self._draft(campaign_id, cand_id)

    def mark_invited(self, campaign_id, cand_id):
        """User confirms they actually sent the invitation; enables an RSVP follow-up. Never inferred."""
        d = self.cache.get_draft(campaign_id, cand_id, outlines.TEMPLATES["speaker_invite"]["version"])
        if not d or d["status"] not in ("approved", "gmail_draft_created"):
            raise Rejected("only an approved speaker invitation can be marked as sent")
        self.cache.upsert_draft(campaign_id, cand_id, d["template_version"],
                                invited_at=time.time())
        return {"ok": True}

    # ---------------------------------------------------------------- gmail
    async def create_gmail_drafts(self, campaign_id, candidate_ids):
        self._require(campaign_id)
        if not candidate_ids:
            raise Rejected("pass explicit reviewed candidate_ids")
        profiles = self.store.research(campaign_id)["profiles"]
        results = []
        for cand_id in dict.fromkeys(candidate_ids):
            d = self.cache.get_draft(campaign_id, cand_id)
            r = {"candidate_id": cand_id}
            if not d or d["status"] not in ("approved", "gmail_draft_created"):
                r["result"] = "skipped: not approved"
            elif d.get("gmail_draft_id"):
                r.update(result="already created", gmail_draft_id=d["gmail_draft_id"])
            elif not self._still_current(campaign_id, d):
                self.cache.upsert_draft(campaign_id, cand_id, d["template_version"], status="needs_review")
                r["result"] = "skipped: inputs changed, re-review needed"
            elif not self.gmail:
                r.update(result="gmail not connected: preview only", preview=self.export_one(d, profiles.get(cand_id)))
            else:
                r.update(await self._gmail_one(campaign_id, d, profiles.get(cand_id) or {}))
            results.append(r)
        return results

    async def _gmail_one(self, campaign_id, d, profile):
        key = f"{campaign_id}:{d['candidate_id']}:{d['template_version']}"
        tv = d["template_version"]
        if d.get("gmail_attempt_at"):  # earlier attempt with unknown outcome: reconcile before retrying
            try:
                found = await asyncio.to_thread(self.gmail.find_by_key, key)
            except Exception as e:
                return {"result": f"uncertain: reconcile failed ({type(e).__name__}); check Gmail Drafts manually"}
            if found:
                self.cache.upsert_draft(campaign_id, d["candidate_id"], tv, gmail_draft_id=found, status="gmail_draft_created")
                return {"result": "reconciled existing draft", "gmail_draft_id": found}
        to = profile.get("contact_email") if profile.get("email_verified_on_page") else None
        self.cache.upsert_draft(campaign_id, d["candidate_id"], tv, gmail_attempt_at=time.time())
        try:
            gid = await asyncio.to_thread(self.gmail.create, to, d["subject"], d["body"], key)
        except Exception as e:
            return {"result": f"failed ({type(e).__name__}); retry will reconcile first"}
        self.cache.upsert_draft(campaign_id, d["candidate_id"], tv, gmail_draft_id=gid, status="gmail_draft_created")
        self.cache.event(campaign_id, f"{d['candidate_id']}: Gmail draft created")
        return {"result": "created" + ("" if to else " (no verified recipient; add To in Gmail)"), "gmail_draft_id": gid}

    @staticmethod
    def export_one(d, profile):
        to = (profile or {}).get("contact_email") if (profile or {}).get("email_verified_on_page") else ""
        return f"To: {to or '(missing - verify manually)'}\nSubject: {d['subject']}\n\n{d['body']}"

    def export(self, campaign_id):
        self._require(campaign_id)
        profiles = self.store.research(campaign_id)["profiles"]
        return "\n\n-----\n\n".join(self.export_one(d, profiles.get(d["candidate_id"]))
                                    for d in self.cache.list_drafts(campaign_id) if d["status"] in ("approved", "gmail_draft_created"))

    # ---------------------------------------------------------------- views
    def get(self, campaign_id):
        self._require(campaign_id)
        cdoc = self.store.candidates(campaign_id)
        profiles = self.store.research(campaign_id)["profiles"]
        drafts = {}
        for d in self.cache.list_drafts(campaign_id):
            if d["candidate_id"] not in drafts or d["updated_at"] > drafts[d["candidate_id"]]["updated_at"]:
                drafts[d["candidate_id"]] = d
        rows = []
        for c in cdoc["candidates"]:
            p, d = profiles.get(c["candidate_id"]) or {}, drafts.get(c["candidate_id"]) or {}
            rows.append({**c, "email": p.get("contact_email"), "email_verified": p.get("email_verified_on_page", False),
                         "fit_reason": p.get("fit_reason") or c.get("fit_hint"), "evidence_count": len(p.get("evidence") or []),
                         "draft_status": d.get("status"), "draft_flags": len(d.get("issues") or [])})
        return {"campaign_id": campaign_id, "intake": cdoc["intake"], "candidates": rows,
                "demo": getattr(self.model, "demo", False), "gmail_connected": self.gmail is not None}

    def detail(self, campaign_id, cand_id):
        self._require(campaign_id)
        cand = next((c for c in self.store.candidates(campaign_id)["candidates"] if c["candidate_id"] == cand_id), None)
        if not cand:
            raise KeyError(cand_id)
        d = self.cache.get_draft(campaign_id, cand_id)
        if d:
            d = {k: d[k] for k in ("template_version", "subject", "body", "evidence_ids", "status", "issues",
                                   "gmail_draft_id", "invited_at") if k in d}
        return {"candidate": cand, "profile": self.store.research(campaign_id)["profiles"].get(cand_id), "draft": d}

    def progress(self, campaign_id):
        self._require(campaign_id)
        counts, dcounts, jcounts = {}, {}, {}
        for c in self.store.candidates(campaign_id)["candidates"]:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        for d in self.cache.list_drafts(campaign_id):
            dcounts[d["status"]] = dcounts.get(d["status"], 0) + 1
        for j in self.cache.jobs(campaign_id):
            jcounts[j["status"]] = jcounts.get(j["status"], 0) + 1
        u = self.cache.usage(campaign_id)
        return {"candidates": counts, "drafts": dcounts, "jobs": jcounts,
                "usage": {k: u[k] for k in ("api_calls", "cache_hits", "input_tokens", "output_tokens", "budget")},
                "stopped": campaign_id in self.jobs.stopped,
                "recent": self.cache.events(campaign_id)}

    # ---------------------------------------------------------------- control
    def stop(self, campaign_id):
        self._require(campaign_id)
        self.jobs.stop(campaign_id)
        self.cache.event(campaign_id, "stopped by user")
        return {"stopped": True}

    def resume(self, campaign_id):
        """Re-queue interrupted/stopped work. Finished profiles and drafts are skipped by the normal checks."""
        self._require(campaign_id)
        stale = [j for j in self.cache.jobs(campaign_id) if j["status"] in ("interrupted", "stopped")]
        for j in stale:
            self.cache.put_job(j["job_id"], campaign_id, j["kind"], j["candidate_id"], "superseded")
        self.jobs.stopped.discard(campaign_id)
        out = {"research": self.research(campaign_id)}
        if any(j["kind"] == "discover" for j in stale):
            out["discover"] = self.jobs.submit(campaign_id, "discover")
        writes = {j["candidate_id"] for j in stale if j["kind"] == "write"}
        out["write"] = [self.jobs.submit(campaign_id, "write", w) for w in writes]
        self.cache.event(campaign_id, "resumed")
        return out
