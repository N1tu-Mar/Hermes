"""Gmail adapter: create drafts only (scope gmail.compose). Never sends.

Each draft carries an X-Outreach-Key header so a retry after a timeout can
reconcile against existing drafts instead of creating a duplicate.
"""
import base64
import os
from email.message import EmailMessage
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def credentials_paths():
    cred = Path(os.path.expanduser(os.environ.get("GMAIL_CREDENTIALS", "~/.config/outreach/credentials.json")))
    token = Path(os.path.expanduser(os.environ.get("GMAIL_TOKEN", "~/.config/outreach/token.json")))
    return cred, token


def build_message(to, subject, body, key, reply_to=None):
    msg = EmailMessage()
    if to:
        msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["X-Outreach-Key"] = key
    msg.set_content(body)
    return {"message": {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}}


class GmailDrafts:
    """Thin wrapper over the official client. `service` injectable for tests."""

    def __init__(self, service):
        self.svc = service

    @classmethod
    def connect(cls, interactive=False):
        """Returns a GmailDrafts or None when OAuth isn't set up."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        cred_path, token_path = credentials_paths()
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not (creds and creds.valid):
            if not (interactive and cred_path.exists()):
                return None
            from google_auth_oauthlib.flow import InstalledAppFlow
            creds = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES).run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        os.chmod(token_path, 0o600)
        return cls(build("gmail", "v1", credentials=creds, cache_discovery=False))

    def create(self, to, subject, body, key, reply_to=None):
        res = self.svc.users().drafts().create(userId="me", body=build_message(to, subject, body, key, reply_to)).execute()
        return res["id"]

    def exists(self, draft_id):
        try:
            self.svc.users().drafts().get(userId="me", id=draft_id, format="minimal").execute()
            return True
        except Exception:
            return False

    def find_by_key(self, key, scan=50):
        """Reconcile: look through recent drafts for our header. Bounded scan."""
        res = self.svc.users().drafts().list(userId="me", maxResults=scan).execute()
        for d in res.get("drafts", []):
            full = self.svc.users().drafts().get(userId="me", id=d["id"], format="metadata").execute()
            headers = full.get("message", {}).get("payload", {}).get("headers", [])
            if any(h.get("name") == "X-Outreach-Key" and h.get("value") == key for h in headers):
                return d["id"]
        return None


if __name__ == "__main__":
    # One-time desktop OAuth: python -m app.gmail
    g = GmailDrafts.connect(interactive=True)
    print("Gmail connected." if g else f"Put your OAuth client JSON at {credentials_paths()[0]} first.")
