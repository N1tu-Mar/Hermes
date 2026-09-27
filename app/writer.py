"""Writing worker: outline + verified facts in, subject/body out, then rule checks.

Validation is plain code (no second LLM call). Anything suspicious flags the
draft for review; nothing is ever sent.
"""
import hashlib
import json
import re

from .contracts import EMAIL_RE

WRITER_INSTRUCTIONS = (
    "You write one short, natural, specific outreach email from a Rutgers student. "
    "Use ONLY the facts provided in `evidence`; do not add publications, dates, affiliations, "
    "relationships, or compliments that are not stated there. No URLs, no placeholders like [Name]. "
    "Follow the outline sections in order, respect maximum_length words for the body, and sign with the sender's name "
    "if given in sender_context. Return which evidence ids you used."
)

DRAFT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["subject", "body", "evidence_ids_used"],
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"},
                   "evidence_ids_used": {"type": "array", "items": {"type": "string"}}},
}

URL_RE = re.compile(r"https?://|www\.", re.I)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
DATE_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}\b", re.I)
RELATIONSHIP_RE = re.compile(r"\b(as we discussed|great (to|meeting) (meet|you)|when we met|our (last )?conversation|"
                             r"you (may )?remember me|following up on our)\b", re.I)
PRAISE_RE = re.compile(r"\b(huge fan|world[- ]renowned|legendary|genius|brilliant|groundbreaking|revolutionary)\b", re.I)
PLACEHOLDER_RE = re.compile(r"\[[A-Z][^\]]{0,30}\]|\{\w+\}")


def input_hash(outline):
    return hashlib.sha256(json.dumps(outline, sort_keys=True).encode()).hexdigest()[:16]


def writer_input(outline, recipient):
    """Only outline, sender info, selected facts, recipient, tone. No web pages, no chat history."""
    return json.dumps({"recipient": recipient, "outline": outline}, ensure_ascii=False)


async def write_draft(model, campaign_id, outline, profile):
    recipient = {"name": profile.get("name"), "organization": profile.get("organization"), "role": profile.get("role")}
    data, _ = await model.structured(campaign_id, WRITER_INSTRUCTIONS, writer_input(outline, recipient),
                                     "email_draft", DRAFT_SCHEMA)
    subject, body = (data.get("subject") or "").strip(), (data.get("body") or "").strip()
    used = [i for i in data.get("evidence_ids_used") or [] if i in outline["evidence_ids"]]
    return {"subject": subject, "body": body, "evidence_ids": used,
            "issues": check_draft(subject, body, outline, profile, used)}


def check_draft(subject, body, outline, profile, used_ids):
    """Deterministic guardrails. Returns list of human-readable issues (empty = clean)."""
    issues = []
    text = f"{subject}\n{body}"
    facts = " ".join(e["claim"] for e in outline["evidence"]) + " " + outline["sender_context"] + " " + \
        json.dumps(outline.get("event_details") or "") + " " + json.dumps(outline.get("earlier_invite") or "") + " " + json.dumps(outline.get("prior_contact") or "") + \
        " " + outline["ask"]
    if not subject:
        issues.append("empty subject")
    if not body:
        issues.append("empty body")
    if URL_RE.search(text):
        issues.append("contains a raw URL")
    if PLACEHOLDER_RE.search(text):
        issues.append("contains a template placeholder")
    for m in EMAIL_RE.findall(text):
        if m.lower() not in facts.lower():
            issues.append(f"mentions an email address not in the facts: {m}")
    for rx, label in ((YEAR_RE, "year"), (DATE_RE, "date")):
        for m in rx.finditer(text):
            if m.group(0).lower() not in facts.lower():
                issues.append(f"mentions a {label} not in the facts: {m.group(0)}")
    if RELATIONSHIP_RE.search(text) and not (outline.get("earlier_invite") or outline.get("prior_contact")):
        issues.append("implies a prior relationship that is not recorded")
    if PRAISE_RE.search(text):
        issues.append("unsupported superlative praise")
    if not used_ids:
        issues.append("no verified evidence used; personalization unsupported")
    words = len(body.split())
    if words > outline["maximum_length"] * 1.3:
        issues.append(f"body too long ({words} words, limit {outline['maximum_length']})")
    quoted = re.findall(r"[\"“]([^\"”]{8,})[\"”]", text)  # quoted titles must come from evidence
    for q in quoted:
        if q.lower() not in facts.lower():
            issues.append(f"quotes a title/phrase not in the evidence: \"{q[:60]}\"")
    return issues
