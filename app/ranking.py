"""Deterministic, transparent candidate ranking.

Unknown values always receive zero.  A criterion can only contribute points
when the campaign or research record contains affirmative evidence.
"""

import re
from urllib.parse import urlsplit

DEFAULT_WEIGHTS = {
    "topical_relevance": 25,
    "organization_fit": 15,
    "role_fit": 15,
    "geographic_fit": 10,
    "contact_availability": 10,
    "evidence_quality": 20,
    "prior_contact_state": 5,
}


def clean_weights(value=None):
    value = value or {}
    out = {}
    for key, default in DEFAULT_WEIGHTS.items():
        try:
            out[key] = max(0.0, min(float(value.get(key, default)), 100.0))
        except (TypeError, ValueError):
            out[key] = float(default)
    if not sum(out.values()):
        return {k: float(v) for k, v in DEFAULT_WEIGHTS.items()}
    return out


def _tokens(*values):
    return {x for v in values for x in re.findall(r"[a-z0-9]+", str(v or "").lower()) if len(x) > 2}


def _overlap(wanted, actual):
    wanted, actual = _tokens(*wanted), _tokens(*actual)
    return (len(wanted & actual) / len(wanted)) if wanted and actual else 0.0


def _criterion(value, reason, missing=False):
    return {
        "value": round(max(0.0, min(value, 1.0)), 3),
        "reason": ("Missing data — contributes 0. " if missing else "") + reason,
    }


def score_candidate(candidate, profile, intake, weights=None, prior_contact=None):
    """Return a stable 0–100 score and a human-readable per-criterion audit."""
    profile = profile or {}
    weights = clean_weights(weights)
    topics = list(intake.get("research_areas") or []) + list(intake.get("industries") or [])
    actual_topics = list(profile.get("research_interests") or []) + [
        profile.get("summary"),
        profile.get("fit_reason"),
        candidate.get("fit_hint"),
    ]
    topical = _overlap(topics, actual_topics)
    criteria = {
        "topical_relevance": _criterion(
            topical, f"{round(topical * 100)}% requested-topic term coverage.", not any(actual_topics) or not topics
        )
    }

    orgs = intake.get("organizations") or []
    org_value = max((_overlap([o], [candidate.get("organization")]) for o in orgs), default=0.0)
    criteria["organization_fit"] = _criterion(
        org_value,
        "Organization matches the requested list." if org_value else "No requested organization match.",
        not orgs or not candidate.get("organization"),
    )

    subtype = intake.get("subtype")
    role = (candidate.get("role") or "").lower()
    role_terms = {
        "research_professor": ("professor", "faculty", "researcher", "director"),
        "startup": ("founder", "ceo", "cto", "co-founder"),
        "speaker_mentor": ("founder", "partner", "director", "head", "mentor", "speaker"),
    }.get(subtype, ())
    role_value = 1.0 if role and any(x in role for x in role_terms) else 0.0
    criteria["role_fit"] = _criterion(
        role_value,
        "Role matches the requested person type." if role_value else "Role has no explicit requested-type match.",
        not role,
    )

    locations = intake.get("locations") or []
    geographic = _overlap(locations, [candidate.get("organization"), candidate.get("fit_hint"), profile.get("summary")])
    if intake.get("work_style") == "remote" and "remote" in _tokens(candidate.get("fit_hint"), profile.get("summary")):
        geographic = max(geographic, 1.0)
    criteria["geographic_fit"] = _criterion(
        geographic,
        "Location/work-style terms found." if geographic else "No affirmative geographic match.",
        not locations and not intake.get("work_style"),
    )

    email = profile.get("contact_email")
    verified = bool(profile.get("email_verified_on_page"))
    contact = 1.0 if verified else (0.5 if email else 0.0)
    criteria["contact_availability"] = _criterion(
        contact,
        "Contact is web-verified."
        if verified
        else "Contact exists but is unverified."
        if email
        else "No contact found.",
        not email,
    )

    evidence = profile.get("evidence") or []
    if evidence:
        official = sum(e.get("source_type") == "official" for e in evidence)
        web = sum(e.get("provenance", "web") == "web" for e in evidence)
        quality = min(1.0, (len(evidence) / 3) * 0.7 + (official / len(evidence)) * 0.2 + (web / len(evidence)) * 0.1)
        reason = f"{len(evidence)} cited claim(s); {official} official-source, {web} web-derived."
    else:
        quality, reason = 0.0, "No cited evidence."
    criteria["evidence_quality"] = _criterion(quality, reason, not evidence)

    # Unknown is deliberately not equivalent to 'never contacted'.
    prior_value = 1.0 if prior_contact == "confirmed_not_contacted" else 0.0
    prior_reason = {
        "confirmed_not_contacted": "Confirmed not previously contacted.",
        "contacted": "Previously contacted; contributes 0.",
        "do_not_contact": "Do-not-contact state; contributes 0.",
    }.get(prior_contact, "Prior-contact state unknown; contributes 0.")
    criteria["prior_contact_state"] = _criterion(prior_value, prior_reason, prior_contact is None)

    denom = sum(weights.values()) or 1
    base = sum(weights[k] * criteria[k]["value"] for k in weights) / denom * 100
    try:
        adjustment = max(-100.0, min(float(candidate.get("manual_score_adjustment") or 0), 100.0))
    except (TypeError, ValueError):
        adjustment = 0.0
    total = max(0.0, min(100.0, base + adjustment))
    for key, item in criteria.items():
        item["weight"] = weights[key]
        item["points"] = round(weights[key] * item["value"] / denom * 100, 1)
    explanation = [
        f"{k.replace('_', ' ').title()}: {v['points']:.1f} points — {v['reason']}" for k, v in criteria.items()
    ]
    if adjustment:
        explanation.append(f"Manual adjustment: {adjustment:+.1f} points (not evidence).")
    return {
        "score": round(total, 1),
        "base_score": round(base, 1),
        "manual_adjustment": adjustment,
        "criteria": criteria,
        "explanation": explanation,
    }


def source_type(url, candidate):
    """Conservative official/third-party label; uncertainty stays third-party."""
    try:
        host = (urlsplit(url).hostname or "").lower()
        profile_host = (urlsplit(candidate.get("profile_url") or "").hostname or "").lower()
    except ValueError:
        return "third_party"
    if host and profile_host and (host == profile_host or host.endswith("." + profile_host)):
        return "official"
    org = _tokens(candidate.get("organization"))
    return "official" if any(len(t) >= 5 and t in host.replace("-", "") for t in org) else "third_party"
