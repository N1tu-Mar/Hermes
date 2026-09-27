"""Outcome analytics and in-app notifications. No tracking pixels, no open tracking.

Every count is derived from stored records, never from an event counter:
`milestones` holds one row per (campaign, candidate, stage) with the first time
that stage was reached, so re-running work can't double-count. Outcomes after
sending (replied, interested, ...) are recorded by the user via the API/UI.

Unknown is `None` (JSON null), zero is 0. A rate with an empty denominator is
unknown, scheduled sends are unknown (HERMES has no scheduler), cost is unknown
unless per-token prices are configured, and usage is unknown for breakdowns it
can't be attributed to (organization, template, sender).
"""
import csv
import hashlib
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta

FUNNEL = ("discovered", "researched", "contactable", "drafted", "approved", "scheduled",
          "sent", "replied", "interested", "declined", "bounced", "meeting_booked")
OUTCOMES = ("replied", "interested", "declined", "bounced", "meeting_booked")
STAGES = set(FUNNEL) - {"scheduled"} | {"research_failed"}
GROUPS = ("campaign", "campaign_type", "sender", "template", "organization")
CLOSED = {"replied", "interested", "declined", "bounced", "meeting_booked"}  # stops a follow-up reminder
# Existing interaction kinds (see Workspace.record_contacted and rule followup_no_reply) so the contact ledger sees outcomes.
INTERACTION_KIND = {"replied": "reply", "interested": "interested", "declined": "decline",
                    "bounced": "bounce", "meeting_booked": "meeting"}


# ---------------------------------------------------------------- recording
def milestone(cache, campaign_id, candidate_id, stage, at=None):
    """First time a candidate reached a stage. Idempotent: returns False if already recorded."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    with cache.lock:
        cur = cache.db.execute("INSERT OR IGNORE INTO milestones VALUES (?,?,?,?)",
                               (campaign_id, candidate_id, stage, at or time.time()))
        return cur.rowcount == 1


def timeline(cache, campaign_id, candidate_id):
    return cache.q("SELECT stage, at FROM milestones WHERE campaign_id=? AND candidate_id=? ORDER BY at",
                   (campaign_id, candidate_id))


def notify(cache, key, kind, message, campaign_id=None, candidate_id=None):
    """Deduplicated by key. Returns True only for a new notification."""
    with cache.lock:
        cur = cache.db.execute(
            "INSERT OR IGNORE INTO notifications (dedupe_key, kind, message, campaign_id, candidate_id, created_at) "
            "VALUES (?,?,?,?,?,?)", (key, kind, message, campaign_id, candidate_id, time.time()))
        new = cur.rowcount == 1
    if new:
        desktop(message)
    return new


def desktop(message):
    """Optional OS notification, opt-in with HERMES_DESKTOP_NOTIFICATIONS=1. Silently skipped if unsupported."""
    if os.environ.get("HERMES_DESKTOP_NOTIFICATIONS") != "1":
        return
    if sys.platform == "darwin":
        cmd = ["osascript", "-e", f"display notification {json.dumps(message)} with title \"HERMES\""]
    elif shutil.which("notify-send"):
        cmd = ["notify-send", "HERMES", message]
    else:
        return
    try:
        subprocess.run(cmd, timeout=5, check=False, capture_output=True)
    except Exception:
        pass


def notifications(cache, include_dismissed=False):
    sql = "SELECT * FROM notifications" + ("" if include_dismissed else " WHERE dismissed_at IS NULL")
    items = cache.q(sql + " ORDER BY id DESC LIMIT 200")
    unread = cache.q("SELECT COUNT(*) n FROM notifications WHERE read_at IS NULL AND dismissed_at IS NULL")[0]["n"]
    return {"unread": unread, "items": items}


def mark(cache, notification_id, field):
    if not cache.q("SELECT 1 FROM notifications WHERE id=?", (notification_id,)):
        raise KeyError(f"notification {notification_id}")
    cache.x(f"UPDATE notifications SET {field}=COALESCE({field}, ?) WHERE id=?", (time.time(), notification_id))


def followups_due(cache, days=None, now=None):
    """Notify once per sent message with no recorded response after N days (HERMES_FOLLOWUP_DAYS, default 7)."""
    days = float(days if days is not None else os.environ.get("HERMES_FOLLOWUP_DAYS") or 7)
    cutoff = (now or time.time()) - days * 86400
    rows = cache.q("""SELECT s.campaign_id, s.candidate_id, s.at FROM milestones s
                      WHERE s.stage='sent' AND s.at<=? AND NOT EXISTS (
                        SELECT 1 FROM milestones o WHERE o.campaign_id=s.campaign_id
                        AND o.candidate_id=s.candidate_id AND o.stage IN (%s))""" % ",".join("?" * len(CLOSED)),
                   (cutoff, *sorted(CLOSED)))
    for r in rows:
        notify(cache, f"followup_due:{r['campaign_id']}:{r['candidate_id']}:{int(r['at'])}", "followup_due",
               f"{r['candidate_id']}: no response recorded {days:g} days after sending; follow-up due",
               r["campaign_id"], r["candidate_id"])


def snapshot_key(prefix, campaign_id, items):
    """Content-addressed dedupe key: the same result set never notifies twice."""
    return f"{prefix}:{campaign_id}:" + hashlib.sha256(json.dumps(sorted(items)).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- backfill
def backfill(svc):
    """Record milestones for data created before analytics existed. Safe to repeat (INSERT OR IGNORE).

    Timestamps are the best stored ones: campaign creation for discovery, researched_at,
    draft updated_at (approximate for approval), invited_at for sent.
    """
    for cid in svc.store.list_ids():
        try:
            cdoc, profiles = svc.store.candidates(cid), svc.store.research(cid)["profiles"]
        except Exception:
            continue
        created = _ts(cdoc.get("created_at"))
        for c in cdoc["candidates"]:
            milestone(svc.cache, cid, c["candidate_id"], "discovered", created)
        for cand_id, p in profiles.items():
            at = _ts(p.get("researched_at"))
            if p.get("status") in ("researched", "needs_contact_review"):
                milestone(svc.cache, cid, cand_id, "researched", at)
                if p.get("email_verified_on_page"):
                    milestone(svc.cache, cid, cand_id, "contactable", at)
            elif p.get("status") == "research_failed":
                milestone(svc.cache, cid, cand_id, "research_failed", at)
        for d in svc.cache.list_drafts(cid):
            if d["status"] in ("needs_review", "approved", "gmail_draft_created"):
                milestone(svc.cache, cid, d["candidate_id"], "drafted", d["updated_at"])
            if d["status"] in ("approved", "gmail_draft_created"):
                milestone(svc.cache, cid, d["candidate_id"], "approved", d["updated_at"])
            if d.get("invited_at"):
                milestone(svc.cache, cid, d["candidate_id"], "sent", d["invited_at"])


def _ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------- reporting
def date_range(start=None, end=None):
    """YYYY-MM-DD, local time, both inclusive. Returns epoch seconds or None."""
    lo = datetime.strptime(start, "%Y-%m-%d").timestamp() if start else None
    hi = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).timestamp() if end else None
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError("start date must be on or before end date")
    return lo, hi


def _in(at, lo, hi):
    return at is not None and (lo is None or at >= lo) and (hi is None or at < hi)


def contact_rows(svc, campaign_id=None, start=None, end=None):
    """One row per candidate with the stages reached inside the date range."""
    lo, hi = date_range(start, end)
    ids = [campaign_id] if campaign_id else svc.store.list_ids()
    if campaign_id:
        svc._require(campaign_id)
    # ponytail: reads every campaign's JSON per report; fine for a personal tool, add a summary table past ~100 campaigns.
    rows = []
    for cid in ids:
        try:
            cdoc = svc.store.candidates(cid)
        except Exception:
            continue
        ms = {}
        for m in svc.cache.q("SELECT candidate_id, stage, at FROM milestones WHERE campaign_id=?", (cid,)):
            ms.setdefault(m["candidate_id"], {})[m["stage"]] = m["at"]
        templates = {}
        for d in sorted(svc.cache.list_drafts(cid), key=lambda d: d["updated_at"] or 0):
            if d["status"] != "blocked":
                templates[d["candidate_id"]] = d["template_version"]
        sender = _sender(svc.cache, cid)
        for c in cdoc["candidates"]:
            times = ms.get(c["candidate_id"], {})
            reached = {s for s, at in times.items() if _in(at, lo, hi)}
            if not reached:
                continue
            rows.append({"campaign_id": cid, "candidate_id": c["candidate_id"], "name": c.get("name"),
                         "organization": c.get("organization") or None,
                         "campaign_type": cdoc["intake"].get("subtype") or cdoc["intake"].get("mode"),
                         "sender": sender, "template": templates.get(c["candidate_id"]),
                         "status": c.get("status"), "stages": reached, "times": times})
    return rows


def _sender(cache, campaign_id):
    """Sender identity is known only when the campaign's reusable content/attachments belong to one identity."""
    names = cache.q("""SELECT DISTINCT i.display_name FROM campaign_assets a
                       LEFT JOIN reusable_content r ON a.asset_type='content' AND r.content_id=a.asset_id
                       LEFT JOIN attachments t ON a.asset_type='attachment' AND t.attachment_id=a.asset_id
                       JOIN identities i ON i.id=COALESCE(r.identity_id, t.identity_id)
                       WHERE a.campaign_id=?""", (campaign_id,))
    return names[0]["display_name"] if len(names) == 1 else None


def _rate(n, d):
    return None if not d else round(n / d, 4)


def aggregate(rows):
    has = lambda s: sum(s in r["stages"] for r in rows)
    counts = {s: has(s) for s in FUNNEL}
    counts["scheduled"] = None  # no scheduled sending in HERMES: unknown, not zero
    counts["research_failed"] = has("research_failed")
    attempted = sum(bool({"researched", "research_failed"} & r["stages"]) for r in rows)
    positive = sum(bool({"interested", "meeting_booked"} & r["stages"]) for r in rows)
    hours = [(r["times"]["replied"] - r["times"]["sent"]) / 3600 for r in rows
             if "replied" in r["stages"] and "sent" in r["times"]]
    return {"counts": counts, "rates": {
        "verified_contact_rate": _rate(counts["contactable"], counts["researched"]),
        "research_failure_rate": _rate(counts["research_failed"], attempted),
        "approval_rate": _rate(counts["approved"], counts["drafted"]),
        "reply_rate": _rate(counts["replied"], counts["sent"]),
        "positive_response_rate": _rate(positive, counts["replied"]),
    }, "time_to_reply_hours": {
        "n": len(hours),
        "median": round(statistics.median(hours), 2) if hours else None,
        "mean": round(statistics.fmean(hours), 2) if hours else None,
    }}


def usage(svc, campaign_ids, start=None, end=None):
    lo, hi = date_range(start, end)
    if not campaign_ids:
        return {"api_calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_hits": 0,
                "estimated_cost_usd": _cost(0, 0), "processing_seconds": None, "jobs_timed": 0}
    marks = ",".join("?" * len(campaign_ids))
    where, args = f"campaign_id IN ({marks})", list(campaign_ids)
    dated = where
    if lo is not None or hi is not None:
        dated += " AND at IS NOT NULL" + (" AND at>=?" if lo is not None else "") + (" AND at<?" if hi is not None else "")
        args_dated = args + [x for x in (lo, hi) if x is not None]
    else:
        args_dated = args
    u = svc.cache.q(f"""SELECT COALESCE(SUM(api_calls),0) api_calls, COALESCE(SUM(input_tokens),0) input_tokens,
                        COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(cache_hits),0) cache_hits
                        FROM usage_log WHERE {dated}""", args_dated)[0]
    jobs_where = dated.replace(" at", " updated_at")
    j = svc.cache.q(f"""SELECT COUNT(*) n, SUM(updated_at-started_at) secs FROM jobs WHERE {jobs_where}
                        AND started_at IS NOT NULL AND status IN ('done','failed')""", args_dated)[0]
    out = {**u, "estimated_cost_usd": _cost(u["input_tokens"], u["output_tokens"]),
           "processing_seconds": round(j["secs"], 2) if j["n"] else None, "jobs_timed": j["n"]}
    if dated != where:
        out["undated_usage_excluded"] = bool(svc.cache.q(
            f"SELECT 1 FROM usage_log WHERE {where} AND at IS NULL LIMIT 1", args))
    return out


def _cost(input_tokens, output_tokens):
    """Needs HERMES_PRICE_INPUT_PER_MTOK and HERMES_PRICE_OUTPUT_PER_MTOK (USD); unknown otherwise.
    Excludes per-call web search fees."""
    try:
        pi = float(os.environ["HERMES_PRICE_INPUT_PER_MTOK"])
        po = float(os.environ["HERMES_PRICE_OUTPUT_PER_MTOK"])
    except (KeyError, ValueError):
        return None
    return round((input_tokens * pi + output_tokens * po) / 1e6, 4)


def report(svc, campaign_id=None, start=None, end=None, group_by="campaign"):
    if group_by not in GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(GROUPS)}")
    rows = contact_rows(svc, campaign_id, start, end)
    scope = [campaign_id] if campaign_id else svc.store.list_ids()
    groups = {}
    field = "campaign_id" if group_by == "campaign" else group_by
    for r in rows:
        groups.setdefault(r[field], []).append(r)
    breakdown = []
    for key, members in sorted(groups.items(), key=lambda kv: (kv[0] is None, str(kv[0]))):
        a = aggregate(members)
        cids = sorted({r["campaign_id"] for r in members})
        attributable = group_by in ("campaign", "campaign_type")
        breakdown.append({"group": key, "contacts": len(members), "campaign_ids": cids, **a,
                          "usage": usage(svc, _group_campaigns(svc, scope, group_by, key), start, end)
                          if attributable else None})
    return {"filters": {"campaign_id": campaign_id, "start": start, "end": end, "group_by": group_by},
            **aggregate(rows), "usage": usage(svc, scope, start, end), "breakdown": breakdown,
            "rows": [_public(r) for r in rows],
            "failures": failures(svc, scope, start, end), "activity": activity(svc, scope, start, end),
            "notes": {
                "scheduled": "HERMES has no scheduled sending; shown as unknown.",
                "outcomes": "Sent and later outcomes are recorded by you; 0 means none recorded. No open tracking.",
                "cost": None if _cost(0, 0) is not None else
                "Set HERMES_PRICE_INPUT_PER_MTOK and HERMES_PRICE_OUTPUT_PER_MTOK to estimate cost.",
                "breakdown_usage": "Usage is per campaign; it can't be split by organization, template, or sender.",
            }}


def _group_campaigns(svc, scope, group_by, key):
    if group_by == "campaign":
        return [key]
    out = []
    for cid in scope:
        try:
            intake = svc.store.candidates(cid)["intake"]
        except Exception:
            continue
        if (intake.get("subtype") or intake.get("mode")) == key:
            out.append(cid)
    return out


def _public(r):
    return {**{k: v for k, v in r.items() if k not in ("stages", "times")},
            "stages": sorted(r["stages"], key=lambda s: (FUNNEL + ("research_failed",)).index(s)),
            "times": r["times"]}


def failures(svc, scope, start=None, end=None):
    lo, hi = date_range(start, end)
    out = []
    for cid in scope:
        for j in svc.cache.jobs(cid, "failed"):
            if _in(j["updated_at"], lo, hi):
                out.append({"type": "job_failed", "campaign_id": cid, "candidate_id": _cand(j["candidate_id"]),
                            "at": j["updated_at"], "detail": f"{j['kind']}: {j['error']}"})
        for m in svc.cache.q("SELECT candidate_id, at FROM milestones WHERE campaign_id=? AND stage='research_failed'", (cid,)):
            if _in(m["at"], lo, hi):
                out.append({"type": "research_failed", "campaign_id": cid, "candidate_id": m["candidate_id"],
                            "at": m["at"], "detail": "no sourced evidence or model/budget error"})
        for d in svc.cache.list_drafts(cid):
            if d["status"] == "blocked" and _in(d["updated_at"], lo, hi):
                out.append({"type": "draft_blocked", "campaign_id": cid, "candidate_id": d["candidate_id"],
                            "at": d["updated_at"], "detail": "; ".join(d["issues"])})
    return sorted(out, key=lambda x: -x["at"])[:200]


def _cand(arg):
    """Write jobs store a JSON request as candidate_id."""
    if arg and arg.startswith("{"):
        try:
            return json.loads(arg).get("candidate_id")
        except ValueError:
            return None
    return (arg or "").partition("#")[0] or None


def activity(svc, scope, start=None, end=None, limit=100):
    lo, hi = date_range(start, end)
    if not scope:
        return []
    sql = f"SELECT campaign_id, at, message FROM events WHERE campaign_id IN ({','.join('?' * len(scope))})"
    args = list(scope)
    if lo is not None:
        sql += " AND at>=?"; args.append(lo)
    if hi is not None:
        sql += " AND at<?"; args.append(hi)
    return svc.cache.q(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))


def to_csv(rep, kind):
    """kind='aggregate' (one row per breakdown group) or 'rows' (one row per contact)."""
    buf = io.StringIO()
    blank = lambda v: "" if v is None else v  # empty cell = unknown; 0 = zero
    if kind == "aggregate":
        cols = ["group_by", "group", "contacts", *FUNNEL, "research_failed", *rep["rates"],
                "median_hours_to_reply", "api_calls", "input_tokens", "output_tokens", "cache_hits",
                "estimated_cost_usd", "processing_seconds", "campaign_ids"]
        w = csv.writer(buf)
        w.writerow(cols)
        for b in rep["breakdown"]:
            u = b["usage"] or {}
            w.writerow([blank(x) for x in [rep["filters"]["group_by"], b["group"], b["contacts"],
                        *[b["counts"][s] for s in FUNNEL], b["counts"]["research_failed"], *b["rates"].values(),
                        b["time_to_reply_hours"]["median"], u.get("api_calls"), u.get("input_tokens"),
                        u.get("output_tokens"), u.get("cache_hits"), u.get("estimated_cost_usd"),
                        u.get("processing_seconds"), " ".join(b["campaign_ids"])]])
    elif kind == "rows":
        stages = [s for s in FUNNEL if s != "scheduled"] + ["research_failed"]
        w = csv.writer(buf)
        w.writerow(["campaign_id", "candidate_id", "name", "organization", "campaign_type", "sender", "template",
                    "status", *[f"{s}_at" for s in stages]])
        for r in rep["rows"]:
            iso = lambda s: datetime.fromtimestamp(r["times"][s]).isoformat(timespec="seconds") if s in r["times"] else ""
            w.writerow([blank(r[k]) for k in ("campaign_id", "candidate_id", "name", "organization",
                                              "campaign_type", "sender", "template", "status")]
                       + [iso(s) for s in stages])
    else:
        raise ValueError("kind must be aggregate or rows")
    return buf.getvalue()
