"""Thin stdio MCP adapter. Calls the running app over loopback HTTP only.

It never opens campaign files; the app process remains the only writer and
enforces the same stage/approval rules as the UI.
Run: python -m app.mcp_server   (with the app already running)
"""
import os
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer

BASE = f"http://127.0.0.1:{os.environ.get('APP_PORT', 8765)}/api"
TOKEN_FILE = Path(os.path.expanduser("~/.config/outreach/app_token"))

mcp = MCPServer("outreach-desk", instructions=(
    "Research and outreach assistant for a Rutgers student. Creates Gmail DRAFTS only after a human approved "
    "each draft in the web UI; it never sends email."))


def call(method, path, body=None, params=None):
    token = os.environ.get("APP_TOKEN") or (TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else "")
    try:
        r = httpx.request(method, BASE + path, json=body, params=params, headers={"X-App-Token": token}, timeout=30)
    except httpx.ConnectError:
        return {"error": "Outreach app is not running. Start it with: python -m app.api"}
    if r.status_code >= 400:
        return {"error": r.json().get("detail", r.text) if "json" in r.headers.get("content-type", "") else r.text}
    return r.json() if "json" in r.headers.get("content-type", "") else {"text": r.text}


@mcp.tool()
def create_campaign(request: str, sender_background: str = "", sender_identity_id: int = 0, subtype: str = "",
                    organizations: list[str] | None = None, outreach_goal: str = "", event_details: str = "",
                    max_candidates: int = 20, budget: int = 60, name: str = "") -> dict:
    """Create a campaign from a natural-language request. subtype: research_professor, startup, or speaker_mentor.
    Pass sender_identity_id (see list_identities) or a free-text sender_background.
    Returns the parsed intake so it can be checked; edit it in the web UI if anything is wrong."""
    parsed = call("POST", "/parse", {"text": request, "subtype": subtype or None})
    if "error" in parsed:
        return parsed
    intake = {**parsed["intake"], "sender_background": sender_background or None,
              "sender_identity_id": str(sender_identity_id) if sender_identity_id else None}
    for k, v in (("organizations", organizations), ("outreach_goal", outreach_goal), ("event_details", event_details)):
        if v:
            intake[k] = v
    res = call("POST", "/campaigns", {"intake": intake, "max_candidates": max_candidates, "budget": budget,
                                      "name": name or None})
    return {**res, "intake": intake, "open_question": parsed["question"]}


@mcp.tool()
def find_candidates(campaign_id: str) -> dict:
    """Start discovery for a campaign (runs in the background; poll list_campaign)."""
    return call("POST", f"/campaigns/{campaign_id}/discover")


@mcp.tool()
def research_candidates(campaign_id: str, candidate_ids: list[str], refresh: bool = False) -> dict:
    """Select and research specific candidates. Already-researched people are skipped unless refresh=True."""
    sel = call("POST", f"/campaigns/{campaign_id}/select", {"candidate_ids": candidate_ids, "action": "select"})
    if "error" in sel:
        return sel
    return call("POST", f"/campaigns/{campaign_id}/research", {"candidate_ids": candidate_ids, "refresh": refresh})


@mcp.tool()
def generate_drafts(campaign_id: str, candidate_ids: list[str], followup: bool = False) -> dict:
    """Draft personalized emails for researched candidates. Drafts land in needs_review; a human approves in the UI."""
    return call("POST", f"/campaigns/{campaign_id}/drafts/generate", {"candidate_ids": candidate_ids, "followup": followup})


@mcp.tool()
def list_campaign(campaign_id: str = "", query: str = "", status: str = "active") -> dict:
    """Without an id: list/search campaigns (status: active, archived, all). With an id: candidates, statuses, progress."""
    if not campaign_id:
        return {"campaigns": call("GET", "/campaigns", params={"q": query, "status": status})}
    return {"campaign": call("GET", f"/campaigns/{campaign_id}"), "progress": call("GET", f"/campaigns/{campaign_id}/progress")}


@mcp.tool()
def create_gmail_drafts(campaign_id: str, candidate_ids: list[str]) -> dict:
    """Create Gmail drafts (not sent) for the given candidate IDs. Only drafts a human already approved are created."""
    return call("POST", f"/campaigns/{campaign_id}/gmail-drafts", {"candidate_ids": candidate_ids})


@mcp.tool()
def manage_campaign(campaign_id: str, action: str, name: str = "") -> dict:
    """action: rename (needs name), archive, unarchive, or duplicate (copies configuration only).
    Permanent deletion is deliberately only available in the web UI."""
    if action == "rename":
        return call("PATCH", f"/campaigns/{campaign_id}", {"name": name})
    if action in ("archive", "unarchive"):
        return call("PATCH", f"/campaigns/{campaign_id}", {"archived": action == "archive"})
    if action == "duplicate":
        return call("POST", f"/campaigns/{campaign_id}/duplicate", {"name": name or None})
    return {"error": "action must be rename, archive, unarchive, or duplicate"}


@mcp.tool()
def search_contacts(query: str = "", tag: str = "", do_not_contact: str = "") -> dict:
    """Search the global contact ledger across all campaigns. do_not_contact: 'yes', 'no', or '' for both."""
    return {"contacts": call("GET", "/contacts", params={"q": query, "tag": tag, "dnc": do_not_contact})}


@mcp.tool()
def get_contact(contact_id: int) -> dict:
    """One contact: notes, tags, relationship, do-not-contact flag, every campaign they appeared in, and timeline."""
    return call("GET", f"/contacts/{contact_id}")


@mcp.tool()
def update_contact(contact_id: int, notes: str | None = None, tags: list[str] | None = None,
                   relationship: str = "", do_not_contact: bool | None = None, dnc_reason: str = "") -> dict:
    """Edit notes/tags/relationship (new, contacted, replied, meeting, declined, bounced) or the do-not-contact flag.
    Do-not-contact blocks drafting, approval, and Gmail draft creation for this person in every campaign."""
    patch = {k: v for k, v in (("notes", notes), ("tags", tags), ("relationship", relationship or None),
                               ("do_not_contact", do_not_contact), ("dnc_reason", dnc_reason or None)) if v is not None}
    return call("PATCH", f"/contacts/{contact_id}", patch)


@mcp.tool()
def log_interaction(contact_id: int, kind: str, detail: str = "", at: str = "") -> dict:
    """Record something that happened outside HERMES. kind: invitation, followup, reply, meeting, decline, bounce, note.
    at: optional ISO date/time."""
    return call("POST", f"/contacts/{contact_id}/interactions", {"kind": kind, "detail": detail, "at": at or None})


@mcp.tool()
def list_contact_reviews() -> dict:
    """Possible duplicate people HERMES refused to merge automatically. Resolve them in the web UI."""
    return {"reviews": call("GET", "/contact-reviews")}


@mcp.tool()
def list_identities() -> dict:
    """Reusable sender identities (Nitu personally, each Rutgers club) with biography, signature, default ask."""
    return {"identities": call("GET", "/identities")}


@mcp.tool()
def import_csv(csv_text: str, kind: str = "contacts", campaign_id: str = "", commit: bool = False) -> dict:
    """Import contacts (kind=contacts) or candidates into a campaign (kind=candidates, needs campaign_id).
    commit=False returns a preview with per-row errors; call again with commit=True to apply valid rows only."""
    path = "/contacts/import" if kind == "contacts" else f"/campaigns/{campaign_id}/import"
    return call("POST", path, {"csv": csv_text, "commit": commit})


@mcp.tool()
def export_csv(campaign_id: str = "") -> dict:
    """CSV text of all contacts, or with campaign_id of that campaign's candidates, research status, and outcomes."""
    return call("GET", f"/campaigns/{campaign_id}/export.csv" if campaign_id else "/contacts/export.csv")


if __name__ == "__main__":
    mcp.run("stdio")
