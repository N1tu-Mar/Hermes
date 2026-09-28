"""Sending and other external actions: every body is a strict model, so typos and string booleans are refused."""

from fastapi import APIRouter

from ..http_contracts import (
    CampaignSending,
    GmailDrafts,
    SendConfirm,
    SendingSettings,
    SendOutcome,
    SendPreview,
    Suppress,
)


def routes(ctx):
    r, svc = APIRouter(), ctx.svc

    @r.get("/api/sending")
    def sending_status():
        return {**svc().outbox.status(), "audit": svc().outbox.global_audit(20)}

    @r.patch("/api/sending/settings")
    def sending_settings(body: SendingSettings):
        return svc().outbox.update_settings(body.model_dump(exclude_unset=True))

    @r.post("/api/sending/pause")
    def sending_pause():
        return svc().outbox.pause(True)

    @r.post("/api/sending/unpause")
    def sending_unpause():
        return svc().outbox.pause(False)

    @r.post("/api/sending/emergency-stop")
    def sending_emergency_stop():
        return svc().outbox.emergency_stop()

    @r.get("/api/suppressions")
    def suppressions():
        return svc().outbox.suppressions()

    @r.post("/api/suppressions")
    def suppress(body: Suppress):
        return svc().outbox.suppress(body.email, body.reason)

    @r.patch("/api/campaigns/{cid}/sending")
    def campaign_sending(cid: str, body: CampaignSending):
        svc()._require(cid)
        return svc().outbox.set_campaign_enabled(cid, body.enabled)

    @r.post("/api/campaigns/{cid}/sends/preview")
    def send_preview(cid: str, body: SendPreview):
        return svc().preview_send(cid, body.candidate_id, body.scheduled_at or None)

    @r.post("/api/campaigns/{cid}/sends")
    def send_confirm(cid: str, body: SendConfirm):
        return svc().confirm_send(cid, body.candidate_id, body.approval_hash, body.scheduled_at or None)

    @r.get("/api/campaigns/{cid}/sends")
    def send_list(cid: str):
        svc()._require(cid)
        return svc().outbox.list(cid)

    @r.get("/api/sends/{send_id}")
    def send_detail(send_id: str):
        return svc().outbox.detail(send_id)

    @r.post("/api/sends/{send_id}/cancel")
    def send_cancel(send_id: str):
        return svc().outbox.view(svc().outbox.cancel(send_id))

    @r.post("/api/sends/{send_id}/outcome")
    def send_outcome(send_id: str, body: SendOutcome):
        return svc().outbox.view(svc().outbox.record_outcome(send_id, body.outcome))

    @r.post("/api/campaigns/{cid}/gmail-drafts")
    async def gmail_drafts(cid: str, s: ctx.Svc, body: GmailDrafts):
        return {"results": await s.create_gmail_drafts(cid, body.candidate_ids)}

    return r
