"""Campaign lifecycle: rename/archive, duplicate, and permanent deletion (archived + exact-ID confirmation only)."""

from fastapi import APIRouter, Body

from ..http_contracts import CampaignPatch, ConfirmDelete


def routes(ctx):
    r, svc = APIRouter(), ctx.svc

    @r.patch("/api/campaigns/{cid}")
    def patch_campaign(cid: str, body: CampaignPatch):
        if body.name is not None:
            svc().rename(cid, body.name)
        if body.archived is not None:
            svc().archive(cid, body.archived)
        return svc().ledger.campaign_meta(cid)

    @r.post("/api/campaigns/{cid}/duplicate")
    def duplicate(cid: str, body: dict = Body(default={})):
        return {"campaign_id": svc().duplicate(cid, body.get("name"))}

    @r.post("/api/campaigns/{cid}/delete")
    def delete(cid: str, body: ConfirmDelete | None = None):
        return svc().delete(cid, (body or ConfirmDelete()).confirm)

    return r
