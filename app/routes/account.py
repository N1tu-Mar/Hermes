"""Remote-mode account settings and the Gmail OAuth handshake."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..campaigns import Rejected
from ..http_contracts import OpenAIKey


def routes(ctx):
    r, cfg, state = APIRouter(), ctx.cfg, ctx.state

    @r.get("/api/account")
    def account(request: Request, s: ctx.Svc):
        uid = ctx.remote_user(request)
        return {
            "user": request.state.user["username"],
            "gmail_connected": s.gmail is not None,
            "openai_key_set": state["accounts"].get_secret(uid, "openai_api_key") is not None,
            "demo": getattr(s.model, "demo", False),
        }

    @r.put("/api/account/openai-key")
    async def set_openai_key(request: Request, body: OpenAIKey):
        uid = ctx.remote_user(request)
        key = body.api_key.strip()
        if not key.startswith("sk-") or len(key) < 20:
            raise ValueError("that does not look like an OpenAI API key")
        state["accounts"].put_secret(uid, "openai_api_key", key)
        await ctx.pool.reload(uid)
        return {"openai_key_set": True}

    @r.delete("/api/account/openai-key")
    async def delete_openai_key(request: Request):
        uid = ctx.remote_user(request)
        state["accounts"].delete_secret(uid, "openai_api_key")
        await ctx.pool.reload(uid)
        return {"openai_key_set": False}

    @r.delete("/api/account/gmail")
    async def disconnect_gmail(request: Request):
        uid = ctx.remote_user(request)
        state["accounts"].delete_secret(uid, "gmail_token")
        await ctx.pool.reload(uid)
        return {"gmail_connected": False}

    @r.get("/auth/gmail/start")
    def gmail_start(request: Request):
        uid = ctx.remote_user(request)
        from ..gmail import credentials_paths, web_flow

        if not credentials_paths()[0].exists():
            raise Rejected("the operator has not configured a Gmail OAuth client (GMAIL_CREDENTIALS)")
        flow = web_flow(cfg.public_url + "/auth/gmail/callback")
        url, st = flow.authorization_url(access_type="offline", prompt="consent")
        ctx.oauth.add(st, uid, getattr(flow, "code_verifier", None))
        return RedirectResponse(url, 303)

    @r.get("/auth/gmail/callback")
    async def gmail_callback(request: Request, code: str = ""):
        if not cfg.remote:
            raise HTTPException(404)
        # The state is single-use, expiring, and bound to the user who started the flow; the session cookie
        # (SameSite=Strict) is not sent on this cross-site redirect, so the state is what identifies the user.
        hit = ctx.oauth.pop(request.query_params.get("state", ""))
        if not hit or not code:
            raise HTTPException(400, "unknown or expired Gmail authorization; start again")
        uid, verifier = hit
        from ..gmail import web_flow

        flow = web_flow(cfg.public_url + "/auth/gmail/callback")
        if verifier:
            flow.code_verifier = verifier
        flow.fetch_token(code=code)
        state["accounts"].put_secret(uid, "gmail_token", flow.credentials.to_json())
        await ctx.pool.reload(uid)
        return RedirectResponse("/", 303)

    return r
