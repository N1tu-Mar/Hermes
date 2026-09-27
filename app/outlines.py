"""Deterministic template routing and outline building. No LLM calls here.

Edit TEMPLATES to change structure/tone per audience. Bump a template's
version when you change it: that invalidates earlier approvals.
"""
import re

TEMPLATES = {
    "research_professor": {
        "version": "research_professor.v1",
        "audience": "professor",
        "greeting": "Dear Professor {last_name},",
        "sections": ["who the sender is", "the professor's specific relevant work",
                     "why the match makes sense", "short ask about undergraduate research opportunities"],
        "default_ask": "whether you might have room for an undergraduate researcher in your group",
        "signoff": "Best regards,",
        "tone": "respectful, concise, curious; no flattery",
        "maximum_length": 180,
    },
    "startup": {
        "version": "startup.v1",
        "audience": "startup founder or team member",
        "greeting": "Hi {first_name},",
        "sections": ["one-line intro of sender", "specific thing about their product/work",
                     "why sender is reaching out", "small, concrete ask"],
        "default_ask": "a 15-minute chat",
        "signoff": "Thanks,",
        "tone": "direct, warm, brief",
        "maximum_length": 140,
    },
    "speaker_invite": {
        "version": "speaker_invite.v1",
        "audience": "potential speaker or mentor",
        "greeting": "Hi {first_name},",
        "sections": ["the Rutgers organization and sender's role", "specific reason for inviting this person",
                     "proposed format/timing if supplied", "clear reply request"],
        "default_ask": "whether you would be open to speaking with our members",
        "signoff": "Best,",
        "tone": "enthusiastic but professional, specific",
        "maximum_length": 190,
    },
    "rsvp_followup": {
        "version": "rsvp_followup.v1",
        "audience": "previously invited speaker",
        "greeting": "Hi {first_name},",
        "sections": ["reference the earlier invitation (date recorded by the app)",
                     "brief restatement of the event", "simple yes/no RSVP request"],
        "default_ask": "a quick yes/no on whether you can join",
        "signoff": "Best,",
        "tone": "short, polite, no pressure",
        "maximum_length": 110,
    },
}


class OutlineBlocked(Exception):
    pass


def route_template(intake, followup=False):
    """Pure routing from confirmed mode/subtype."""
    subtype = intake.get("subtype")
    if subtype == "speaker_mentor":
        return "rsvp_followup" if followup else "speaker_invite"
    if followup:
        raise OutlineBlocked("follow-ups only exist for speaker invitations")
    if subtype == "startup":
        return "startup"
    return "research_professor"  # research mode and research_professor subtype


def _names(full):
    full = re.sub(r"\([^)]*\)", " ", full or "")  # drop "(demo)", "(she/her)" etc.
    parts = [p for p in full.replace(",", " ").split() if p.lower().rstrip(".") not in {"dr", "prof", "professor"}]
    return (parts[0] if parts else "there"), (parts[-1] if parts else "")


def pick_evidence(profile, k=2):
    """Only verified, sourced evidence qualifies; prefer ones not about contact info."""
    ev = [(i, e) for i, e in enumerate(profile.get("evidence") or []) if e.get("source_url") and e.get("claim")]
    ev.sort(key=lambda ie: "email" in ie[1]["claim"].lower())
    return ev[:k]


def build_outline(intake, profile, sender_background, followup=False, earlier_invite=None):
    name = route_template(intake, followup)
    t = TEMPLATES[name]
    if name == "rsvp_followup" and not earlier_invite:
        raise OutlineBlocked("no earlier invitation recorded for this candidate; follow-up blocked")
    if not sender_background or not sender_background.strip():
        raise OutlineBlocked("sender background is required before drafting")
    evidence = pick_evidence(profile)
    if not evidence:
        raise OutlineBlocked("no sourced evidence to personalize with")
    first, last = _names(profile.get("name"))
    ask = (intake.get("outreach_goal") or "").strip() or t["default_ask"]
    return {
        "template": name,
        "template_version": t["version"],
        "audience": t["audience"],
        "greeting": t["greeting"].format(first_name=first, last_name=last or first),
        "sender_context": sender_background.strip(),
        "evidence_ids": [f"e{i}" for i, _ in evidence],
        "evidence": [{"id": f"e{i}", "claim": e["claim"], "source_url": e["source_url"]} for i, e in evidence],
        "specific_connection": profile.get("fit_reason") or "",
        "ask": ask,
        "event_details": intake.get("event_details") if name in ("speaker_invite", "rsvp_followup") else None,
        "earlier_invite": earlier_invite,
        "sections": t["sections"],
        "signoff": t["signoff"],
        "tone": t["tone"],
        "maximum_length": t["maximum_length"],
    }
