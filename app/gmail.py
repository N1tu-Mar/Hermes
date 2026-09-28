"""Gmail adapter: create drafts and read metadata of HERMES-created threads. Never sends.

Scopes:
  gmail.compose  - create/read drafts (drafts only; HERMES never calls send).
  gmail.metadata - read labels + headers (no bodies) so a thread HERMES created
                   can be checked for a sent message, a reply, or a bounce.
HERMES only ever calls threads.get with thread IDs it recorded when it created
a draft. It never lists or searches the inbox. Note: with gmail.metadata granted,
Gmail rejects format=full and `q` searches, so this module uses only
format=metadata/minimal and plain listing of drafts. Sending is separately
opt-in and requires gmail.send plus application-level safety gates.

Each draft carries an X-Outreach-Key header so a retry after a timeout can
reconcile against existing drafts instead of creating a duplicate.

Sending goes draft -> drafts.send. Gmail deletes a draft when it sends it and
cannot send it twice, so "does our draft still exist?" answers "did the send
land?" without any inbox read scope. `can_send` is True only when the token
was explicitly granted gmail.send (python -m app.gmail --enable-sending).
gmail.compose alone technically permits sending, so the app gates on this.
"""

import base64
import hashlib
import json
import os
from email.message import EmailMessage
from pathlib import Path

COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
METADATA = "https://www.googleapis.com/auth/gmail.metadata"
SCOPES = [COMPOSE, METADATA]
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
THREAD_HEADERS = ["From", "Subject", "Message-ID", "Auto-Submitted", "X-Outreach-Key"]
REAUTH = "Gmail authorization expired, was revoked, or lacks a scope. Run `python -m app.gmail` to reconnect."


class GmailAuthError(Exception):
    """Credentials unusable (expired/revoked/missing scope). The user must re-consent."""


def http_status(exc):
    return getattr(getattr(exc, "resp", None), "status", None)


def credentials_paths():
    cred = Path(os.path.expanduser(os.environ.get("GMAIL_CREDENTIALS", "~/.config/outreach/credentials.json")))
    token = Path(os.path.expanduser(os.environ.get("GMAIL_TOKEN", "~/.config/outreach/token.json")))
    return cred, token


def rfc_message_id(key):
    """Stable RFC Message-ID for one logical outbound message."""
    token = hashlib.sha256(str(key).encode()).hexdigest()[:32]
    return f"<m-hermes-{token}@outreach.local>"


def build_message(to, subject, body, key, reply_to=None, attachments=None,
                  thread_id=None, in_reply_to=None, message_id=None):
    msg = EmailMessage()
    if to:
        msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["X-Outreach-Key"] = key
    msg["Message-ID"] = message_id or rfc_message_id(key)
    if in_reply_to:  # Gmail threads a draft only if threadId, References/In-Reply-To and Subject agree
        msg["In-Reply-To"] = msg["References"] = in_reply_to
    msg.set_content(body)
    for item in attachments or []:
        media = item["media_type"].split("/", 1)
        if len(media) != 2:
            raise ValueError(f"invalid attachment media type: {item['media_type']}")
        msg.add_attachment(item["content"], maintype=media[0], subtype=media[1], filename=item["filename"])
    out = {"message": {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}}
    if thread_id:
        out["message"]["threadId"] = thread_id
    return out


def _run(request):
    """Execute; map auth failures to GmailAuthError and a missing thread/draft to KeyError."""
    try:
        return request.execute()
    except Exception as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        scope_denied = status == 403 and any(w in str(e).lower() for w in ("insufficient", "scope"))  # not rate limits
        if type(e).__name__ == "RefreshError" or status == 401 or scope_denied:
            raise GmailAuthError(REAUTH) from e
        if status == 404:
            raise KeyError("not found in Gmail") from e
        raise


def _ids(res, message_id=None):
    msg = res.get("message") or {}
    return {"draft_id": res["id"], "message_id": msg.get("id"), "thread_id": msg.get("threadId"),
            "rfc_message_id": message_id}


class GmailDrafts:
    """Thin wrapper over the official client. `service` injectable for tests."""

    def __init__(self, service, can_sync=True, can_send=False):
        self.svc, self.can_send, self._sender = service, can_send, None
        self.can_sync = can_sync  # False when the token predates the gmail.metadata scope

    @classmethod
    def connect(cls, interactive=False, want_send=False):
        """Returns a GmailDrafts, None when OAuth isn't set up, or raises GmailAuthError when it's revoked."""
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        cred_path, token_path = credentials_paths()
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path))  # keeps the scopes actually granted
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                if not interactive:
                    raise GmailAuthError(REAUTH)
                creds = None
        wants_upgrade = creds and interactive and (
            not creds.has_scopes(SCOPES) or (want_send and not creds.has_scopes([SEND_SCOPE])))
        if not (creds and creds.valid and creds.has_scopes([COMPOSE])) or wants_upgrade:
            if not (interactive and cred_path.exists()):
                return None
            from google_auth_oauthlib.flow import InstalledAppFlow
            scopes = SCOPES + ([SEND_SCOPE] if want_send else [])
            creds = InstalledAppFlow.from_client_secrets_file(str(cred_path), scopes).run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        os.chmod(token_path, 0o600)
        return cls(build("gmail", "v1", credentials=creds, cache_discovery=False),
                   can_sync=creds.has_scopes([METADATA]), can_send=creds.has_scopes([SEND_SCOPE]))

    @classmethod
    def from_token_json(cls, text):
        """Remote mode: build from a user's stored (decrypted) OAuth token instead of a token file."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_info(json.loads(text), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return cls(build("gmail", "v1", credentials=creds, cache_discovery=False),
                   can_sync=creds.has_scopes([METADATA]), can_send=creds.has_scopes([SEND_SCOPE]))

    def create(self, to, subject, body, key, reply_to=None, attachments=None,
               thread_id=None, in_reply_to=None):
        """Returns {draft_id, message_id, thread_id}."""
        message_id = rfc_message_id(key)
        payload = build_message(to, subject, body, key, reply_to, attachments, thread_id, in_reply_to, message_id)
        return _ids(_run(self.svc.users().drafts().create(userId="me", body=payload)), message_id)

    def draft_exists(self, draft_id):
        """True/False from Gmail; network or server errors raise so callers stay 'uncertain'."""
        try:
            _run(self.svc.users().drafts().get(userId="me", id=draft_id, format="minimal"))
            return True
        except KeyError:
            return False
        except Exception as e:
            if http_status(e) == 404:
                return False
            raise

    def send_draft(self, draft_id):
        """Returns Gmail's Message resource (id, threadId, labelIds)."""
        return self.svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()

    def sender(self):
        """Authenticated address; Gmail sets From to it."""
        if not self._sender:
            self._sender = self.svc.users().getProfile(userId="me").execute()["emailAddress"]
        return self._sender

    def find_by_key(self, key, scan=100):
        """Reconcile: look through recent drafts for our header. Bounded scan. Returns ids or None."""
        res = _run(self.svc.users().drafts().list(userId="me", maxResults=scan))
        for d in res.get("drafts", []):
            full = _run(self.svc.users().drafts().get(userId="me", id=d["id"], format="metadata"))
            headers = full.get("message", {}).get("payload", {}).get("headers", [])
            if any(h.get("name") == "X-Outreach-Key" and h.get("value") == key for h in headers):
                return _ids(full, next((h.get("value") for h in headers if h.get("name", "").lower() == "message-id"),
                                       rfc_message_id(key)))
        return None

    def message(self, message_id):
        """Metadata for one known Gmail message; never searches or reads a body."""
        messages = getattr(self.svc.users(), "messages", None)
        if not messages:
            raise NotImplementedError("Gmail message metadata is unavailable")
        res = _run(messages().get(userId="me", id=message_id, format="metadata",
                                  metadataHeaders=THREAD_HEADERS))
        headers = {h["name"].lower(): h["value"] for h in (res.get("payload") or {}).get("headers", [])}
        return {"id": res["id"], "thread_id": res.get("threadId"), "labels": res.get("labelIds") or [],
                "at": int(res.get("internalDate") or 0) / 1000, "headers": headers}

    def delivery_state(self, draft_id, message_id=None, thread_id=None, key=None):
        """Classify a known draft as present, sent, or deleted using persisted provider IDs.

        Provider/network errors are deliberately allowed to escape: callers must
        keep the outcome uncertain and must not resend on an inconclusive read.
        """
        try:
            full = _run(self.svc.users().drafts().get(userId="me", id=draft_id, format="metadata"))
            ids = _ids(full, rfc_message_id(key) if key else None)
            return {"state": "draft", **ids}
        except KeyError:
            pass

        checked = False
        if message_id:
            try:
                msg = self.message(message_id)
                checked = True
                if "SENT" in msg["labels"]:
                    return {"state": "sent", "message_id": msg["id"], "thread_id": msg["thread_id"],
                            "rfc_message_id": msg["headers"].get("message-id"), "delivered_at": msg["at"]}
            except KeyError:
                checked = True
            except NotImplementedError:
                pass

        if thread_id and self.can_sync:
            checked = True
            try:
                messages = self.thread(thread_id)
            except KeyError:
                messages = []
            expected_rfc = rfc_message_id(key) if key else None
            for msg in messages:
                headers = msg["headers"]
                matches = (message_id and msg["id"] == message_id) or (key and headers.get("x-outreach-key") == key) \
                    or (expected_rfc and headers.get("message-id") == expected_rfc)
                if matches and "SENT" in msg["labels"]:
                    return {"state": "sent", "message_id": msg["id"], "thread_id": thread_id,
                            "rfc_message_id": headers.get("message-id") or expected_rfc,
                            "delivered_at": msg["at"]}
        return {"state": "deleted" if checked else "unknown_missing"}

    def thread(self, thread_id):
        """Compact metadata for one HERMES thread: id, labels, time, a few headers. No bodies."""
        if not self.can_sync:
            raise GmailAuthError("Reply tracking needs the gmail.metadata scope. Run `python -m app.gmail` to grant it.")
        res = _run(self.svc.users().threads().get(userId="me", id=thread_id, format="metadata",
                                                  metadataHeaders=THREAD_HEADERS))
        return [{"id": m["id"], "labels": m.get("labelIds") or [], "at": int(m.get("internalDate") or 0) / 1000,
                 "headers": {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers", [])}}
                for m in res.get("messages", [])]


def web_flow(redirect_uri):
    """Remote mode OAuth (web application client). The desktop flow above only works on the local machine."""
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_secrets_file(str(credentials_paths()[0]), SCOPES, redirect_uri=redirect_uri)


if __name__ == "__main__":
    # One-time desktop OAuth; add --enable-sending to also grant gmail.send.
    import sys
    try:
        g = GmailDrafts.connect(interactive=True, want_send="--enable-sending" in sys.argv)
    except GmailAuthError as e:
        g = None
        print(e)
    print(("Gmail connected (drafts + reply tracking"
           + (" + sending" if g.can_send else "") + ").") if g and g.can_sync
          else f"Put your OAuth client JSON at {credentials_paths()[0]} first." if not g
          else "Gmail connected for drafts only; reply tracking scope was not granted.")
