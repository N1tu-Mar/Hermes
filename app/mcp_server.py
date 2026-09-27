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


def call(method, path, body=None):
    token = os.environ.get("APP_TOKEN") or (TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else "")
    try:
        r = httpx.request(method, BASE + path, json=body, headers={"X-App-Token": token}, timeout=30)
    except httpx.ConnectError:
        return {"error": "Outreach app is not running. Start it with: python -m app.api"}
    if r.status_code >= 400:
        return {"error": r.json().get("detail", r.text) if "json" in r.headers.get("content-type", "") else r.text}
    return r.json()


@mcp.tool()
def create_campaign(request: str, sender_background: str, subtype: str = "", organizations: list[str] | None = None,
                    outreach_goal: str = "", event_details: str = "", max_candidates: int = 20, budget: int = 60) -> dict:
    """Create a campaign from a natural-language request. subtype: research_professor, startup, or speaker_mentor.
    Returns the parsed intake so it can be checked; edit it in the web UI if anything is wrong."""
    parsed = call("POST", "/parse", {"text": request, "subtype": subtype or None})
    if "error" in parsed:
        return parsed
    intake = {**parsed["intake"], "sender_background": sender_background}
    for k, v in (("organizations", organizations), ("outreach_goal", outreach_goal), ("event_details", event_details)):
        if v:
            intake[k] = v
    res = call("POST", "/campaigns", {"intake": intake, "max_candidates": max_candidates, "budget": budget})
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
def list_campaign(campaign_id: str = "") -> dict:
    """Without an id: list campaigns. With an id: candidates, statuses, and progress."""
    if not campaign_id:
        return {"campaigns": call("GET", "/campaigns")}
    return {"campaign": call("GET", f"/campaigns/{campaign_id}"), "progress": call("GET", f"/campaigns/{campaign_id}/progress")}


@mcp.tool()
def create_gmail_drafts(campaign_id: str, candidate_ids: list[str]) -> dict:
    """Create Gmail drafts (not sent) for the given candidate IDs. Only drafts a human already approved are created."""
    return call("POST", f"/campaigns/{campaign_id}/gmail-drafts", {"candidate_ids": candidate_ids})


if __name__ == "__main__":
    mcp.run("stdio")
