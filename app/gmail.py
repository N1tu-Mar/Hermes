"""Gmail adapter: drafts (scope gmail.compose) and opt-in sending (gmail.send).

Each draft carries an X-Outreach-Key header so a retry after a timeout can
reconcile against existing drafts instead of creating a duplicate.

Sending goes draft -> drafts.send. Gmail deletes a draft when it sends it and
cannot send it twice, so "does our draft still exist?" answers "did the send
land?" without any inbox read scope. `can_send` is True only when the token
was explicitly granted gmail.send (python -m app.gmail --enable-sending).
gmail.compose alone technically permits sending, so the app gates on this.
"""
import base64
import os
from email.message import EmailMessage
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def http_status(exc):
    return getattr(getattr(exc, "resp", None), "status", None)


def credentials_paths():
    cred = Path(os.path.expanduser(os.environ.get("GMAIL_CREDENTIALS", "~/.config/outreach/credentials.json")))
    token = Path(os.path.expanduser(os.environ.get("GMAIL_TOKEN", "~/.config/outreach/token.json")))
    return cred, token


def build_message(to, subject, body, key):
    msg = EmailMessage()
    if to:
        msg["To"] = to
    msg["Subject"] = subject
    msg["X-Outreach-Key"] = key
    msg.set_content(body)
    return {"message": {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}}


class GmailDrafts:
    """Thin wrapper over the official client. `service` injectable for tests."""

    def __init__(self, service, can_send=False):
        self.svc, self.can_send, self._sender = service, can_send, None

    @classmethod
    def connect(cls, interactive=False, want_send=False):
        """Returns a GmailDrafts or None when OAuth isn't set up. Keeps the scopes the token was granted."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        cred_path, token_path = credentials_paths()
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path))
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not (creds and creds.valid) or (want_send and not creds.has_scopes([SEND_SCOPE])):
            if not (interactive and cred_path.exists()):
                return None
            from google_auth_oauthlib.flow import InstalledAppFlow
            scopes = SCOPES + ([SEND_SCOPE] if want_send else [])
            creds = InstalledAppFlow.from_client_secrets_file(str(cred_path), scopes).run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        os.chmod(token_path, 0o600)
        return cls(build("gmail", "v1", credentials=creds, cache_discovery=False), creds.has_scopes([SEND_SCOPE]))

    def create(self, to, subject, body, key):
        res = self.svc.users().drafts().create(userId="me", body=build_message(to, subject, body, key)).execute()
        return res["id"]

    def draft_exists(self, draft_id):
        """True/False from Gmail; network or server errors raise so callers stay 'uncertain'."""
        try:
            self.svc.users().drafts().get(userId="me", id=draft_id, format="minimal").execute()
            return True
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
        """Reconcile: look through recent drafts for our header. Bounded scan."""
        res = self.svc.users().drafts().list(userId="me", maxResults=scan).execute()
        for d in res.get("drafts", []):
            full = self.svc.users().drafts().get(userId="me", id=d["id"], format="metadata").execute()
            headers = full.get("message", {}).get("payload", {}).get("headers", [])
            if any(h.get("name") == "X-Outreach-Key" and h.get("value") == key for h in headers):
                return d["id"]
        return None


if __name__ == "__main__":
    # One-time desktop OAuth: python -m app.gmail   (add --enable-sending to also grant gmail.send)
    import sys
    g = GmailDrafts.connect(interactive=True, want_send="--enable-sending" in sys.argv)
    if not g:
        print(f"Put your OAuth client JSON at {credentials_paths()[0]} first.")
    else:
        print("Gmail connected: drafts" + (" + gmail.send granted (sending stays off until enabled in the app)."
                                           if g.can_send else " only."))
