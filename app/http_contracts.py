"""Request contracts shared by every route: strict bodies, one error shape, per-route size limits.

Strict means: unknown fields are refused and nothing is coerced ("false" is not a bool, 1 is not a str).
Limits are enforced by `BodyLimit` before the body is read, parsed, or decoded.
"""

from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse

JSON_LIMIT = 256 * 1024  # normal JSON bodies
CSV_LIMIT = 4 * 1024 * 1024  # CSV text inside JSON; csvio caps the text itself at 2 MB, the rest is escaping headroom
ATTACHMENT_LIMIT = 14 * 1024 * 1024  # base64 of workspace.MAX_ATTACHMENT_BYTES (10 MiB) is ~13.3 MiB, plus envelope


def body_limit(path):
    if path == "/api/attachments":
        return ATTACHMENT_LIMIT
    if path.endswith("/import"):
        return CSV_LIMIT
    return JSON_LIMIT


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ConfirmDelete(Strict):
    confirm: str = ""  # empty is refused by the service with a 409 that says what to send


class CampaignPatch(Strict):
    name: str | None = None
    archived: bool | None = None


class TemplateArchive(Strict):
    archived: bool = True


class DoNotContact(Strict):
    do_not_contact: bool = True
    reason: str | None = None


class RunRule(Strict):
    dry_run: bool = True


class CampaignSending(Strict):
    enabled: bool


class SendingSettings(Strict):
    """Partial update: only the fields sent are applied (the defaults below are never used)."""

    enabled: bool = False
    paused: bool = False
    timezone: str = ""
    quiet_start: str = ""
    quiet_end: str = ""
    daily_limit: int = 0
    hourly_limit: int = 0
    spacing_seconds: int = 0


class SendPreview(Strict):
    candidate_id: str
    scheduled_at: str | None = None


class SendConfirm(SendPreview):
    approval_hash: str


class SendOutcome(Strict):
    outcome: str


class Suppress(Strict):
    email: str
    reason: str = "do_not_contact"


class GmailDrafts(Strict):
    candidate_ids: list[str]


class OpenAIKey(Strict):
    api_key: str


def too_large(limit):
    return JSONResponse({"detail": f"request body too large (limit {limit} bytes)"}, 413)


class BodyLimit:
    """ASGI middleware: 413 from Content-Length alone, or as soon as streamed bytes pass the limit.

    A streamed overrun answers 413 itself and then reports a disconnect to the app, because framework body
    readers swallow exceptions raised from receive(); anything the app then tries to send is dropped."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = body_limit(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and not declared.isdigit():
            return await JSONResponse({"detail": "bad Content-Length"}, 400)(scope, receive, send)
        if declared is not None and int(declared) > limit:
            return await too_large(limit)(scope, receive, send)
        seen, over = 0, False

        async def counted():
            nonlocal seen, over
            if over:
                return {"type": "http.disconnect"}
            msg = await receive()
            seen += len(msg.get("body", b"")) if msg["type"] == "http.request" else 0
            if seen > limit:
                over = True
                await too_large(limit)(scope, receive, send)
                return {"type": "http.disconnect"}
            return msg

        async def guarded(msg):
            if not over:
                await send(msg)

        await self.app(scope, counted, guarded)
