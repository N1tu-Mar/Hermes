"""Schema constants and validation for the two campaign JSON files.

Validation returns a list of problems instead of raising so one malformed
record gets flagged without crashing the whole run.
"""

import re
from urllib.parse import urlsplit, urlunsplit

SCHEMA_VERSION = 1

MODES = {"research", "outreach"}
SUBTYPES = {None, "startup", "research_professor", "speaker_mentor"}
INTAKE_FIELDS = (
    "mode", "subtype", "raw_request", "organizations", "locations",
    "research_areas", "industries", "work_style", "other_criteria",
    "outreach_goal", "event_details", "sender_background", "sender_identity_id", "source_urls",
)
LIST_FIELDS = {"organizations", "locations", "research_areas", "industries", "source_urls"}

CANDIDATE_STATES = {
    "discovered",
    "selected",
    "researching",
    "researched",
    "excluded",
    "needs_contact_review",
    "research_failed",
}
DRAFT_STATES = {"generated", "needs_review", "approved", "gmail_draft_created", "blocked"}

CANDIDATE_KEYS = ("candidate_id", "name", "organization", "role",
                  "profile_url", "discovery_source_url", "status", "ranking",
                  "pinned", "manual_score_adjustment")
PROFILE_KEYS = ("candidate_id", "name", "organization", "role", "contact_email",
                "contact_source_url", "email_verified_on_page", "summary",
                "research_interests", "fit_reason", "evidence", "researched_at", "status")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def normalize_url(url):
    if not url or not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, ""))


def dedupe_key(name, organization):
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"{norm(name)}|{norm(organization)}"


def clean_intake(data):
    """Keep known fields only, coerce list fields, validate mode/subtype."""
    out = {}
    for k in INTAKE_FIELDS:
        v = data.get(k)
        if k in LIST_FIELDS:
            if isinstance(v, str):
                v = [s.strip() for s in v.split(",") if s.strip()]
            v = [str(s).strip() for s in (v or []) if str(s).strip()]
            if k == "source_urls":
                v = list(dict.fromkeys(u for u in map(normalize_url, v) if u))[:10]
        elif v is not None and not isinstance(v, str):
            v = str(v)
        out[k] = v
    if out["mode"] not in MODES:
        raise ValueError("mode must be 'research' or 'outreach'")
    if out["subtype"] not in SUBTYPES:
        raise ValueError(f"unknown subtype {out['subtype']!r}")
    return out


def candidate_problems(c):
    p = []
    if not isinstance(c, dict):
        return ["not an object"]
    for k in ("candidate_id", "name", "status"):
        if not c.get(k):
            p.append(f"missing {k}")
    if c.get("status") and c["status"] not in CANDIDATE_STATES:
        p.append(f"bad status {c['status']!r}")
    extra = set(c) - set(CANDIDATE_KEYS) - {"error", "fit_hint"}
    if extra:
        p.append(f"unexpected fields {sorted(extra)} (no research data in candidates.json)")
    return p


def profile_problems(pr):
    p = []
    if not isinstance(pr, dict):
        return ["not an object"]
    if not pr.get("candidate_id"):
        p.append("missing candidate_id")
    for e in pr.get("evidence") or []:
        if not (
            isinstance(e, dict) and e.get("claim") and normalize_url(e.get("source_url")) and e.get("retrieved_at")
        ):
            p.append(f"unsourced evidence {e!r:.80}")
        if e.get("provenance") == "manual_correction" and e.get("web_verified"):
            p.append("manual evidence mislabeled as web verified")
    if pr.get("email_verified_on_page") and not (pr.get("contact_email") and pr.get("contact_source_url")):
        p.append("email marked verified without email and source")
    if pr.get("provenance", {}).get("contact_email", {}).get("kind") == "manual_correction" and pr.get("email_verified_on_page"):
        p.append("manually corrected email mislabeled as web verified")
    if pr.get("contact_email") and not EMAIL_RE.fullmatch(pr["contact_email"]):
        p.append("malformed contact_email")
    return p


def validate_files(candidates_doc, research_doc):
    """Whole-pair check used after every run. Returns list of problems."""
    probs = []
    for doc, name in ((candidates_doc, "candidates"), (research_doc, "research")):
        if doc.get("schema_version") != SCHEMA_VERSION:
            probs.append(f"{name}: schema_version {doc.get('schema_version')} != {SCHEMA_VERSION}")
    if candidates_doc.get("campaign_id") != research_doc.get("campaign_id"):
        probs.append("campaign_id mismatch between files")
    ids = set()
    for c in candidates_doc.get("candidates", []):
        probs += [f"candidate {c.get('candidate_id')}: {x}" for x in candidate_problems(c)]
        ids.add(c.get("candidate_id"))
    for cid, pr in research_doc.get("profiles", {}).items():
        if cid not in ids:
            probs.append(f"profile {cid}: no matching candidate")
        if pr.get("candidate_id") != cid:
            probs.append(f"profile {cid}: key/candidate_id mismatch")
        probs += [f"profile {cid}: {x}" for x in profile_problems(pr)]
    return probs
